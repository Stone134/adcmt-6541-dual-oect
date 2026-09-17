"""Offline tests for GUI scheduling and exact legacy text formatting."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets

from oect_dual_gui import (
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

        with tempfile.TemporaryDirectory() as directory:
            def cell(name: str, vds: float, vg: float, interval: float) -> CellPlan:
                return CellPlan(
                    name=name,
                    drain_channel=1 if name == "A" else 3,
                    gate_channel=2 if name == "A" else 4,
                    vds_v=vds, vg_initial_v=vg, vg_final_v=vg - 0.1,
                    vg_step_v=-0.1, hold_s=60.0, keep_s=0.0,
                    delay_s=1.0, interval_s=interval,
                    ids_limit_a=0.03, ig_limit_a=0.003,
                    output_path=Path(directory) / f"{name}.txt",
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
            self.assertIn("SOV-0.01", events)
            self.assertIn("SOV-0.02", events)
            self.assertIn("SOV-0.1", events)
            self.assertIn("SOV-0.3", events)


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
        self.assertEqual(plan.cell_a.output_path, Path(__file__).resolve().parent / "measurement=lvl1.txt")
        self.assertEqual(plan.cell_b.output_path, Path(__file__).resolve().parent / "measurement=lvl2.txt")
        for cell in (plan.cell_a, plan.cell_b):
            self.assertEqual(cell.ids_limit_a, 0.3)
            self.assertEqual(cell.ig_limit_a, 0.3)
        self.assertFalse(hasattr(self.window.controls_a, "ids_limit"))

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
