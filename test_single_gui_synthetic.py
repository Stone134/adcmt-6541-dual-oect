"""Offline tests for the CH1/CH2-only single-OECT front panel."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets

from oect_dual_gui import CellPlan
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
        keep_s=0.0,
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
            self.assertNotIn((3, "ST1"), fake.events)
            self.assertNotIn((4, "ST1"), fake.events)

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
