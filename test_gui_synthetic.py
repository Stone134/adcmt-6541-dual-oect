"""Offline tests for GUI scheduling and exact legacy text formatting."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from oect_dual_gui import (
    CellPlan,
    CellRuntime,
    DualOECTInstrument,
    ExperimentPlan,
    LEGACY_HEADER,
    LegacyTextWriter,
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


if __name__ == "__main__":
    unittest.main()
