"""Offline tests using only synthetic values; no instrument or experiment data."""

from __future__ import annotations

import unittest

from adcmt_6541_sync import (
    RunConfig,
    command_plan,
    parse_measurement_list,
    rows_for_batch,
)


def synthetic_config() -> RunConfig:
    return RunConfig.from_mapping(
        {
            "host": "127.0.0.1",
            "port": 5025,
            "period_s": 1.0,
            "samples_per_gate": 3,
            "gate_voltages_v": [-0.1, -0.2],
            "vds_a_v": -0.01,
            "vds_b_v": -0.02,
            "current_limits_a": [0.03, 0.003, 0.03, 0.003],
            "integration_code": "IT5",
            "fixed_measurement_range": False,
            "batch_samples": 2,
            "poll_interval_s": 0.01,
            "output_csv": "synthetic.csv",
            "standby_when_finished": True,
        }
    )


class SyntheticTests(unittest.TestCase):
    def test_measurement_parser(self) -> None:
        self.assertEqual(
            parse_measurement_list("+1.00000E-06,-2.50000E-03,+3.00000E+00"),
            [1e-6, -2.5e-3, 3.0],
        )

    def test_command_plan_contains_sync_roles(self) -> None:
        plan = command_plan(synthetic_config())
        self.assertEqual(plan.count("CP6"), 1)
        self.assertEqual(plan.count("TG0"), 1)
        self.assertEqual(plan.count("OP3"), 1)
        self.assertEqual(plan.count("CP7"), 3)
        self.assertEqual(plan.count("TG1"), 3)
        self.assertEqual(plan.count("OP1"), 3)
        self.assertEqual(plan.count("R0"), 4)
        self.assertEqual(plan.count("RD0"), 4)
        self.assertNotIn("R1", plan)
        self.assertIn("SP0,0.055,1000", plan)
        self.assertEqual(plan.count("SP0,0.050,999"), 3)

    def test_channel_mapping_and_common_sample_index(self) -> None:
        cfg = synthetic_config()
        rows = list(
            rows_for_batch(
                cfg=cfg,
                run_started_utc="2026-01-01T00:00:00+00:00",
                step_started_utc="2026-01-01T00:00:03+00:00",
                step_index=1,
                start_index=1,
                vg_a_v=-0.2,
                vg_b_v=-0.2,
                channel_values=(
                    [1.1, 1.2],  # CH1 Id A
                    [2.1, 2.2],  # CH2 Ig A
                    [3.1, 3.2],  # CH3 Id B
                    [4.1, 4.2],  # CH4 Ig B
                ),
            )
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["sample_index_global"], 4)
        self.assertEqual(rows[0]["nominal_elapsed_s"], "4.000000000")
        self.assertEqual(rows[0]["id_a_a"], "1.1")
        self.assertEqual(rows[0]["ig_a_a"], "2.1")
        self.assertEqual(rows[0]["id_b_a"], "3.1")
        self.assertEqual(rows[0]["ig_b_a"], "4.1")


if __name__ == "__main__":
    unittest.main()
