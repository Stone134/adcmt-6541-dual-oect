#!/usr/bin/env python3
"""Hardware-synchronized four-channel OECT logging with an ADCMT 6541.

The 6541, not the host computer, generates the 1 s measurement cadence.  CH1 is
the timing master and CH2-CH4 are internally triggered slaves.  Each channel's
measurement memory is read in batches, so network/host latency cannot stretch
the sampling interval.

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import signal
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


FLOAT_RE = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[Ee][-+]?\d+)?")
INT_RE = re.compile(r"\d+")


class ADCMTError(RuntimeError):
    """Instrument, protocol, or validation failure."""


@dataclass(frozen=True)
class ChannelConfig:
    channel: int
    role: str
    voltage_v: float
    current_limit_a: float


@dataclass(frozen=True)
class RunConfig:
    host: str
    port: int
    socket_timeout_s: float
    period_s: float
    samples_per_gate: int
    gate_voltages_v: tuple[float, ...]
    vds_a_v: float
    vds_b_v: float
    current_limits_a: tuple[float, float, float, float]
    integration_code: str
    fixed_measurement_range: bool
    batch_samples: int
    poll_interval_s: float
    output_csv: str
    standby_when_finished: bool

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "RunConfig":
        limits = tuple(float(x) for x in raw["current_limits_a"])
        gates = tuple(float(x) for x in raw["gate_voltages_v"])
        cfg = cls(
            host=str(raw["host"]),
            port=int(raw.get("port", 5025)),
            socket_timeout_s=float(raw.get("socket_timeout_s", 15.0)),
            period_s=float(raw.get("period_s", 1.0)),
            samples_per_gate=int(raw.get("samples_per_gate", 3600)),
            gate_voltages_v=gates,
            vds_a_v=float(raw["vds_a_v"]),
            vds_b_v=float(raw["vds_b_v"]),
            current_limits_a=limits,  # type: ignore[arg-type]
            integration_code=str(raw.get("integration_code", "IT5")).upper(),
            fixed_measurement_range=bool(raw.get("fixed_measurement_range", False)),
            batch_samples=int(raw.get("batch_samples", 60)),
            poll_interval_s=float(raw.get("poll_interval_s", 0.25)),
            output_csv=str(raw.get("output_csv", "oect_sync_measurement.csv")),
            standby_when_finished=bool(raw.get("standby_when_finished", True)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.host:
            raise ADCMTError("host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ADCMTError("port must be between 1 and 65535")
        if not 0.001 <= self.period_s <= 60.0:
            raise ADCMTError("period_s must be between 0.001 and 60 seconds")
        if not 1 <= self.samples_per_gate <= 10000:
            raise ADCMTError("samples_per_gate must be 1..10000 (6541 memory limit)")
        if not self.gate_voltages_v:
            raise ADCMTError("gate_voltages_v must contain at least one value")
        if any(abs(v) > 3.0 for v in (*self.gate_voltages_v, self.vds_a_v, self.vds_b_v)):
            raise ADCMTError("this program fixes the source range at 3 V; all voltages must be <= 3 V")
        if len(self.current_limits_a) != 4:
            raise ADCMTError("current_limits_a must contain CH1, CH2, CH3, CH4 limits")
        if any(not 0 < x <= 0.5 for x in self.current_limits_a):
            raise ADCMTError("each current limit must be > 0 A and <= 0.5 A")
        if self.integration_code not in {f"IT{i}" for i in range(0, 9)}:
            raise ADCMTError("integration_code must be IT0..IT8 for DC measurements")
        if not 1 <= self.batch_samples <= self.samples_per_gate:
            raise ADCMTError("batch_samples must be between 1 and samples_per_gate")
        if self.poll_interval_s <= 0:
            raise ADCMTError("poll_interval_s must be positive")


class LineSocket:
    """Line-oriented ADCMT LAN transport (7-bit ASCII, port 5025)."""

    def __init__(self, host: str, port: int, timeout_s: float) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._socket: socket.socket | None = None
        self._reader = None

    def connect(self) -> None:
        self._socket = socket.create_connection((self.host, self.port), self.timeout_s)
        self._socket.settimeout(self.timeout_s)
        self._reader = self._socket.makefile("rb")

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def write(self, command: str) -> None:
        if self._socket is None:
            raise ADCMTError("instrument socket is not connected")
        payload = command.strip().encode("ascii") + b"\n"
        self._socket.sendall(payload)

    def query(self, command: str) -> str:
        self.write(command)
        if self._reader is None:
            raise ADCMTError("instrument socket is not connected")
        response = self._reader.readline()
        if not response:
            raise ADCMTError(f"connection closed while waiting for response to {command!r}")
        return response.decode("ascii", errors="strict").strip()

    def __enter__(self) -> "LineSocket":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class ADCMT6541:
    def __init__(self, transport: LineSocket) -> None:
        self.io = transport

    def write(self, command: str) -> None:
        self.io.write(command)

    def query(self, command: str) -> str:
        return self.io.query(command)

    def select(self, channel: int) -> None:
        if channel not in (1, 2, 3, 4):
            raise ADCMTError(f"invalid channel {channel}")
        self.write(f"SCH{channel}")

    def identify(self) -> str:
        response = self.query("*IDN?")
        if "6541" not in response:
            raise ADCMTError(f"expected ADCMT 6541, received: {response!r}")
        return response

    def set_all_standby_direct(self) -> None:
        # Direct per-channel SBY is safe even before synchronization is configured.
        for channel in (1, 2, 3, 4):
            self.select(channel)
            self.write("SBY")

    def set_master_standby(self) -> None:
        self.select(1)
        self.write("M1")
        self.write("SBY")

    def configure(self, cfg: RunConfig) -> None:
        period_ms = cfg.period_s * 1000.0
        # Manual section 2.9.2: slave Tp = master Tp - TA. TA is 1 ms
        # through 6 s and 2 ms above 6 s.
        timing_adjust_ms = 1.0 if period_ms <= 6000.0 else 2.0
        slave_period_ms = period_ms - timing_adjust_ms
        if slave_period_ms < 0.5:
            raise ADCMTError("synchronized operation requires a master period >= 1.5 ms")

        initial_gate = cfg.gate_voltages_v[0]
        channels = (
            ChannelConfig(1, "OECT A drain", cfg.vds_a_v, cfg.current_limits_a[0]),
            ChannelConfig(2, "OECT A gate", initial_gate, cfg.current_limits_a[1]),
            ChannelConfig(3, "OECT B drain", cfg.vds_b_v, cfg.current_limits_a[2]),
            ChannelConfig(4, "OECT B gate", initial_gate, cfg.current_limits_a[3]),
        )

        # Common remote format. The 6541 does not provide a measurement timestamp,
        # so timing is represented by the common hardware sample index in the CSV.
        self.write("DL1")
        self.write("OH0")

        for ch in channels:
            self.select(ch.channel)
            self.write("SBY")
            self.write("MD0")       # DC source mode
            self.write("VF")        # voltage source
            self.write("SVR4")      # fixed 3 V range; avoids source range changes
            self.write(f"LMI{format_number(ch.current_limit_a)}")
            self.write(f"SOV{format_number(ch.voltage_v)}")
            self.write("FX0")       # explicit measurement function
            self.write("F2")        # DC current measurement
            self.write("R1" if cfg.fixed_measurement_range else "R0")
            if not cfg.fixed_measurement_range:
                self.write("RD0")   # no extra delay after an automatic range change
            self.write(cfg.integration_code)
            self.write("AZ0")       # auto-zero off for deterministic timing
            self.write("RE5")       # 5.5 digit output
            self.write("SPN1")      # one measurement per common trigger
            self.write("ST0")
            self.write("RL")

            if ch.channel == 1:
                # The 5 us larger master delays compensate the approximately
                # 5 us master-to-slave internal synchronization delay.
                self.write(f"SP0,0.055,{format_number(period_ms)}")
                self.write("SD0.035")
                self.write("OP3")   # master OPERATE OUT
                self.write("CP6")   # master SYNC OUT
                self.write("TG0")   # no external trigger on master
                self.write("M1")    # hold until buffers and gate level are ready
            else:
                self.write(f"SP0,0.050,{format_number(slave_period_ms)}")
                self.write("SD0.030")
                self.write("OP1")   # slave OPR/STBY IN
                self.write("CP7")   # no COMPLETE/SYNC output on slave
                self.write("TG1")   # enable internal trigger input
                self.write("M1")    # slave trigger mode is HOLD

    def set_gate_voltage(self, channel: int, voltage_v: float) -> float:
        if channel not in (2, 4):
            raise ADCMTError("gate voltage channels are CH2 and CH4")
        self.select(channel)
        self.write(f"SOV{format_number(voltage_v)}")
        response = self.query("SOV?")
        return parse_first_float(response)

    def prepare_memory(self, sample_count: int) -> None:
        for channel in (1, 2, 3, 4):
            self.select(channel)
            self.write("ST0")
            self.write("RL")
            self.write(f"RNM{sample_count}")
            self.write("ST1")

    def start_synchronized_measurement(self) -> None:
        self.select(1)
        self.write("M0")

    def pause_synchronized_measurement(self) -> None:
        self.select(1)
        self.write("M1")

    def operate_all(self) -> None:
        self.select(1)
        self.write("OPR")

    def memory_count(self, channel: int) -> int:
        self.select(channel)
        response = self.query("SZ?")
        match = INT_RE.search(response)
        if not match:
            raise ADCMTError(f"cannot parse SZ? response from CH{channel}: {response!r}")
        return int(match.group(0))

    def fetch_memory(self, channel: int, start: int, stop: int) -> list[float]:
        if stop < start:
            return []
        self.select(channel)
        self.write(f"RDN{start},{stop}")
        response = self.query("RDT?")
        values = parse_measurement_list(response)
        expected = stop - start + 1
        if len(values) != expected:
            raise ADCMTError(
                f"CH{channel} returned {len(values)} values for {start}..{stop}; "
                f"expected {expected}. Response starts {response[:100]!r}"
            )
        return values


def format_number(value: float) -> str:
    return f"{value:.12g}"


def parse_first_float(text: str) -> float:
    match = FLOAT_RE.search(text)
    if not match:
        raise ADCMTError(f"no numeric value in response {text!r}")
    return float(match.group(0))


def parse_measurement_list(text: str) -> list[float]:
    values: list[float] = []
    for item in text.split(","):
        match = FLOAT_RE.search(item)
        if not match:
            raise ADCMTError(f"cannot parse measurement item {item!r}")
        value = float(match.group(0))
        if abs(value) >= 8.0e30:
            raise ADCMTError(f"instrument returned no-data/error sentinel {value:g}")
        values.append(value)
    return values


CSV_FIELDS = (
    "run_started_utc",
    "step_started_utc",
    "gate_step_index",
    "sample_index_in_step",
    "sample_index_global",
    "nominal_elapsed_s",
    "vds_a_set_v",
    "id_a_a",
    "vg_a_set_v",
    "ig_a_a",
    "vds_b_set_v",
    "id_b_a",
    "vg_b_set_v",
    "ig_b_a",
    "sync_skew_spec_us_approx",
)


class CsvSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=CSV_FIELDS)
        self._writer.writeheader()
        self._file.flush()

    def write_batch(self, rows: Iterable[dict[str, object]]) -> None:
        self._writer.writerows(rows)
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "CsvSink":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def rows_for_batch(
    *,
    cfg: RunConfig,
    run_started_utc: str,
    step_started_utc: str,
    step_index: int,
    start_index: int,
    vg_a_v: float,
    vg_b_v: float,
    channel_values: Sequence[Sequence[float]],
) -> Iterable[dict[str, object]]:
    lengths = {len(values) for values in channel_values}
    if len(lengths) != 1:
        raise ADCMTError(f"channel batch lengths differ: {sorted(lengths)}")
    batch_length = lengths.pop()
    id_a, ig_a, id_b, ig_b = channel_values
    for offset in range(batch_length):
        sample_in_step = start_index + offset
        sample_global = step_index * cfg.samples_per_gate + sample_in_step
        yield {
            "run_started_utc": run_started_utc,
            "step_started_utc": step_started_utc,
            "gate_step_index": step_index,
            "sample_index_in_step": sample_in_step,
            "sample_index_global": sample_global,
            "nominal_elapsed_s": f"{sample_global * cfg.period_s:.9f}",
            "vds_a_set_v": f"{cfg.vds_a_v:.12g}",
            "id_a_a": f"{id_a[offset]:.12g}",
            "vg_a_set_v": f"{vg_a_v:.12g}",
            "ig_a_a": f"{ig_a[offset]:.12g}",
            "vds_b_set_v": f"{cfg.vds_b_v:.12g}",
            "id_b_a": f"{id_b[offset]:.12g}",
            "vg_b_set_v": f"{vg_b_v:.12g}",
            "ig_b_a": f"{ig_b[offset]:.12g}",
            "sync_skew_spec_us_approx": "5",
        }


STOP_REQUESTED = False


def request_stop(_signum: int, _frame: object) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def run_measurement(cfg: RunConfig, output_path: Path) -> None:
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    run_started_utc = datetime.now(timezone.utc).isoformat()

    transport = LineSocket(cfg.host, cfg.port, cfg.socket_timeout_s)
    instrument: ADCMT6541 | None = None
    completed_normally = False
    try:
        transport.connect()
        instrument = ADCMT6541(transport)
        identity = instrument.identify()
        print(f"Connected: {identity}")

        instrument.set_all_standby_direct()
        instrument.configure(cfg)
        instrument.operate_all()

        with CsvSink(output_path) as sink:
            for step_index, requested_gate_v in enumerate(cfg.gate_voltages_v):
                if STOP_REQUESTED:
                    break

                instrument.pause_synchronized_measurement()
                vg_a_actual = instrument.set_gate_voltage(2, requested_gate_v)
                vg_b_actual = instrument.set_gate_voltage(4, requested_gate_v)
                if not math.isclose(vg_a_actual, requested_gate_v, abs_tol=1e-6):
                    raise ADCMTError(f"CH2 source verification failed: {vg_a_actual} V")
                if not math.isclose(vg_b_actual, requested_gate_v, abs_tol=1e-6):
                    raise ADCMTError(f"CH4 source verification failed: {vg_b_actual} V")

                instrument.prepare_memory(cfg.samples_per_gate)
                step_started_utc = datetime.now(timezone.utc).isoformat()
                instrument.start_synchronized_measurement()
                next_index = 0
                print(
                    f"Step {step_index + 1}/{len(cfg.gate_voltages_v)}: "
                    f"Vg={requested_gate_v:g} V, collecting {cfg.samples_per_gate} samples"
                )

                while next_index < cfg.samples_per_gate and not STOP_REQUESTED:
                    master_count = min(
                        instrument.memory_count(1), cfg.samples_per_gate
                    )
                    should_fetch = (
                        master_count - next_index >= cfg.batch_samples
                        or master_count == cfg.samples_per_gate
                    )
                    if should_fetch:
                        counts = [instrument.memory_count(ch) for ch in (1, 2, 3, 4)]
                        common_count = min(min(counts), cfg.samples_per_gate)
                        if common_count > next_index:
                            channel_values = [
                                instrument.fetch_memory(ch, next_index, common_count - 1)
                                for ch in (1, 2, 3, 4)
                            ]
                            sink.write_batch(
                                rows_for_batch(
                                    cfg=cfg,
                                    run_started_utc=run_started_utc,
                                    step_started_utc=step_started_utc,
                                    step_index=step_index,
                                    start_index=next_index,
                                    vg_a_v=vg_a_actual,
                                    vg_b_v=vg_b_actual,
                                    channel_values=channel_values,
                                )
                            )
                            next_index = common_count
                            print(
                                f"  stored {next_index}/{cfg.samples_per_gate}",
                                end="\r" if next_index < cfg.samples_per_gate else "\n",
                                flush=True,
                            )
                    if next_index < cfg.samples_per_gate:
                        time.sleep(cfg.poll_interval_s)

                instrument.pause_synchronized_measurement()
                if STOP_REQUESTED:
                    print("Stop requested; ending safely after the current query.")
                    break

                final_counts = [instrument.memory_count(ch) for ch in (1, 2, 3, 4)]
                if final_counts != [cfg.samples_per_gate] * 4:
                    raise ADCMTError(
                        "synchronized channel counts differ at step end: "
                        + ", ".join(f"CH{i + 1}={n}" for i, n in enumerate(final_counts))
                    )

        completed_normally = not STOP_REQUESTED
    finally:
        if instrument is not None and cfg.standby_when_finished:
            try:
                instrument.set_master_standby()
                print("All four channels set to Standby.")
            except Exception as exc:  # best-effort safety cleanup
                print(f"WARNING: could not set Standby during cleanup: {exc}", file=sys.stderr)
        transport.close()

    if not completed_normally:
        raise ADCMTError("measurement stopped before all gate steps completed")


def command_plan(cfg: RunConfig) -> list[str]:
    period_ms = cfg.period_s * 1000.0
    adjust_ms = 1.0 if period_ms <= 6000.0 else 2.0
    slave_period_ms = period_ms - adjust_ms
    initial_gate = cfg.gate_voltages_v[0]
    voltages = (cfg.vds_a_v, initial_gate, cfg.vds_b_v, initial_gate)
    lines = [
        "# Representative command plan (queries/readback omitted)",
        "DL1",
        "OH0",
    ]
    for channel, (voltage, limit) in enumerate(zip(voltages, cfg.current_limits_a), 1):
        lines.extend(
            [
                f"SCH{channel}",
                "SBY",
                "MD0",
                "VF",
                "SVR4",
                f"LMI{format_number(limit)}",
                f"SOV{format_number(voltage)}",
                "FX0",
                "F2",
                "R1" if cfg.fixed_measurement_range else "R0",
                *([] if cfg.fixed_measurement_range else ["RD0"]),
                cfg.integration_code,
                "AZ0",
                "RE5",
                "SPN1",
            ]
        )
        if channel == 1:
            lines.extend(
                [
                    f"SP0,0.055,{format_number(period_ms)}",
                    "SD0.035",
                    "OP3",
                    "CP6",
                    "TG0",
                    "M1",
                ]
            )
        else:
            lines.extend(
                [
                    f"SP0,0.050,{format_number(slave_period_ms)}",
                    "SD0.030",
                    "OP1",
                    "CP7",
                    "TG1",
                    "M1",
                ]
            )
    lines.extend(
        [
            "# Per gate step: pause master, set CH2/CH4, reset four memories, then:",
            "SCH1",
            "OPR",
            "M0",
            "# Fetch common address ranges with RDN start,stop followed by RDT?.",
        ]
    )
    return lines


def load_config(path: Path) -> RunConfig:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ADCMTError("configuration root must be a JSON object")
    return RunConfig.from_mapping(raw)


def resolve_output(config_path: Path, configured: str, override: str | None) -> Path:
    value = override if override is not None else configured
    path = Path(value)
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="JSON configuration file")
    parser.add_argument("--output", help="CSV output path (overrides output_csv in JSON)")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="connect to the 6541 and energize outputs; without this flag only print the plan",
    )
    args = parser.parse_args(argv)

    try:
        config_path = args.config.resolve()
        cfg = load_config(config_path)
        output_path = resolve_output(config_path, cfg.output_csv, args.output)

        if not args.execute:
            print("DRY RUN: no network connection and no outputs will be enabled.\n")
            print("\n".join(command_plan(cfg)))
            print(f"\nPlanned CSV: {output_path}")
            print("Re-run with --execute only after checking wiring, limits, and voltages.")
            return 0

        run_measurement(cfg, output_path)
        print(f"Completed. Data: {output_path}")
        return 0
    except (ADCMTError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
