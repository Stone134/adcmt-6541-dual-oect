#!/usr/bin/env python3
"""Single-OECT CH1/CH2 front panel for the ADCMT 6541."""

from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PyQt5 import QtCore, QtWidgets
import pyqtgraph as pg

from adcmt_6541_sync import ADCMT6541, ADCMTError, LineSocket, format_number
from oect_dual_gui import (
    DEFAULT_CURRENT_LIMIT_A,
    CellPlan,
    CellRuntime,
    CellControls,
    CompactAxisItem,
    ConnectionTestWorker,
    LegacyTextWriter,
    MainWindow,
    make_screenshot,
)


SINGLE_APP_TITLE = "ADCMT 6541 - Single OECT Measurement (CH1/CH2)"


@dataclass(frozen=True)
class SingleExperimentPlan:
    host: str
    port: int
    simulation: bool
    cell: CellPlan

    @property
    def hardware_interval_s(self) -> float:
        interval_ms = int(round(self.cell.interval_s * 1000.0))
        if interval_ms < 100:
            raise ValueError("Measurement interval must be at least 0.100 s")
        return interval_ms / 1000.0


class SingleOECTInstrument:
    """Operate CH1 as drain/master and CH2 as gate/synchronized slave."""

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

    def configure(self, plan: SingleExperimentPlan) -> None:
        inst = self._inst()
        # Park every output first so old settings from a previous four-channel
        # session cannot accidentally operate CH3 or CH4.
        inst.set_all_standby_direct()
        for channel in (3, 4):
            inst.select(channel)
            for command in ("OP5", "CP7", "TG0", "M1"):
                inst.write(command)

        base_ms = plan.hardware_interval_s * 1000.0
        timing_adjust_ms = 1.0 if base_ms <= 6000.0 else 2.0
        slave_ms = base_ms - timing_adjust_ms
        if slave_ms < 0.5:
            raise ADCMTError("hardware synchronization requires an interval >= 0.100 s")

        channel_values = {
            1: (plan.cell.vds_v, plan.cell.ids_limit_a),
            2: (plan.cell.vg_initial_v, plan.cell.ig_limit_a),
        }
        inst.write("DL1")
        inst.write("OH0")
        for channel in (1, 2):
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
                "R1",
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
            if channel == 1:
                commands = (
                    f"SP0,0.055,{format_number(base_ms)}",
                    "SD0.035",
                    "OP3",
                    "CP6",
                    "TG0",
                    "M1",
                )
            else:
                commands = (
                    f"SP0,0.050,{format_number(slave_ms)}",
                    "SD0.030",
                    "OP1",
                    "CP7",
                    "TG1",
                    "M1",
                )
            for command in commands:
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

    def set_gate(self, voltage_v: float) -> None:
        self.set_channel_voltage(2, voltage_v)

    def zero_sources(self) -> None:
        self.set_channel_voltage(1, 0.0)
        self.set_channel_voltage(2, 0.0)

    def memory_count(self, channel: int) -> int:
        return self._inst().memory_count(channel)

    def fetch_channels(self, start: int, stop: int) -> tuple[list[float], list[float]]:
        inst = self._inst()
        return inst.fetch_memory(1, start, stop), inst.fetch_memory(2, start, stop)

    def rollover_memory(self) -> None:
        inst = self._inst()
        inst.select(1)
        inst.write("M1")
        for channel in (1, 2):
            inst.select(channel)
            for command in ("ST0", "RL", "RNM10000", "ST1"):
                inst.write(command)
        inst.select(1)
        inst.write("M0")

    def standby_and_close(self) -> None:
        if self.instrument is not None:
            try:
                self.instrument.set_master_standby()
            finally:
                self.transport.close()
                self.instrument = None


