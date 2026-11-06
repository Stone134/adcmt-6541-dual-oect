#!/usr/bin/env python3
"""Polished two-OECT front panel for synchronized ADCMT 6541 measurements."""

from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Iterable, Sequence

from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from adcmt_6541_sync import ADCMT6541, ADCMTError, LineSocket, format_number


APP_TITLE = "ADCMT 6541 - Dual OECT Synchronized Measurement"
LEGACY_HEADER = "Time\tGateI\tGateV\tDrainI\tDrainV\n"
DEFAULT_CURRENT_LIMIT_A = 0.3


def labview_scientific(value: float) -> str:
    """Match the historical LabVIEW text format, for example 1.0000000E-1."""
    mantissa, exponent = f"{value:.7E}".split("E")
    return f"{mantissa}E{int(exponent):+d}"


def voltage_levels(initial: float, final: float, step: float) -> tuple[float, ...]:
    if math.isclose(initial, final, abs_tol=1e-12):
        return (initial,)
    if math.isclose(step, 0.0, abs_tol=1e-15):
        raise ValueError("Vg step cannot be zero when initial and final Vg differ")
    if (final - initial) * step < 0:
        raise ValueError("Vg step sign must point from Vg initial toward Vg final")

    count = int(math.floor(abs((final - initial) / step) + 1e-10))
    levels = [initial + i * step for i in range(count + 1)]
    tolerance = max(abs(step) * 1e-9, 1e-12)
    if not math.isclose(levels[-1], final, abs_tol=tolerance):
        levels.append(final)
    else:
        levels[-1] = final
    return tuple(levels)


def common_hardware_interval(interval_a_s: float, interval_b_s: float) -> float:
    """Return a millisecond-resolution base interval shared by all four channels."""
    a_ms = int(round(interval_a_s * 1000.0))
    b_ms = int(round(interval_b_s * 1000.0))
    if a_ms < 100 or b_ms < 100:
        raise ValueError("Measurement intervals must be at least 0.100 s")
    return math.gcd(a_ms, b_ms) / 1000.0


@dataclass(frozen=True)
class CellPlan:
    name: str
    drain_channel: int
    gate_channel: int
    vds_v: float
    vg_initial_v: float
    vg_final_v: float
    vg_step_v: float
    hold_s: float
    delay_s: float
    interval_s: float
    ids_limit_a: float
    ig_limit_a: float
    output_path: Path
    enabled: bool = True
    cycles: int = 1
    mode: str = "sweep_cycles"
    keep_s: float = 0.0

    @cached_property
    def levels(self) -> tuple[float, ...]:
        if self.mode not in ("rise_keep", "sweep_cycles"):
            raise ValueError("Unknown sweep mode")
        if self.mode == "rise_keep":
            if not math.isfinite(self.keep_s) or self.keep_s < 0:
                raise ValueError("Keep at final must be nonnegative and finite")
            return voltage_levels(self.vg_initial_v, self.vg_final_v, self.vg_step_v)
        if isinstance(self.cycles, bool) or not isinstance(self.cycles, int) or self.cycles < 1:
            raise ValueError("Cycles must be a positive integer")
        forward = voltage_levels(self.vg_initial_v, self.vg_final_v, self.vg_step_v)
        # Share turning points and the initial voltage between successive cycles.
        round_trip = forward[1:] + forward[-2::-1] if len(forward) > 1 else ()
        return forward[:1] + round_trip * self.cycles

    @property
    def estimated_duration_s(self) -> float:
        return len(self.levels) * self.hold_s + self.final_keep_s


    @property
    def final_keep_s(self) -> float:
        return self.keep_s if self.mode == "rise_keep" else 0.0


@dataclass(frozen=True)
class ExperimentPlan:
    host: str
    port: int
    simulation: bool
    cell_a: CellPlan
    cell_b: CellPlan

    @property
    def active_cells(self) -> tuple[CellPlan, ...]:
        return tuple(cell for cell in (self.cell_a, self.cell_b) if cell.enabled)

    @property
    def hardware_interval_s(self) -> float:
        active = self.active_cells
        if not active:
            raise ValueError("Enable at least one OECT before starting")
        interval_ms = [int(round(cell.interval_s * 1000.0)) for cell in active]
        if any(value < 100 for value in interval_ms):
            raise ValueError("Measurement intervals must be at least 0.100 s")
        common_ms = interval_ms[0]
        for value in interval_ms[1:]:
            common_ms = math.gcd(common_ms, value)
        return common_ms / 1000.0


