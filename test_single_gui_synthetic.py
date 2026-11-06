"""Offline tests for the CH1/CH2-only single-OECT front panel."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets

from oect_dual_gui import AcquisitionWorker, CellPlan, CellRuntime, ExperimentPlan
from oect_single_gui import (
    SingleAcquisitionWorker,
    SingleExperimentPlan,
    SingleOECTInstrument,
    SingleOECTWindow,
)


def make_cell(path: Path, hold_s: float = 0.2) -> CellPlan:
    return CellPlan(
        name="A",
        drain_channel=1,
        gate_channel=2,
        vds_v=-0.01,
        vg_initial_v=-0.1,
        vg_final_v=-0.1,
        vg_step_v=-0.1,
        hold_s=hold_s,
        cycles=1,
        delay_s=0.0,
        interval_s=0.1,
        ids_limit_a=0.3,
        ig_limit_a=0.3,
        output_path=path,
    )


class SingleInstrumentTests(unittest.TestCase):
    def test_configures_measurement_only_on_ch1_and_ch2(self) -> None:
        class FakeInstrument:
            def __init__(self) -> None:
                self.channel = 0
                self.events: list[tuple[int, str]] = []

            def set_all_standby_direct(self) -> None:
                self.events.append((0, "standby_all"))

            def select(self, channel: int) -> None:
                self.channel = channel
                self.events.append((channel, f"SCH{channel}"))

            def write(self, command: str) -> None:
                self.events.append((self.channel, command))

            def query(self, command: str) -> str:
                self.events.append((self.channel, command))
                return "R0"

        with tempfile.TemporaryDirectory() as directory:
            plan = SingleExperimentPlan(
                host="127.0.0.1",
                port=5025,
                simulation=False,
                cell=make_cell(Path(directory) / "single.txt"),
            )
            driver = SingleOECTInstrument(plan.host, plan.port)
            fake = FakeInstrument()
            driver.instrument = fake  # type: ignore[assignment]
            driver.configure(plan)
            for channel in (3, 4):
                commands = [command for ch, command in fake.events if ch == channel]
                self.assertEqual(
                    commands[:5],
                    [f"SCH{channel}", "OP5", "CP7", "TG0", "M1"],
                )
            self.assertIn((1, "CP6"), fake.events)
            self.assertIn((2, "TG1"), fake.events)
            self.assertIn((1, "R0"), fake.events)
            self.assertIn((2, "R0"), fake.events)
            self.assertIn((1, "RD0"), fake.events)
            self.assertIn((2, "RD0"), fake.events)
            self.assertNotIn((1, "R1"), fake.events)
            self.assertNotIn((2, "R1"), fake.events)
            self.assertNotIn((3, "ST1"), fake.events)
            self.assertNotIn((4, "ST1"), fake.events)

    def test_both_workers_complete_cycles_and_zero_only_at_the_end(self) -> None:
        class FakeInstrument:
            def __init__(self) -> None:
                self.gates: list[float] = []
                self.zero_count = 0

            def set_gate(self, *args: float) -> None:
                self.gates.append(args[-1])

            def zero_sources(self) -> None:
                self.zero_count += 1

            def zero_cell(self, *_channels: int) -> None:
                self.zero_count += 1

        cell = replace(make_cell(Path("unused.txt")), vg_initial_v=0,
                       vg_final_v=-0.3, cycles=2)
        for dual in (False, True):
            with self.subTest(dual=dual):
                state = CellRuntime(cell, None)
                state.start_stage(0)
                fake = FakeInstrument()
                if dual:
                    disabled = replace(cell, name="B", enabled=False)
                    worker = AcquisitionWorker(ExperimentPlan("", 5025, True, cell, disabled))
                    other = CellRuntime(disabled, None, done=True)
                    advance = lambda elapsed: worker._advance_due_stages((state, other), elapsed, fake)
                else:
                    worker = SingleAcquisitionWorker(SingleExperimentPlan("", 5025, True, cell))
                    advance = lambda elapsed: worker._advance_if_due(state, elapsed, fake)
                for _ in range(len(cell.levels) - 1):
                    advance(state.stage_ends_elapsed_s)
                    self.assertFalse(state.done)
                    self.assertEqual(fake.zero_count, 0)
                self.assertEqual(tuple(fake.gates), cell.levels[1:])
                advance(state.stage_ends_elapsed_s - 0.001)
                self.assertFalse(state.done)
                advance(state.stage_ends_elapsed_s)
                self.assertTrue(state.done)
                self.assertEqual(fake.zero_count, 1)
                self.assertAlmostEqual(state.stage_ends_elapsed_s, cell.estimated_duration_s)

    def test_short_simulation_writes_one_historical_format_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "single.txt"
            plan = SingleExperimentPlan(
                host="127.0.0.1",
                port=5025,
                simulation=True,
                cell=make_cell(path),
            )
            progress: list[tuple[str, int, str]] = []
            worker = SingleAcquisitionWorker(plan)
            worker.progress_ready.connect(
                lambda name, percent, detail: progress.append((name, percent, detail))
            )
            worker.run()
            self.assertTrue(path.exists())
            lines = path.read_text().splitlines()
            self.assertEqual(lines[0], "Time\tGateI\tGateV\tDrainI\tDrainV")
            self.assertGreater(len(lines), 1)
            self.assertTrue(any(percent == 100 for _, percent, _ in progress))


class SingleGuiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        self.window = SingleOECTWindow()
        self.window.resize(1180, 800)
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()

    def test_rise_and_keep_selection_reaches_single_plan(self) -> None:
        controls = self.window.controls_a
        controls.mode.setCurrentIndex(controls.mode.findData("rise_keep"))
        controls.keep.setValue(25)
        plan = self.window._build_plan()
        self.assertEqual(plan.cell.mode, "rise_keep")
        self.assertEqual(plan.cell.final_keep_s, 25)
        self.assertEqual(len(plan.cell.levels), 9)
        self.assertEqual(plan.cell.estimated_duration_s, 9 * controls.hold.value() + 25)
        self.assertTrue(controls.mode.isVisible())
        self.assertTrue(controls.keep.isVisible())
        self.assertFalse(controls.cycles.isVisible())

    def test_cycle_control_reaches_single_plan(self) -> None:
        controls = self.window.controls_a
        controls.vg_initial.setValue(0)
        controls.vg_final.setValue(-0.3)
        controls.vg_step.setValue(-0.1)
        controls.cycles.setValue(2)
        plan = self.window._build_plan()
        self.assertEqual(plan.cell.cycles, 2)
        self.assertEqual(len(plan.cell.levels), 13)
        self.assertEqual(plan.cell.levels[-1], 0)
        self.assertEqual(plan.cell.estimated_duration_s, 13 * controls.hold.value())

    def test_same_controls_and_two_live_plots(self) -> None:
        plan = self.window._build_plan()
        self.assertEqual(plan.cell.drain_channel, 1)
        self.assertEqual(plan.cell.gate_channel, 2)
        self.assertEqual(
            plan.cell.output_path,
            Path(__file__).resolve().parent.parent / "measurement=single.txt",
        )
        self.assertEqual(set(self.window.plots), {"id_a", "vg_a"})
        self.assertTrue(self.window.controls_a.switch_row.isHidden())
        self.assertTrue(self.window.plots["id_a"].getViewBox().state["yInverted"])
        self.assertTrue(self.window.plots["vg_a"].getViewBox().state["yInverted"])


if __name__ == "__main__":
    unittest.main()