class SingleAcquisitionWorker(QtCore.QThread):
    sample_ready = QtCore.pyqtSignal(str, float, float, float, float, float)
    progress_ready = QtCore.pyqtSignal(str, int, str)
    status_ready = QtCore.pyqtSignal(str)
    completed = QtCore.pyqtSignal(bool, str)

    def __init__(self, plan: SingleExperimentPlan) -> None:
        super().__init__()
        self.plan = plan
        self._stop_event = threading.Event()
        self._instrument: SingleOECTInstrument | None = None

    def request_stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        writer: LegacyTextWriter | None = None
        success = False
        message = "Measurement ended"
        try:
            writer = LegacyTextWriter(self.plan.cell.output_path)
            state = CellRuntime(self.plan.cell, writer)
            state.start_stage(0.0)
            if self.plan.simulation:
                self._run_simulation(state)
            else:
                self._run_instrument(state)
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
            if writer is not None:
                writer.close()
            self.completed.emit(success, message)

    def _run_simulation(self, state: CellRuntime) -> None:
        interval_s = self.plan.hardware_interval_s
        run_started = time.monotonic()
        next_tick = run_started + interval_s
        self.status_ready.emit(f"Simulation running - CH1/CH2 interval {interval_s:g} s")
        while not self._stop_event.is_set() and not state.done:
            now = time.monotonic()
            self._advance_if_due(state, now - run_started, None)
            remaining = next_tick - now
            if remaining > 0:
                time.sleep(min(remaining, 0.05))
                continue
            elapsed_s = next_tick - run_started
            age = max(0.0, elapsed_s - state.stage_started_elapsed_s)
            response = 1.0 - math.exp(-age / 8.0)
            drain_i = state.plan.vds_v * (1e-6 + abs(state.gate_v) * 5e-4) * response
            drain_i += 2e-9 * math.sin(elapsed_s * 0.7)
            gate_i = -state.gate_v * 2e-8 + 4e-10 * math.cos(elapsed_s * 0.3)
            self._process_sample(state, elapsed_s, drain_i, gate_i)
            next_tick += interval_s

    def _run_instrument(self, state: CellRuntime) -> None:
        instrument = SingleOECTInstrument(self.plan.host, self.plan.port)
        self._instrument = instrument
        identity = instrument.connect()
        self.status_ready.emit(f"Connected: {identity}")
        instrument.configure(self.plan)
        run_started = time.monotonic()
        instrument.operate_and_start()
        epoch_started_s = time.monotonic() - run_started
        next_address = 0
        interval_s = self.plan.hardware_interval_s
        self.status_ready.emit(f"CH1/CH2 synchronized at {interval_s:g} s")

        while not self._stop_event.is_set() and not state.done:
            elapsed_s = time.monotonic() - run_started
            if elapsed_s >= state.stage_ends_elapsed_s:
                next_address = self._fetch_available(
                    state, instrument, next_address, epoch_started_s, interval_s
                )
                self._advance_if_due(state, time.monotonic() - run_started, instrument)
            master_count = instrument.memory_count(1)
            if master_count > next_address:
                next_address = self._fetch_available(
                    state, instrument, next_address, epoch_started_s, interval_s
                )
            if master_count >= 9000:
                next_address = self._fetch_available(
                    state, instrument, next_address, epoch_started_s, interval_s
                )
                instrument.rollover_memory()
                epoch_started_s = time.monotonic() - run_started
                next_address = 0
            time.sleep(min(0.05, interval_s / 4.0))

        self._fetch_available(state, instrument, next_address, epoch_started_s, interval_s)

    def _fetch_available(
        self,
        state: CellRuntime,
        instrument: SingleOECTInstrument,
        next_address: int,
        epoch_started_s: float,
        interval_s: float,
    ) -> int:
        common_count = min(instrument.memory_count(1), instrument.memory_count(2))
        if common_count <= next_address:
            return next_address
        drain_values, gate_values = instrument.fetch_channels(next_address, common_count - 1)
        for offset, address in enumerate(range(next_address, common_count)):
            elapsed_s = epoch_started_s + (address + 1) * interval_s
            self._process_sample(state, elapsed_s, drain_values[offset], gate_values[offset])
        return common_count

    def _advance_if_due(
        self,
        state: CellRuntime,
        elapsed_s: float,
        instrument: SingleOECTInstrument | None,
    ) -> None:
        if state.done or elapsed_s < state.stage_ends_elapsed_s:
            return
        if state.is_final_level:
            if instrument is not None:
                instrument.zero_sources()
            state.advance(elapsed_s)
            self.status_ready.emit("OECT: schedule complete; Vds and Vg set to 0 V")
            self.progress_ready.emit(
                "A", 100, f"complete | outputs 0 V | {state.samples_written} samples"
            )
            return
        state.advance(elapsed_s)
        if instrument is not None:
            instrument.set_gate(state.gate_v)
        self.status_ready.emit(
            f"OECT: Vg -> {state.gate_v:g} V; waiting {state.plan.delay_s:g} s before logging"
        )

    def _process_sample(
        self,
        state: CellRuntime,
        elapsed_s: float,
        drain_i: float,
        gate_i: float,
    ) -> None:
        if state.done or not state.should_record(elapsed_s):
            return
        if state.writer is None:
            raise RuntimeError("enabled OECT has no data writer")
        state.writer.write(elapsed_s, gate_i, state.gate_v, drain_i, state.plan.vds_v)
        state.samples_written += 1
        self.sample_ready.emit(
            "A", elapsed_s, gate_i, state.gate_v, drain_i, state.plan.vds_v
        )
        phase = "delay" if elapsed_s < state.measure_started_elapsed_s else "measuring"
        detail = (
            f"{phase} | level {state.level_index + 1}/{len(state.plan.levels)} | "
            f"Vg {state.gate_v:g} V | {state.samples_written} samples"
        )
        self.progress_ready.emit("A", state.progress_percent(elapsed_s), detail)