class LegacyTextWriter:
    """Write exactly the five-column format used by the historical LabVIEW VI."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w", encoding="ascii", newline="")
        self._handle.write(LEGACY_HEADER)
        self._handle.flush()

    def write(
        self,
        elapsed_s: float,
        gate_i_a: float,
        gate_v_v: float,
        drain_i_a: float,
        drain_v_v: float,
    ) -> None:
        values = (elapsed_s, gate_i_a, gate_v_v, drain_i_a, drain_v_v)
        self._handle.write("\t".join(labview_scientific(value) for value in values) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


@dataclass
class CellRuntime:
    plan: CellPlan
    writer: LegacyTextWriter | None
    level_index: int = 0
    stage_started_elapsed_s: float = 0.0
    measure_started_elapsed_s: float = 0.0
    stage_ends_elapsed_s: float = 0.0
    next_output_elapsed_s: float = 0.0
    done: bool = False
    samples_written: int = 0

    @property
    def gate_v(self) -> float:
        return self.plan.levels[self.level_index]

    @property
    def is_final_level(self) -> bool:
        return self.level_index == len(self.plan.levels) - 1

    def start_stage(self, elapsed_s: float) -> None:
        self.stage_started_elapsed_s = elapsed_s
        self.measure_started_elapsed_s = elapsed_s + self.plan.delay_s
        # The pre-measurement delay is inside the period/hold, not extra time.
        self.stage_ends_elapsed_s = elapsed_s + self.plan.hold_s
        if self.is_final_level:
            self.stage_ends_elapsed_s += self.plan.final_keep_s
        self.next_output_elapsed_s = self.measure_started_elapsed_s

    def advance(self, elapsed_s: float) -> bool:
        if self.is_final_level:
            self.done = True
            return False
        scheduled_start_s = self.stage_ends_elapsed_s
        self.level_index += 1
        # Anchor the next level to the planned boundary so small host-side
        # command/GUI delays do not accumulate over many hours of steps.
        self.start_stage(scheduled_start_s)
        self.measure_started_elapsed_s = max(
            self.measure_started_elapsed_s, elapsed_s + self.plan.delay_s
        )
        self.next_output_elapsed_s = self.measure_started_elapsed_s
        return True

    def should_record(self, sample_elapsed_s: float) -> bool:
        if self.done:
            return False
        tolerance = 0.0005
        if sample_elapsed_s + tolerance < self.measure_started_elapsed_s:
            return False
        if sample_elapsed_s - tolerance > self.stage_ends_elapsed_s:
            return False
        if sample_elapsed_s + tolerance < self.next_output_elapsed_s:
            return False
        # Start the interval from the first actual hardware sample after a
        # stage/delay boundary. The first sample can fall between nominal ticks.
        self.next_output_elapsed_s = sample_elapsed_s + self.plan.interval_s
        return True

    def progress_percent(self, elapsed_s: float) -> int:
        if self.done:
            return 100
        total = max(self.stage_ends_elapsed_s - self.stage_started_elapsed_s, 1e-9)
        part = max(0.0, min(1.0, (elapsed_s - self.stage_started_elapsed_s) / total))
        completed = self.level_index + part
        return min(99, int(round(100.0 * completed / len(self.plan.levels))))


class DualOECTInstrument:
    """ADCMT setup and memory operations for independent A/B source values."""

    def __init__(self, host: str, port: int, timeout_s: float = 15.0) -> None:
        self.transport = LineSocket(host, port, timeout_s)
        self.instrument: ADCMT6541 | None = None

    def connect(self) -> str:
        self.transport.connect()
        self.instrument = ADCMT6541(self.transport)
        return self.instrument.identify()

    def _inst(self) -> ADCMT6541:
        if self.instrument is None:
            raise ADCMTError("instrument is not connected")
        return self.instrument

    def configure(self, plan: ExperimentPlan) -> None:
        inst = self._inst()
        inst.set_all_standby_direct()
        base_ms = plan.hardware_interval_s * 1000.0
        timing_adjust_ms = 1.0 if base_ms <= 6000.0 else 2.0
        slave_ms = base_ms - timing_adjust_ms
        if slave_ms < 0.5:
            raise ADCMTError("hardware synchronization requires a base interval >= 0.100 s")

        channel_values = {
            1: (plan.cell_a.vds_v if plan.cell_a.enabled else 0.0, plan.cell_a.ids_limit_a),
            2: (
                plan.cell_a.vg_initial_v if plan.cell_a.enabled else 0.0,
                plan.cell_a.ig_limit_a,
            ),
            3: (plan.cell_b.vds_v if plan.cell_b.enabled else 0.0, plan.cell_b.ids_limit_a),
            4: (
                plan.cell_b.vg_initial_v if plan.cell_b.enabled else 0.0,
                plan.cell_b.ig_limit_a,
            ),
        }
        inst.write("DL1")
        inst.write("OH0")
        for channel in (1, 2, 3, 4):
            voltage_v, limit_a = channel_values[channel]
            inst.select(channel)
            for command in (
                "SBY",
                "MD0",
                "VF",
                "SVR4",
                f"LMI{format_number(limit_a)}",
                f"SOV{format_number(voltage_v)}",
                "FX0",
                "F2",
                "R0",
                "RD0",
                "IT5",
                "AZ0",
                "RE5",
                "SPN1",
                "ST0",
                "RL",
                "RNM10000",
                "ST1",
            ):
                inst.write(command)
            range_mode = inst.query("R?").replace(" ", "").upper()
            if range_mode not in {"R0", "0", "+0"}:
                raise ADCMTError(
                    f"CH{channel} current auto-range verification failed: {range_mode!r}"
                )
            if channel == 1:
                for command in (
                    f"SP0,0.055,{format_number(base_ms)}",
                    "SD0.035",
                    "OP3",
                    "CP6",
                    "TG0",
                    "M1",
                ):
                    inst.write(command)
            else:
                for command in (
                    f"SP0,0.050,{format_number(slave_ms)}",
                    "SD0.030",
                    "OP1",
                    "CP7",
                    "TG1",
                    "M1",
                ):
                    inst.write(command)

    def operate_and_start(self) -> None:
        inst = self._inst()
        inst.select(1)
        inst.write("OPR")
        inst.write("M0")

    def set_channel_voltage(self, channel: int, voltage_v: float) -> None:
        inst = self._inst()
        inst.select(channel)
        inst.write(f"SOV{format_number(voltage_v)}")
        returned = inst.query("SOV?")
        numeric = returned.replace("SOV", "").strip()
        try:
            actual = float(numeric)
        except ValueError as exc:
            raise ADCMTError(f"cannot parse SOV? response {returned!r}") from exc
        if not math.isclose(actual, voltage_v, abs_tol=1e-6):
            raise ADCMTError(
                f"CH{channel} voltage verification failed: requested {voltage_v}, got {actual}"
            )

    def set_gate(self, gate_channel: int, voltage_v: float) -> None:
        self.set_channel_voltage(gate_channel, voltage_v)

    def zero_cell(self, drain_channel: int, gate_channel: int) -> None:
        # CH1 remains operating at 0 V if A finishes first: its SYNC OUT must
        # keep clocking CH3/CH4 until B has completed its own schedule.
        self.set_channel_voltage(drain_channel, 0.0)
        self.set_channel_voltage(gate_channel, 0.0)

    def memory_count(self, channel: int) -> int:
        return self._inst().memory_count(channel)

    def fetch_all_channels(self, start: int, stop: int) -> tuple[list[float], ...]:
        inst = self._inst()
        return tuple(inst.fetch_memory(channel, start, stop) for channel in (1, 2, 3, 4))

    def rollover_memory(self) -> None:
        inst = self._inst()
        inst.select(1)
        inst.write("M1")
        for channel in (1, 2, 3, 4):
            inst.select(channel)
            inst.write("ST0")
            inst.write("RL")
            inst.write("RNM10000")
            inst.write("ST1")
        inst.select(1)
        inst.write("M0")

    def standby_and_close(self) -> None:
        if self.instrument is not None:
            try:
                self.instrument.set_master_standby()
            finally:
                self.transport.close()
                self.instrument = None


class AcquisitionWorker(QtCore.QThread):
    sample_ready = QtCore.pyqtSignal(str, float, float, float, float, float)
    progress_ready = QtCore.pyqtSignal(str, int, str)
    status_ready = QtCore.pyqtSignal(str)
    completed = QtCore.pyqtSignal(bool, str)

    def __init__(self, plan: ExperimentPlan) -> None:
        super().__init__()
        self.plan = plan
        self._stop_event = threading.Event()
        self._instrument: DualOECTInstrument | None = None

    def request_stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        writers: list[LegacyTextWriter] = []
        success = False
        message = "Measurement ended"
        try:
            states_list: list[CellRuntime] = []
            for cell in (self.plan.cell_a, self.plan.cell_b):
                writer = LegacyTextWriter(cell.output_path) if cell.enabled else None
                if writer is not None:
                    writers.append(writer)
                state = CellRuntime(cell, writer, done=not cell.enabled)
                if cell.enabled:
                    state.start_stage(0.0)
                else:
                    self.progress_ready.emit(cell.name, 0, "OFF | held at 0 V | no data file")
                states_list.append(state)
            states = tuple(states_list)

            if self.plan.simulation:
                self._run_simulation(states)
            else:
                self._run_instrument(states)

            if self._stop_event.is_set():
                message = "Stopped safely by user"
            else:
                success = True
                message = "Measurement completed"
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
        finally:
            if self._instrument is not None:
                try:
                    self._instrument.standby_and_close()
                except Exception as exc:
                    success = False
                    message += f"; Standby cleanup warning: {exc}"
            for writer in writers:
                try:
                    writer.close()
                except Exception:
                    pass
            self.completed.emit(success, message)

    def _run_simulation(self, states: Sequence[CellRuntime]) -> None:
        base_s = self.plan.hardware_interval_s
        run_started = time.monotonic()
        next_tick = run_started + base_s
        self.status_ready.emit(f"Simulation running - synchronized base interval {base_s:g} s")

        while not self._stop_event.is_set() and not all(state.done for state in states):
            now = time.monotonic()
            self._advance_due_stages(states, now - run_started, instrument=None)
            remaining = next_tick - now
            if remaining > 0:
                time.sleep(min(remaining, 0.05))
                continue
            elapsed_s = next_tick - run_started
            values = self._synthetic_values(states, elapsed_s)
            self._process_sample(states, elapsed_s, values)
            next_tick += base_s
            if next_tick < time.monotonic() - base_s:
                next_tick = time.monotonic() + base_s

    def _run_instrument(self, states: Sequence[CellRuntime]) -> None:
        instrument = DualOECTInstrument(self.plan.host, self.plan.port)
        self._instrument = instrument
        identity = instrument.connect()
        self.status_ready.emit(f"Connected: {identity}")
        instrument.configure(self.plan)
        run_started = time.monotonic()
        instrument.operate_and_start()
        epoch_started_elapsed_s = time.monotonic() - run_started
        epoch_address_start = 0
        next_address = 0
        base_s = self.plan.hardware_interval_s
        self.status_ready.emit(f"Hardware synchronized at {base_s:g} s; streaming four channels")

        while not self._stop_event.is_set() and not all(state.done for state in states):
            now_elapsed_s = time.monotonic() - run_started
            if any(
                not state.done and now_elapsed_s >= state.stage_ends_elapsed_s
                for state in states
            ):
                next_address = self._fetch_available(
                    states,
                    instrument,
                    next_address,
                    epoch_address_start,
                    epoch_started_elapsed_s,
                    base_s,
                )
                self._advance_due_stages(states, time.monotonic() - run_started, instrument)

            master_count = instrument.memory_count(1)
            if master_count > next_address:
                next_address = self._fetch_available(
                    states,
                    instrument,
                    next_address,
                    epoch_address_start,
                    epoch_started_elapsed_s,
                    base_s,
                )

            if master_count >= 9000:
                self.status_ready.emit("Rolling measurement memory without changing source values")
                next_address = self._fetch_available(
                    states,
                    instrument,
                    next_address,
                    epoch_address_start,
                    epoch_started_elapsed_s,
                    base_s,
                )
                instrument.rollover_memory()
                epoch_started_elapsed_s = time.monotonic() - run_started
                epoch_address_start = 0
                next_address = 0

            time.sleep(min(0.05, base_s / 4.0))

        self._fetch_available(
            states,
            instrument,
            next_address,
            epoch_address_start,
            epoch_started_elapsed_s,
            base_s,
        )

    def _fetch_available(
        self,
        states: Sequence[CellRuntime],
        instrument: DualOECTInstrument,
        next_address: int,
        epoch_address_start: int,
        epoch_started_elapsed_s: float,
        base_s: float,
    ) -> int:
        counts = [instrument.memory_count(channel) for channel in (1, 2, 3, 4)]
        common_count = min(counts)
        if common_count <= next_address:
            return next_address
        channel_values = instrument.fetch_all_channels(next_address, common_count - 1)
        for offset, address in enumerate(range(next_address, common_count)):
            elapsed_s = epoch_started_elapsed_s + (address - epoch_address_start + 1) * base_s
            values = tuple(channel[offset] for channel in channel_values)
            self._process_sample(states, elapsed_s, values)
        return common_count

    def _advance_due_stages(
        self,
        states: Sequence[CellRuntime],
        elapsed_s: float,
        instrument: DualOECTInstrument | None,
    ) -> None:
        for state in states:
            if state.done or elapsed_s < state.stage_ends_elapsed_s:
                continue
            if state.is_final_level:
                if instrument is not None:
                    instrument.zero_cell(state.plan.drain_channel, state.plan.gate_channel)
                state.advance(elapsed_s)
                self.status_ready.emit(
                    f"{state.plan.name}: schedule complete; Vds and Vg set to 0 V"
                )
                self.progress_ready.emit(
                    state.plan.name,
                    100,
                    f"complete | outputs 0 V | {state.samples_written} samples",
                )
                continue
            changed = state.advance(elapsed_s)
            if changed:
                if instrument is not None:
                    instrument.set_gate(state.plan.gate_channel, state.gate_v)
                self.status_ready.emit(
                    f"{state.plan.name}: Vg -> {state.gate_v:g} V; "
                    f"waiting {state.plan.delay_s:g} s before logging"
                )

    def _process_sample(
        self,
        states: Sequence[CellRuntime],
        elapsed_s: float,
        values: Sequence[float],
    ) -> None:
        id_a, ig_a, id_b, ig_b = values
        paired = ((states[0], id_a, ig_a), (states[1], id_b, ig_b))
        for state, drain_i, gate_i in paired:
            if state.done:
                continue
            if state.should_record(elapsed_s):
                if state.writer is None:
                    raise RuntimeError(f"OECT {state.plan.name}: enabled cell has no data writer")
                state.writer.write(
                    elapsed_s,
                    gate_i,
                    state.gate_v,
                    drain_i,
                    state.plan.vds_v,
                )
                state.samples_written += 1
                self.sample_ready.emit(
                    state.plan.name,
                    elapsed_s,
                    gate_i,
                    state.gate_v,
                    drain_i,
                    state.plan.vds_v,
                )
            phase = "delay" if elapsed_s < state.measure_started_elapsed_s else "measuring"
            detail = (
                f"{phase} | level {state.level_index + 1}/{len(state.plan.levels)} | "
                f"Vg {state.gate_v:g} V | {state.samples_written} samples"
            )
            self.progress_ready.emit(
                state.plan.name,
                state.progress_percent(elapsed_s),
                detail,
            )

    @staticmethod
    def _synthetic_values(
        states: Sequence[CellRuntime], elapsed_s: float
    ) -> tuple[float, float, float, float]:
        results: list[float] = []
        for index, state in enumerate(states):
            if state.done:
                results.extend((0.0, 0.0))
                continue
            age = max(0.0, elapsed_s - state.stage_started_elapsed_s)
            response = 1.0 - math.exp(-age / (8.0 + index * 3.0))
            drain = state.plan.vds_v * (1e-6 + abs(state.gate_v) * 5e-4) * response
            drain += 2e-9 * math.sin(elapsed_s * (0.7 + index * 0.2))
            gate = -state.gate_v * 2e-8 + 4e-10 * math.cos(elapsed_s * 0.3)
            results.extend((drain, gate))
        return results[0], results[1], results[2], results[3]


class ConnectionTestWorker(QtCore.QThread):
    finished_test = QtCore.pyqtSignal(bool, str)

    def __init__(self, host: str, port: int) -> None:
        super().__init__()
        self.host = host
        self.port = port

    def run(self) -> None:
        transport = LineSocket(self.host, self.port, 5.0)
        try:
            transport.connect()
            identity = ADCMT6541(transport).identify()
            self.finished_test.emit(True, identity)
        except Exception as exc:
            self.finished_test.emit(False, str(exc))
        finally:
            transport.close()


class CompactDoubleSpinBox(QtWidgets.QDoubleSpinBox):
    """Keep input precision without displaying unnecessary trailing zeros."""

    def textFromValue(self, value: float) -> str:  # noqa: N802
        number = f"{value:.{self.decimals()}f}"
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        return "0" if number == "-0" else number


class CompactAxisItem(pg.AxisItem):
    """Show plot ticks without trailing decimal zeros, including SI-scaled axes."""

    def tickStrings(self, values: Sequence[float], scale: float, spacing: float) -> list[str]:  # noqa: N802
        labels = [f"{value * scale:.8g}" for value in values]
        return ["0" if label == "-0" else label for label in labels]


class CellControls:
    def __init__(self, name: str, accent: str, default_output: Path) -> None:
        self.default_output_dir = default_output.parent
        self.default_output_name = default_output.name
        self.group = QtWidgets.QGroupBox(f"OECT {name}")
        self.group.setProperty("accent", accent)
        grid = QtWidgets.QGridLayout(self.group)
        grid.setContentsMargins(14, 18, 14, 8)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self.switch_row = QtWidgets.QWidget()
        switch_layout = QtWidgets.QHBoxLayout(self.switch_row)
        switch_layout.setContentsMargins(0, 0, 0, 0)
        switch_layout.addWidget(QtWidgets.QLabel("Measurement channel"))
        switch_layout.addStretch(1)
        self.active = QtWidgets.QCheckBox("ON")
        self.active.setObjectName("cellToggle")
        self.active.setChecked(True)
        self.active.setToolTip("Turn this OECT measurement on or off for the next run")
        self.active.toggled.connect(self._active_changed)
        switch_layout.addWidget(self.active)
        grid.addWidget(self.switch_row, 0, 0, 1, 2)

        self.vds = self._spin(-3.0, 3.0, -0.01, 0.01, 4, " V")
        self.vg_initial = self._spin(-3.0, 3.0, 0.0, 0.1, 4, " V")
        self.vg_final = self._spin(-3.0, 3.0, -0.8, 0.1, 4, " V")
        self.vg_step = self._spin(-3.0, 3.0, -0.1, 0.1, 4, " V")
        self.hold = self._spin(0.1, 1_000_000.0, 60.0, 10.0, 3, " s")
        self.cycles = QtWidgets.QSpinBox()
        self.cycles.setRange(1, 1_000_000)
        self.cycles.setValue(1)
        self.cycles.setKeyboardTracking(False)
        self.cycles.setToolTip("One cycle sweeps initial → final → initial; no extra hold after the sweep")
        self.delay_ms = self._spin(0.0, 59_998.0, 1000.0, 100.0, 1, " ms")
        self.interval = self._spin(0.1, 60.0, 1.0, 0.1, 3, " s")

        self.keep = self._spin(0.0, 1_000_000.0, 3600.0, 60.0, 3, " s")
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("Rise and keep", "rise_keep")
        self.mode.addItem("Sweep in cycles", "sweep_cycles")
        self.mode.setCurrentIndex(1)
        self.mode.setToolTip("Choose a one-way sweep with a final hold, or repeated round trips")
        self.mode_value = QtWidgets.QStackedWidget()
        self.mode_value.addWidget(self.keep)
        self.mode_value.addWidget(self.cycles)

        rows = (
            (("Vds", self.vds), ("Period", self.hold)),
            (("Vg initial", self.vg_initial), ("Delay", self.delay_ms)),
            (("Vg final", self.vg_final), ("Cycles", self.mode_value)),
            (("Vg step", self.vg_step), ("Read interval", self.interval)),
        )
        for row, pairs in enumerate(rows):
            for column, (label, widget) in enumerate(pairs):
                field = QtWidgets.QWidget()
                field_layout = QtWidgets.QHBoxLayout(field)
                field_layout.setContentsMargins(0, 0, 0, 0)
                field_layout.setSpacing(8)
                caption = QtWidgets.QLabel(label)
                if widget is self.mode_value:
                    self.mode_caption = caption
                caption.setMinimumWidth(80)
                widget.setMinimumWidth(90)
                field_layout.addWidget(caption)
                field_layout.addWidget(widget, 1)
                grid.addWidget(field, row + 1, column)

        file_row = QtWidgets.QWidget()
        file_layout = QtWidgets.QHBoxLayout(file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.setSpacing(8)
        file_layout.addWidget(QtWidgets.QLabel("Raw-data file"))
        self.output = QtWidgets.QLineEdit(default_output.name)
        self.output.setToolTip(
            f"Historical five-column tab-delimited .txt output; relative paths use {self.default_output_dir}"
        )
        self.browse = QtWidgets.QPushButton("Browse")
        self.browse.setProperty("secondary", True)
        self.browse.clicked.connect(self._browse)
        file_layout.addWidget(self.output, 1)
        file_layout.addWidget(self.browse)
        mode_row = QtWidgets.QWidget()
        mode_layout = QtWidgets.QHBoxLayout(mode_row)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.addWidget(QtWidgets.QLabel("Program"))
        mode_layout.addWidget(self.mode, 1)
        grid.addWidget(mode_row, 5, 0, 1, 2)
        grid.addWidget(file_row, 6, 0, 1, 2)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFixedWidth(116)
        self.detail = QtWidgets.QLabel("Ready")
        self.detail.setProperty("muted", True)
        progress_row = QtWidgets.QWidget()
        progress_layout = QtWidgets.QHBoxLayout(progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(12)
        progress_layout.addWidget(self.progress)
        progress_layout.addWidget(self.detail, 1)
        grid.addWidget(progress_row, 7, 0, 1, 2)

        self._settings = (
            self.vds,
            self.vg_initial,
            self.vg_final,
            self.vg_step,
            self.hold,
            self.cycles,
            self.keep,
            self.mode,
            self.delay_ms,
            self.interval,
            self.output,
            self.browse,
        )

        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._mode_changed()

    def _mode_changed(self, *_args: object) -> None:
        rise_keep = self.mode.currentData() == "rise_keep"
        self.mode_value.setCurrentWidget(self.keep if rise_keep else self.cycles)
        self.mode_caption.setText("Keep at final" if rise_keep else "Cycles")

    @staticmethod
    def _spin(
        minimum: float,
        maximum: float,
        value: float,
        step: float,
        decimals: int,
        suffix: str,
    ) -> QtWidgets.QDoubleSpinBox:
        box = CompactDoubleSpinBox()
        box.setRange(minimum, maximum)
        box.setValue(value)
        box.setSingleStep(step)
        box.setDecimals(decimals)
        box.setSuffix(suffix)
        box.setKeyboardTracking(False)
        return box

    def _browse(self) -> None:
        initial_path = Path(self.output.text()).expanduser()
        if not initial_path.is_absolute():
            initial_path = self.default_output_dir / initial_path
        selected, _ = QtWidgets.QFileDialog.getSaveFileName(
            self.group,
            "Choose raw-data output",
            str(initial_path),
            "Text data (*.txt);;All files (*)",
        )
        if selected:
            if not selected.lower().endswith(".txt"):
                selected += ".txt"
            self.output.setText(selected)

    def _active_changed(self, checked: bool) -> None:
        self.active.setText("ON" if checked else "OFF")
        for widget in self._settings:
            widget.setEnabled(checked and self.active.isEnabled())
        self.progress.setValue(0)
        self.detail.setText("Ready" if checked else "OFF | held at 0 V | no data file")

    def set_enabled(self, enabled: bool) -> None:
        self.active.setEnabled(enabled)
        for widget in self._settings:
            widget.setEnabled(enabled and self.active.isChecked())


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1580, 960)
        self.setMinimumSize(1180, 760)
        self.worker: AcquisitionWorker | None = None
        self.connection_worker: ConnectionTestWorker | None = None
        self.plot_data: dict[str, dict[str, list[float]]] = {
            "A": {"t": [], "id": [], "vg": []},
            "B": {"t": [], "id": [], "vg": []},
        }

        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)
        outer.addWidget(self._header())

        body = QtWidgets.QWidget()
        body_layout = QtWidgets.QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)
        controls = self._control_panel()
        controls.setMinimumWidth(550)
        controls.setMaximumWidth(600)
        body_layout.addWidget(controls)
        body_layout.addWidget(self._plot_panel(), 1)
        outer.addWidget(body, 1)
        outer.addWidget(self._footer())
        self._apply_style()
        self._simulation_changed(True)

    def _header(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setObjectName("headerCard")
        layout = QtWidgets.QHBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        title_box = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel("Dual OECT synchronized measurement")
        title.setObjectName("title")
        subtitle = QtWidgets.QLabel(
            "ADCMT 6541 hardware-timed acquisition • independent A/B voltage schedules"
        )
        subtitle.setProperty("muted", True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        layout.addLayout(title_box, 1)
        self.mode_badge = QtWidgets.QLabel("SIMULATION")
        self.mode_badge.setObjectName("modeBadge")
        layout.addWidget(self.mode_badge)
        return card

    def _control_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        connection = QtWidgets.QGroupBox("Connection")
        grid = QtWidgets.QGridLayout(connection)
        grid.setContentsMargins(14, 18, 14, 8)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self.simulation = QtWidgets.QCheckBox("Simulation - no instrument commands")
        self.simulation.setChecked(True)
        self.simulation.toggled.connect(self._simulation_changed)
        self.host = QtWidgets.QLineEdit("192.168.1.204")
        self.host.setFixedWidth(140)
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(5025)
        self.port.setFixedWidth(82)
        self.test_button = QtWidgets.QPushButton("Test connection")
        self.test_button.setProperty("secondary", True)
        self.test_button.clicked.connect(self._test_connection)
        compliance = QtWidgets.QLabel("Auto current range • 0.3 A maximum compliance")
        compliance.setProperty("muted", True)
        compliance.setToolTip("Current measurement auto-ranges; maximum source compliance remains 0.3 A")
        grid.addWidget(self.simulation, 0, 0, 1, 3)
        grid.addWidget(compliance, 0, 3, 1, 3, QtCore.Qt.AlignRight)
        grid.addWidget(QtWidgets.QLabel("Instrument IP"), 1, 0)
        grid.addWidget(self.host, 1, 1)
        grid.addWidget(QtWidgets.QLabel("Port"), 1, 2)
        grid.addWidget(self.port, 1, 3)
        grid.addWidget(self.test_button, 1, 4, 1, 2)
        layout.addWidget(connection)

        output_root = Path(__file__).resolve().parent.parent
        self.controls_a = CellControls("A", "cyan", output_root / "measurement=lvl1.txt")
        self.controls_b = CellControls("B", "violet", output_root / "measurement=lvl2.txt")
        layout.addWidget(self.controls_a.group)
        layout.addWidget(self.controls_b.group)
        layout.addStretch(1)
        return panel

    def _plot_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        self.plots: dict[str, pg.PlotWidget] = {}
        self.curves: dict[str, pg.PlotDataItem] = {}
        plot_specs = (
            ("id_a", "OECT A • Drain current", "Id", "A", "#078A84"),
            ("id_b", "OECT B • Drain current", "Id", "A", "#7455B7"),
            ("vg_a", "OECT A • Gate voltage", "Vg", "V", "#1CA89B"),
            ("vg_b", "OECT B • Gate voltage", "Vg", "V", "#9874C8"),
        )
        for index, (key, title, ylabel, units, color) in enumerate(plot_specs):
            plot = pg.PlotWidget(
                background="#FFFCF6",
                axisItems={
                    "left": CompactAxisItem(orientation="left"),
                    "bottom": CompactAxisItem(orientation="bottom"),
                },
            )
            plot.setTitle(title, color="#273449", size="11pt")
            plot.setLabel("bottom", "Elapsed time", units="s", color="#526378")
            plot.setLabel("left", ylabel, units=units, color="#526378")
            plot.getViewBox().invertY(True)
            plot.showGrid(x=True, y=True, alpha=0.14)
            plot.getAxis("left").setTextPen("#526378")
            plot.getAxis("bottom").setTextPen("#526378")
            plot.getAxis("left").setPen("#CAD5E3")
            plot.getAxis("bottom").setPen("#CAD5E3")
            plot.setDownsampling(auto=True, mode="peak")
            plot.setClipToView(True)
            curve = plot.plot(pen=pg.mkPen(color, width=2), connect="finite")
            self.plots[key] = plot
            self.curves[key] = curve
            grid.addWidget(plot, index // 2, index % 2)
        return panel

    def _footer(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setObjectName("footerCard")
        layout = QtWidgets.QHBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        self.status_dot = QtWidgets.QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_label = QtWidgets.QLabel("Ready - simulation mode is enabled")
        self.status_label.setProperty("muted", True)
        self.summary_label = QtWidgets.QLabel("A: 0 samples   B: 0 samples")
        self.summary_label.setProperty("muted", True)
        self.start_button = QtWidgets.QPushButton("Start measurement")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QtWidgets.QPushButton("Stop safely")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        layout.addWidget(self.status_dot)
        layout.addWidget(self.status_label, 1)
        layout.addWidget(self.summary_label)
        layout.addSpacing(12)
        layout.addWidget(self.start_button)
        layout.addWidget(self.stop_button)
        return card

    def _apply_style(self) -> None:
        arrow_up = (Path(__file__).resolve().parent / "assets" / "spin_up.svg").as_posix()
        arrow_down = (Path(__file__).resolve().parent / "assets" / "spin_down.svg").as_posix()
        self.setStyleSheet(
            """
            QMainWindow { background: #F6F2EA; }
            QWidget { color: #273449; font-family: 'Helvetica Neue'; font-size: 13px; }
            QFrame#headerCard, QFrame#footerCard, QGroupBox { background: #FFFCF6; border: 1px solid #E2DACE; border-radius: 10px; }
            QFrame#headerCard { border-left: 4px solid #0D9D93; }
            QLabel#title { font-size: 23px; font-weight: 700; color: #1C3045; }
            QLabel[muted="true"] { color: #63758B; }
            QLabel#modeBadge { color: #075E57; background: #DDF6F0; border-radius: 10px; padding: 5px 12px; font-size: 11px; font-weight: 800; }
            QLabel#statusDot { color: #0D9D93; font-size: 16px; }
            QGroupBox { margin-top: 12px; padding: 7px 0 0 0; font-weight: 700; }
            QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: #30435B; }
            QGroupBox[accent="cyan"] { border-top: 2px solid #0D9D93; }
            QGroupBox[accent="violet"] { border-top: 2px solid #8260BE; }
            QLineEdit, QSpinBox, QDoubleSpinBox { background: #FFFDF9; border: 1px solid #D9D1C5; border-radius: 6px; padding: 3px 7px; color: #24364A; min-height: 20px; selection-background-color: #AFDAD6; }
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #0D9D93; }
            QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { background: #F2EEE7; color: #8796A6; }
            QSpinBox, QDoubleSpinBox { padding-right: 22px; }
            QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; width: 19px; background: #F3EFE7; border-left: 1px solid #D9D1C5; border-top-right-radius: 5px; }
            QSpinBox::down-button, QDoubleSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; width: 19px; background: #F3EFE7; border-left: 1px solid #D9D1C5; border-bottom-right-radius: 5px; }
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url("__ARROW_UP__"); width: 10px; height: 6px; }
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url("__ARROW_DOWN__"); width: 10px; height: 6px; }
            QPushButton { border: 1px solid #D9D1C5; border-radius: 7px; padding: 6px 11px; font-weight: 700; background: #F3EFE7; color: #30435B; }
            QPushButton:hover { background: #EAE3D8; }
            QPushButton:disabled { background: #F1EEE8; color: #9CACBB; }
            QPushButton#startButton { background: #0D9D93; border-color: #0D9D93; color: white; min-width: 145px; }
            QPushButton#startButton:hover { background: #0B827A; }
            QPushButton#stopButton { background: #C7586A; border-color: #C7586A; color: white; min-width: 105px; }
            QPushButton#stopButton:hover { background: #AC4557; }
            QPushButton[secondary="true"] { background: #FAF7F1; border: 1px solid #D9D1C5; }
            QProgressBar { background: #F1ECE3; border: 1px solid #DDD5CA; border-radius: 6px; text-align: center; color: #30435B; min-height: 16px; }
            QProgressBar::chunk { background: #49BAAF; border-radius: 5px; }
            QCheckBox { spacing: 8px; }
            QCheckBox#cellToggle { color: #526378; font-weight: 800; spacing: 7px; }
            QCheckBox#cellToggle::indicator { width: 34px; height: 17px; border-radius: 9px; border: 1px solid #B8B0A5; background: #D8D2C8; }
            QCheckBox#cellToggle::indicator:checked { border-color: #0D9D93; background: #0D9D93; }
            QCheckBox#cellToggle::indicator:disabled { border-color: #D5CEC4; background: #E7E2DA; }
            """.replace("__ARROW_UP__", arrow_up).replace("__ARROW_DOWN__", arrow_down)
        )

    def _simulation_changed(self, checked: bool) -> None:
        self.host.setEnabled(not checked)
        self.port.setEnabled(not checked)
        self.test_button.setEnabled(not checked)
        self.mode_badge.setText("SIMULATION" if checked else "LIVE INSTRUMENT")
        self.mode_badge.setStyleSheet(
            "background: #DDF6F0; color: #075E57;" if checked else "background: #FFF0D9; color: #785318;"
        )
        self.status_label.setText(
            "Ready - simulation mode is enabled"
            if checked
            else "Ready - review wiring and polarity before starting"
        )

    def _cell_plan(self, name: str, controls: CellControls) -> CellPlan:
        drain_channel, gate_channel = ((1, 2) if name == "A" else (3, 4))
        enabled = controls.active.isChecked()
        output_text = controls.output.text().strip()
        if not output_text and enabled:
            raise ValueError(f"OECT {name}: raw-data filename is required")
        if not output_text:
            output_text = controls.default_output_name
        output_path = Path(output_text).expanduser()
        if not output_path.is_absolute():
            output_path = controls.default_output_dir / output_path
        return CellPlan(
            name=name,
            drain_channel=drain_channel,
            gate_channel=gate_channel,
            vds_v=controls.vds.value(),
            vg_initial_v=controls.vg_initial.value(),
            vg_final_v=controls.vg_final.value(),
            vg_step_v=controls.vg_step.value(),
            hold_s=controls.hold.value(),
            cycles=controls.cycles.value(),
            mode=controls.mode.currentData(),
            keep_s=controls.keep.value(),
            delay_s=controls.delay_ms.value() / 1000.0,
            interval_s=controls.interval.value(),
            ids_limit_a=DEFAULT_CURRENT_LIMIT_A,
            ig_limit_a=DEFAULT_CURRENT_LIMIT_A,
            output_path=output_path.resolve(),
            enabled=enabled,
        )

    def _build_plan(self) -> ExperimentPlan:
        plan = ExperimentPlan(
            host=self.host.text().strip(),
            port=self.port.value(),
            simulation=self.simulation.isChecked(),
            cell_a=self._cell_plan("A", self.controls_a),
            cell_b=self._cell_plan("B", self.controls_b),
        )
        for cell in plan.active_cells:
            _ = cell.levels
        _ = plan.hardware_interval_s
        if (
            plan.cell_a.enabled
            and plan.cell_b.enabled
            and plan.cell_a.output_path == plan.cell_b.output_path
        ):
            raise ValueError("OECT A and B must use different output files")
        if not plan.host and not plan.simulation:
            raise ValueError("Instrument IP address is required")
        for cell in plan.active_cells:
            if cell.hold_s <= 0 or cell.interval_s <= 0:
                raise ValueError(f"OECT {cell.name}: hold and interval must be positive")
            if cell.delay_s >= cell.hold_s:
                raise ValueError(f"OECT {cell.name}: delay must be shorter than the hold period")
            if cell.ids_limit_a <= 0 or cell.ig_limit_a <= 0:
                raise ValueError(f"OECT {cell.name}: current limits must be positive")
        return plan

    def _start(self) -> None:
        try:
            plan = self._build_plan()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid settings", str(exc))
            return

        existing = [cell.output_path for cell in plan.active_cells if cell.output_path.exists()]
        if existing:
            answer = QtWidgets.QMessageBox.question(
                self,
                "Replace existing output?",
                "The following output file(s) already exist and will be replaced:\n\n"
                + "\n".join(str(path) for path in existing),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return

        for cell in ("A", "B"):
            for values in self.plot_data[cell].values():
                values.clear()
        for curve in self.curves.values():
            curve.setData([], [])
        for cell, controls in (
            (plan.cell_a, self.controls_a),
            (plan.cell_b, self.controls_b),
        ):
            controls.progress.setValue(0)
            controls.detail.setText("Ready" if cell.enabled else "OFF | held at 0 V | no data file")

        self.worker = AcquisitionWorker(plan)
        self.worker.sample_ready.connect(self._sample)
        self.worker.progress_ready.connect(self._progress)
        self.worker.status_ready.connect(self._status)
        self.worker.completed.connect(self._completed)
        self.worker.finished.connect(self._worker_finished)
        self._set_running(True)
        durations = " • ".join(
            f"{cell.name} ≈ {cell.estimated_duration_s:g} s" if cell.enabled else f"{cell.name} OFF"
            for cell in (plan.cell_a, plan.cell_b)
        )
        self.status_label.setText(
            f"Starting • hardware base interval {plan.hardware_interval_s:g} s • {durations}"
        )
        self.worker.start()

    def _stop(self) -> None:
        if self.worker is not None:
            self.stop_button.setEnabled(False)
            self.status_label.setText("Stopping safely after the current instrument transaction...")
            self.worker.request_stop()

    @QtCore.pyqtSlot(str, float, float, float, float, float)
    def _sample(
        self,
        cell: str,
        elapsed_s: float,
        _gate_i: float,
        gate_v: float,
        drain_i: float,
        _drain_v: float,
    ) -> None:
        data = self.plot_data[cell]
        data["t"].append(elapsed_s)
        data["id"].append(drain_i)
        data["vg"].append(gate_v)
        if cell == "A":
            self.curves["id_a"].setData(data["t"], data["id"])
            self.curves["vg_a"].setData(data["t"], data["vg"], stepMode="right")
        else:
            self.curves["id_b"].setData(data["t"], data["id"])
            self.curves["vg_b"].setData(data["t"], data["vg"], stepMode="right")
        self.summary_label.setText(
            f"A: {len(self.plot_data['A']['t'])} samples   "
            f"B: {len(self.plot_data['B']['t'])} samples"
        )

    @QtCore.pyqtSlot(str, int, str)
    def _progress(self, cell: str, percent: int, detail: str) -> None:
        controls = self.controls_a if cell == "A" else self.controls_b
        controls.progress.setValue(percent)
        controls.detail.setText(detail)

    @QtCore.pyqtSlot(str)
    def _status(self, text: str) -> None:
        self.status_label.setText(text)

    @QtCore.pyqtSlot(bool, str)
    def _completed(self, success: bool, message: str) -> None:
        self._set_running(False)
        self.status_label.setText(message)
        self.status_dot.setStyleSheet("color: #0D9D93;" if success else "color: #C7586A;")
        if not success and "Stopped safely" not in message:
            QtWidgets.QMessageBox.critical(self, "Measurement ended", message)

    @QtCore.pyqtSlot()
    def _worker_finished(self) -> None:
        self.worker = None

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.simulation.setEnabled(not running)
        self.host.setEnabled(not running and not self.simulation.isChecked())
        self.port.setEnabled(not running and not self.simulation.isChecked())
        self.test_button.setEnabled(not running and not self.simulation.isChecked())
        self.controls_a.set_enabled(not running)
        self.controls_b.set_enabled(not running)

    def _test_connection(self) -> None:
        self.test_button.setEnabled(False)
        self.status_label.setText("Testing read-only *IDN? connection...")
        self.connection_worker = ConnectionTestWorker(self.host.text().strip(), self.port.value())
        self.connection_worker.finished_test.connect(self._connection_result)
        self.connection_worker.finished.connect(self._connection_worker_finished)
        self.connection_worker.start()

    @QtCore.pyqtSlot(bool, str)
    def _connection_result(self, success: bool, message: str) -> None:
        self.test_button.setEnabled(True)
        self.status_label.setText(("Connected: " if success else "Connection failed: ") + message)
        if not success:
            QtWidgets.QMessageBox.warning(self, "Connection test", message)

    @QtCore.pyqtSlot()
    def _connection_worker_finished(self) -> None:
        self.connection_worker = None

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if self.worker is not None and self.worker.isRunning():
            answer = QtWidgets.QMessageBox.question(
                self,
                "Measurement is running",
                "Stop safely and close?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
            self.worker.request_stop()
            if not self.worker.wait(10_000):
                self.status_label.setText("Instrument is still stopping; please wait before closing")
                event.ignore()
                return
        event.accept()


def make_screenshot(window: MainWindow, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap = window.grab()
    if not pixmap.save(str(path)):
        raise RuntimeError(f"could not save screenshot to {path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path, help="render the GUI to a PNG and exit")
    args = parser.parse_args(argv)
    if args.screenshot:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pg.setConfigOptions(antialias=True)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName(APP_TITLE)
    window = MainWindow()
    window.show()
    if args.screenshot:
        QtWidgets.QApplication.processEvents()
        make_screenshot(window, args.screenshot.resolve())
        return 0
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
