"""Offline tests for GUI scheduling and exact legacy text formatting."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtGui, QtWidgets

from oect_dual_gui import (
    AcquisitionWorker,
    CellPlan,
    CellRuntime,
    DualOECTInstrument,
    ExperimentPlan,
    LEGACY_HEADER,
    LegacyTextWriter,
    MainWindow,
    common_hardware_interval,
    labview_scientific,
    voltage_levels,
)


class GuiSyntheticTests(unittest.TestCase):
    def test_labview_scientific_format(self) -> None:
        self.assertEqual(labview_scientific(1.038), "1.0380000E+0")
        self.assertEqual(labview_scientific(-1.324e-8), "-1.3240000E-8")
        self.assertEqual(labview_scientific(-0.01), "-1.0000000E-2")

    def test_legacy_file_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic=lvl1.txt"
            writer = LegacyTextWriter(path)
            writer.write(1.038, -1.324e-8, -0.1, 2.14e-9, -0.01)
            writer.close()
            self.assertEqual(
                path.read_bytes(),
                (
                    LEGACY_HEADER
                    + "1.0380000E+0\t-1.3240000E-8\t-1.0000000E-1\t"
                    + "2.1400000E-9\t-1.0000000E-2\n"
                ).encode("ascii"),
            )

    def test_decreasing_and_nondivisible_voltage_levels(self) -> None:
        self.assertEqual(voltage_levels(0.0, -0.8, -0.1), tuple(-0.1 * i for i in range(9)))
        self.assertEqual(voltage_levels(0.0, -0.25, -0.1), (0.0, -0.1, -0.2, -0.25))

    def test_independent_intervals_share_hardware_clock(self) -> None:
        self.assertEqual(common_hardware_interval(1.0, 2.0), 1.0)
        self.assertEqual(common_hardware_interval(1.5, 2.0), 0.5)

    def test_disabled_cell_is_held_at_zero_and_gets_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def cell(name: str, enabled: bool, interval: float) -> CellPlan:
                return CellPlan(
                    name=name,
                    drain_channel=1 if name == "A" else 3,
                    gate_channel=2 if name == "A" else 4,
                    vds_v=-0.01,
                    vg_initial_v=-0.1,
                    vg_final_v=-0.1,
                    vg_step_v=-0.1,
                    hold_s=0.2,
                    keep_s=0.0,
                    delay_s=0.0,
                    interval_s=interval,
                    ids_limit_a=0.3,
                    ig_limit_a=0.3,
                    output_path=root / f"{name}.txt",
                    enabled=enabled,
                )

            plan = ExperimentPlan(
                host="127.0.0.1",
                port=5025,
                simulation=True,
                cell_a=cell("A", False, 1.0),
                cell_b=cell("B", True, 0.1),
            )
            self.assertEqual(plan.hardware_interval_s, 0.1)
            progress: list[tuple[str, int, str]] = []
            worker = AcquisitionWorker(plan)
            worker.progress_ready.connect(
                lambda name, percent, detail: progress.append((name, percent, detail))
            )
            worker.run()
            self.assertFalse(plan.cell_a.output_path.exists())
            self.assertTrue(plan.cell_b.output_path.exists())
            self.assertGreater(len(plan.cell_b.output_path.read_text().splitlines()), 1)
            self.assertIn(("A", 0, "OFF | held at 0 V | no data file"), progress)
            self.assertTrue(any(name == "B" and percent == 100 for name, percent, _ in progress))

    def test_output_interval_starts_at_first_available_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.txt"
            plan = CellPlan(
                name="A", drain_channel=1, gate_channel=2, vds_v=-0.01,
                vg_initial_v=-0.1, vg_final_v=-0.1, vg_step_v=-0.1,
                hold_s=1.0, keep_s=0.0, delay_s=0.0, interval_s=0.2,
                ids_limit_a=0.03, ig_limit_a=0.003, output_path=path,
            )
            writer = LegacyTextWriter(path)
            try:
                state = CellRuntime(plan, writer)
                state.start_stage(0.0)
                accepted = [t for t in (0.1, 0.2, 0.3, 0.4, 0.5) if state.should_record(t)]
                self.assertEqual(accepted, [0.1, 0.3, 0.5])
                self.assertEqual(state.progress_percent(0.999), 99)
                state.advance(1.0)
                self.assertEqual(state.progress_percent(1.0), 100)
            finally:
                writer.close()

    def test_gui_instrument_configuration_keeps_independent_voltage_values(self) -> None:
        class FakeInstrument:
            def __init__(self) -> None:
                self.events: list[str] = []

            def set_all_standby_direct(self) -> None:
                self.events.append("standby_all")

            def select(self, channel: int) -> None:
                self.events.append(f"SCH{channel}")

            def write(self, command: str) -> None:
                self.events.append(command)

            def query(self, command: str) -> str:
                self.events.append(command)
                return "R0"

        with tempfile.TemporaryDirectory() as directory:
            def cell(
                name: str,
                vds: float,
                vg: float,
                interval: float,
                enabled: bool = True,
            ) -> CellPlan:
                return CellPlan(
                    name=name,
                    drain_channel=1 if name == "A" else 3,
                    gate_channel=2 if name == "A" else 4,
                    vds_v=vds, vg_initial_v=vg, vg_final_v=vg - 0.1,
                    vg_step_v=-0.1, hold_s=60.0, keep_s=0.0,
                    delay_s=1.0, interval_s=interval,
                    ids_limit_a=0.03, ig_limit_a=0.003,
                    output_path=Path(directory) / f"{name}.txt",
                    enabled=enabled,
                )

            plan = ExperimentPlan(
                host="127.0.0.1", port=5025, simulation=False,
                cell_a=cell("A", -0.01, -0.1, 1.0),
                cell_b=cell("B", -0.02, -0.3, 2.0),
            )
            driver = DualOECTInstrument(plan.host, plan.port)
            fake = FakeInstrument()
            driver.instrument = fake  # type: ignore[assignment]
            driver.configure(plan)
            events = fake.events
            self.assertEqual(events.count("CP6"), 1)
            self.assertEqual(events.count("TG1"), 3)
            self.assertEqual(events.count("SP0,0.050,999"), 3)
            self.assertEqual(events.count("R0"), 4)
            self.assertEqual(events.count("RD0"), 4)
            self.assertEqual(events.count("R?"), 4)
            self.assertNotIn("R1", events)
            self.assertIn("SOV-0.01", events)
            self.assertIn("SOV-0.02", events)
            self.assertIn("SOV-0.1", events)
            self.assertIn("SOV-0.3", events)

            fake.events.clear()
            b_only = ExperimentPlan(
                host="127.0.0.1", port=5025, simulation=False,
                cell_a=cell("A", -0.01, -0.1, 1.0, enabled=False),
                cell_b=cell("B", -0.02, -0.3, 2.0),
            )
            driver.configure(b_only)
            source_by_channel: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: []}
            selected = 0
            for event in fake.events:
                if event.startswith("SCH"):
                    selected = int(event[3:])
                elif event.startswith("SOV"):
                    source_by_channel[selected].append(event)
            self.assertEqual(source_by_channel[1], ["SOV0"])
            self.assertEqual(source_by_channel[2], ["SOV0"])
            self.assertEqual(source_by_channel[3], ["SOV-0.02"])
            self.assertEqual(source_by_channel[4], ["SOV-0.3"])

    def test_completed_cell_sets_both_sources_to_zero_without_stopping_master(self) -> None:
        class FakeInstrument:
            def __init__(self) -> None:
                self.events: list[str] = []
                self.channel = 1

            def select(self, channel: int) -> None:
                self.channel = channel
                self.events.append(f"SCH{channel}")

            def write(self, command: str) -> None:
                self.events.append(command)

            def query(self, command: str) -> str:
                self.events.append(command)
                return "SOV+0.0000E+0"

        driver = DualOECTInstrument("127.0.0.1", 5025)
        fake = FakeInstrument()
        driver.instrument = fake  # type: ignore[assignment]
        driver.zero_cell(1, 2)
        self.assertEqual(
            fake.events,
            ["SCH1", "SOV0", "SOV?", "SCH2", "SOV0", "SOV?"],
        )
        self.assertNotIn("SBY", fake.events)
        self.assertNotIn("M1", fake.events)

    def test_shorter_cell_stops_logging_and_reaches_100_percent(self) -> None:
        class FakeInstrument:
            def __init__(self) -> None:
                self.zeroed: list[tuple[int, int]] = []

            def zero_cell(self, drain_channel: int, gate_channel: int) -> None:
                self.zeroed.append((drain_channel, gate_channel))

        with tempfile.TemporaryDirectory() as directory:
            def cell(name: str, duration: float) -> CellPlan:
                return CellPlan(
                    name=name,
                    drain_channel=1 if name == "A" else 3,
                    gate_channel=2 if name == "A" else 4,
                    vds_v=-0.01,
                    vg_initial_v=-0.1,
                    vg_final_v=-0.1,
                    vg_step_v=-0.1,
                    hold_s=duration,
                    keep_s=0.0,
                    delay_s=0.0,
                    interval_s=0.1,
                    ids_limit_a=0.3,
                    ig_limit_a=0.3,
                    output_path=Path(directory) / f"{name}.txt",
                )

            for shorter_name in ("A", "B"):
                with self.subTest(shorter_name=shorter_name):
                    plan = ExperimentPlan(
                        host="127.0.0.1", port=5025, simulation=True,
                        cell_a=cell("A", 1.0 if shorter_name == "A" else 3.0),
                        cell_b=cell("B", 1.0 if shorter_name == "B" else 3.0),
                    )
                    writers = (
                        LegacyTextWriter(plan.cell_a.output_path),
                        LegacyTextWriter(plan.cell_b.output_path),
                    )
                    try:
                        states = (
                            CellRuntime(plan.cell_a, writers[0]),
                            CellRuntime(plan.cell_b, writers[1]),
                        )
                        for state in states:
                            state.start_stage(0.0)
                        worker = AcquisitionWorker(plan)
                        progress: list[tuple[str, int, str]] = []
                        samples: list[str] = []
                        worker.progress_ready.connect(
                            lambda name, percent, detail: progress.append((name, percent, detail))
                        )
                        worker.sample_ready.connect(
                            lambda name, *_values: samples.append(name)
                        )
                        fake = FakeInstrument()
                        worker._advance_due_stages(states, 1.01, fake)  # type: ignore[arg-type]
                        self.assertEqual(
                            fake.zeroed,
                            [(1, 2)] if shorter_name == "A" else [(3, 4)],
                        )
                        self.assertTrue(next(state for state in states if state.plan.name == shorter_name).done)
                        self.assertFalse(next(state for state in states if state.plan.name != shorter_name).done)
                        self.assertIn(
                            (shorter_name, 100, "complete | outputs 0 V | 0 samples"),
                            progress,
                        )
                        worker._process_sample(states, 1.1, (1e-6, 2e-9, 3e-6, 4e-9))
                        self.assertEqual(samples, ["B" if shorter_name == "A" else "A"])
                        self.assertEqual(
                            [event for event in progress if event[0] == shorter_name],
                            [(shorter_name, 100, "complete | outputs 0 V | 0 samples")],
                        )
                        self.assertEqual(
                            plan.cell_a.output_path.read_text().count("\n"),
                            1 if shorter_name == "A" else 2,
                        )
                        self.assertEqual(
                            plan.cell_b.output_path.read_text().count("\n"),
                            1 if shorter_name == "B" else 2,
                        )
                        simulated = worker._synthetic_values(states, 1.1)
                        self.assertEqual(
                            simulated[:2] if shorter_name == "A" else simulated[2:],
                            (0.0, 0.0),
                        )
                        worker._advance_due_stages(states, 3.01, fake)  # type: ignore[arg-type]
                        self.assertTrue(all(state.done for state in states))
                        self.assertEqual(fake.zeroed, [(1, 2), (3, 4)] if shorter_name == "A" else [(3, 4), (1, 2)])
                        self.assertEqual([p for _, p, _ in progress if p == 100], [100, 100])
                    finally:
                        for writer in writers:
                            writer.close()


class GuiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow()
        self.window.resize(1280, 800)
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()

    def test_compact_controls_and_fixed_current_limits(self) -> None:
        self.assertEqual(self.window.controls_a.vg_final.text(), "-0.8 V")
        self.assertEqual(self.window.controls_a.keep.text(), "3600 s")
        self.assertEqual(self.window.controls_b.delay_ms.text(), "1000 ms")
        plan = self.window._build_plan()
        self.assertEqual(plan.cell_a.output_path, Path(__file__).resolve().parent.parent / "measurement=lvl1.txt")
        self.assertEqual(plan.cell_b.output_path, Path(__file__).resolve().parent.parent / "measurement=lvl2.txt")
        for cell in (plan.cell_a, plan.cell_b):
            self.assertEqual(cell.ids_limit_a, 0.3)
            self.assertEqual(cell.ig_limit_a, 0.3)
        self.assertFalse(hasattr(self.window.controls_a, "ids_limit"))
        self.assertNotIn("Confirm 0.3 A", self.window.status_label.text())

    def test_each_oect_has_an_independent_on_off_switch(self) -> None:
        self.window.controls_a.active.setChecked(False)
        self.window.controls_a.output.clear()
        self.window.controls_b.interval.setValue(2.0)
        plan = self.window._build_plan()
        self.assertFalse(plan.cell_a.enabled)
        self.assertTrue(plan.cell_b.enabled)
        self.assertEqual(plan.hardware_interval_s, 2.0)
        self.assertFalse(self.window.controls_a.vds.isEnabled())
        self.assertFalse(self.window.controls_a.output.isEnabled())
        self.assertEqual(
            self.window.controls_a.detail.text(),
            "OFF | held at 0 V | no data file",
        )
        self.window.controls_b.active.setChecked(False)
        with self.assertRaisesRegex(ValueError, "Enable at least one OECT"):
            self.window._build_plan()

    def test_spinbox_arrows_are_dark_and_renderable(self) -> None:
        style = self.window.styleSheet()
        assets = Path(__file__).resolve().parent / "assets"
        for name in ("spin_up.svg", "spin_down.svg"):
            path = assets / name
            self.assertIn(str(path), style)
            pixmap = QtGui.QPixmap(str(path))
            self.assertFalse(pixmap.isNull())
            color = pixmap.toImage().pixelColor(5, 3)
            self.assertLess(max(color.red(), color.green(), color.blue()), 40)

    def test_both_cells_share_a_page_without_scrolling(self) -> None:
        self.assertEqual(self.window.findChildren(QtWidgets.QScrollArea), [])
        a = self.window.controls_a.group
        b = self.window.controls_b.group
        self.assertTrue(a.isVisible())
        self.assertTrue(b.isVisible())
        self.assertEqual(a.x(), b.x())
        self.assertLess(a.y(), b.y())
        plot = self.window.plots["id_a"]
        self.assertGreater(plot.height(), 200)
        self.assertLess(plot.width() / plot.height(), 1.8)

    def test_control_columns_follow_requested_order(self) -> None:
        for controls in (self.window.controls_a, self.window.controls_b):
            group = controls.group
            left = (controls.vds, controls.vg_initial, controls.vg_final, controls.vg_step)
            right = (controls.hold, controls.delay_ms, controls.keep, controls.interval)
            left_positions = [widget.mapTo(group, widget.rect().topLeft()) for widget in left]
            right_positions = [widget.mapTo(group, widget.rect().topLeft()) for widget in right]
            self.assertEqual(len({point.x() for point in left_positions}), 1)
            self.assertEqual(len({point.x() for point in right_positions}), 1)
            self.assertLess(left_positions[0].x(), right_positions[0].x())
            self.assertEqual(
                [point.y() for point in left_positions],
                [point.y() for point in right_positions],
            )
            self.assertTrue(all(
                first.y() < second.y()
                for first, second in zip(left_positions, left_positions[1:])
            ))

    def test_time_is_horizontal_and_negative_values_are_above(self) -> None:
        for key, quantity in (("id_a", "Id"), ("id_b", "Id"), ("vg_a", "Vg"), ("vg_b", "Vg")):
            plot = self.window.plots[key]
            self.assertEqual(plot.getAxis("bottom").labelText, "Elapsed time")
            self.assertEqual(plot.getAxis("left").labelText, quantity)
            self.assertTrue(plot.getViewBox().state["yInverted"])
        self.assertEqual(
            self.window.plots["id_a"].getAxis("left").tickStrings([2.5e-9, 3e-9], 1e9, 0.5),
            ["2.5", "3"],
        )
        self.window._sample("A", 1.25, 1e-10, -0.1, -2e-9, -0.01)
        self.assertEqual(list(self.window.curves["id_a"].xData), [1.25])
        self.assertEqual(list(self.window.curves["id_a"].yData), [-2e-9])
        self.assertEqual(list(self.window.curves["vg_a"].xData), [1.25])
        self.assertEqual(list(self.window.curves["vg_a"].yData), [-0.1])

    def test_nanoampere_current_curve_keeps_all_points(self) -> None:
        for index in range(100):
            self.window._sample("A", float(index), 0.0, -0.1, 2e-9 + index * 1e-11, -0.01)
        self.app.processEvents()
        self.assertEqual(len(self.window.curves["id_a"].getData()[0]), 100)


if __name__ == "__main__":
    unittest.main()