class SingleOECTWindow(MainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(SINGLE_APP_TITLE)
        self.resize(1180, 800)
        self.summary_label.setText("0 samples")

    def _header(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setObjectName("headerCard")
        layout = QtWidgets.QHBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        title_box = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel("Single OECT measurement")
        title.setObjectName("title")
        subtitle = QtWidgets.QLabel("ADCMT 6541 • CH1 drain/master • CH2 gate/synchronized")
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
        compliance = QtWidgets.QLabel("CH1–2 limit: 0.3 A each")
        compliance.setProperty("muted", True)
        grid.addWidget(self.simulation, 0, 0, 1, 3)
        grid.addWidget(compliance, 0, 3, 1, 3, QtCore.Qt.AlignRight)
        grid.addWidget(QtWidgets.QLabel("Instrument IP"), 1, 0)
        grid.addWidget(self.host, 1, 1)
        grid.addWidget(QtWidgets.QLabel("Port"), 1, 2)
        grid.addWidget(self.port, 1, 3)
        grid.addWidget(self.test_button, 1, 4, 1, 2)
        layout.addWidget(connection)

        output_root = Path(__file__).resolve().parent.parent
        self.controls_a = CellControls("", "cyan", output_root / "measurement=single.txt")
        self.controls_a.group.setTitle("OECT • CH1 drain / CH2 gate")
        self.controls_a.switch_row.hide()
        layout.addWidget(self.controls_a.group)
        layout.addStretch(1)
        return panel

    def _plot_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        self.plots: dict[str, pg.PlotWidget] = {}
        self.curves: dict[str, pg.PlotDataItem] = {}
        for row, (key, title, ylabel, units, color) in enumerate((
            ("id_a", "OECT • Drain current (CH1)", "Id", "A", "#078A84"),
            ("vg_a", "OECT • Gate voltage (CH2)", "Vg", "V", "#1CA89B"),
        )):
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
            self.plots[key] = plot
            self.curves[key] = plot.plot(pen=pg.mkPen(color, width=2), connect="finite")
            grid.addWidget(plot, row, 0)
        return panel

    def _build_plan(self) -> SingleExperimentPlan:
        cell = self._cell_plan("A", self.controls_a)
        _ = cell.levels
        plan = SingleExperimentPlan(
            host=self.host.text().strip(),
            port=self.port.value(),
            simulation=self.simulation.isChecked(),
            cell=cell,
        )
        _ = plan.hardware_interval_s
        if not plan.host and not plan.simulation:
            raise ValueError("Instrument IP address is required")
        if cell.hold_s <= 0 or cell.interval_s <= 0:
            raise ValueError("Hold and interval must be positive")
        if cell.delay_s >= cell.hold_s:
            raise ValueError("Delay must be shorter than the hold period")
        return plan

    def _start(self) -> None:
        try:
            plan = self._build_plan()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid settings", str(exc))
            return
        if plan.cell.output_path.exists():
            answer = QtWidgets.QMessageBox.question(
                self,
                "Replace existing output?",
                f"This output file already exists and will be replaced:\n\n{plan.cell.output_path}",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return
        if not plan.simulation:
            answer = QtWidgets.QMessageBox.warning(
                self,
                "Confirm 0.3 A current limits",
                "CH1 and CH2 current limits are fixed at 0.3 A. "
                "That may be unsafe for an OECT. Start the live measurement anyway?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return

        for values in self.plot_data["A"].values():
            values.clear()
        for curve in self.curves.values():
            curve.setData([], [])
        self.controls_a.progress.setValue(0)
        self.controls_a.detail.setText("Ready")
        self.worker = SingleAcquisitionWorker(plan)
        self.worker.sample_ready.connect(self._sample)
        self.worker.progress_ready.connect(self._progress)
        self.worker.status_ready.connect(self._status)
        self.worker.completed.connect(self._completed)
        self.worker.finished.connect(self._worker_finished)
        self._set_running(True)
        self.status_label.setText(
            f"Starting • CH1/CH2 interval {plan.hardware_interval_s:g} s • "
            f"duration ≈ {plan.cell.estimated_duration_s:g} s"
        )
        self.worker.start()

    @QtCore.pyqtSlot(str, float, float, float, float, float)
    def _sample(
        self,
        _cell: str,
        elapsed_s: float,
        _gate_i: float,
        gate_v: float,
        drain_i: float,
        _drain_v: float,
    ) -> None:
        data = self.plot_data["A"]
        data["t"].append(elapsed_s)
        data["id"].append(drain_i)
        data["vg"].append(gate_v)
        self.curves["id_a"].setData(data["t"], data["id"])
        self.curves["vg_a"].setData(data["t"], data["vg"], stepMode="right")
        self.summary_label.setText(f"{len(data['t'])} samples")

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.simulation.setEnabled(not running)
        self.host.setEnabled(not running and not self.simulation.isChecked())
        self.port.setEnabled(not running and not self.simulation.isChecked())
        self.test_button.setEnabled(not running and not self.simulation.isChecked())
        self.controls_a.set_enabled(not running)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path, help="render the GUI to a PNG and exit")
    args = parser.parse_args(argv)
    if args.screenshot:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pg.setConfigOptions(antialias=True)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName(SINGLE_APP_TITLE)
    window = SingleOECTWindow()
    window.show()
    if args.screenshot:
        QtWidgets.QApplication.processEvents()
        make_screenshot(window, args.screenshot.resolve())
        return 0
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
