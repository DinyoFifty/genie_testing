"""Verify guided calibration selection against captured firmware metadata."""

import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from linearreg import (
    ANTENNA_PAIRS, USED_OFFSETS, calibration_baseline, distance_fit, find_runs,
    group_runs, group_values, recommended_offsets, regression_data,
)


class RegressionBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.live = Path(self.temp.name)
        projects = {}
        for board, project in (("initiator", "bt_cs_ncp"), ("reflector", "bt_cs_soc_reflector_studio")):
            projects[board] = {
                "project": project,
                "physical_antenna": 1,
                "available_antennas": 1,
                "aox_control_pins": 0,
                "offset_config_type": "wireless",
                "wireless_offsets_cm": [270, 67, 0, 0],
                "wired_offsets_cm": [25, 25, 0, 0],
            }
        self.metadata = {
            "batch_id": "ant1-baseline-270",
            "attempt_id": "trial-1",
            "firmware": {"mode": "isolated", "projects": projects, "fingerprint": "saved-baseline"},
            "rotated_board": "reflector",
            "fixed_yaw_deg": 0,
            "setup": {},
        }

    def add_run(self, name, actual=1, measured=1.2, *, metadata=None, guided=True, **summary_changes):
        folder = self.live / name
        capture = folder / "capture"
        capture.mkdir(parents=True)
        summary = {
            "actual_distance_m": actual,
            "conditions": {"initiator_yaw_deg": 0, "reflector_yaw_deg": 0},
            "stop_reason": "target_reached",
            "target_reached": True,
            "reports_saved": True,
            "estimate_target": 100,
            "finite_filtered_estimate_count": 100,
        }
        if guided:
            summary["calibration"] = copy.deepcopy(self.metadata if metadata is None else metadata)
        summary.update(summary_changes)
        (capture / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        with (capture / "measurements.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("initiatorId", "reflectorId", "distanceMain"))
            writer.writerows(("initiator", "reflector", measured) for _ in range(100))
        return folder

    def test_saved_baseline_supplies_offsets_and_pair(self):
        self.add_run("near", measured=1.2)
        self.add_run("far", actual=2, measured=2.2)
        runs = find_runs(self.live)
        baseline = calibration_baseline(runs)
        self.assertEqual(baseline["pair"], ANTENNA_PAIRS[0])
        self.assertEqual(baseline["offsets"], ((270, 67, 0, 0), (270, 67, 0, 0)))
        actual, measured, pairs, skipped = regression_data(runs)
        self.assertFalse(skipped)
        offsets, _ = recommended_offsets(baseline["offsets"], baseline["pair"], USED_OFFSETS[1],
                                         actual, distance_fit(actual, measured), pairs)
        self.assertEqual(offsets, ((280, 67, 0, 0), (280, 67, 0, 0)))

    def test_selecting_one_batch_includes_its_trials_without_other_baselines(self):
        other = copy.deepcopy(self.metadata)
        other["batch_id"] = "different-baseline"
        for project in other["firmware"]["projects"].values():
            project["wireless_offsets_cm"][0] = 60
        for index in range(40):
            distance = (1, 2, 5, 10, 20)[index // 8]
            self.add_run(f"guided/Batch1_ANT1-ANT1/condition-{index}", distance, distance + 0.2)
            self.add_run(f"guided/Batch2_ANT1-ANT1/condition-{index}", distance,
                         distance + 0.4, metadata=other)
        groups = group_runs(find_runs(self.live))
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group.guided for group in groups))
        self.assertEqual([len(group.runs) for group in groups], [40, 40])
        actual, measured, _, skipped = regression_data(groups[0].runs)
        self.assertEqual(actual, [1, 2, 5, 10, 20])
        self.assertAlmostEqual(distance_fit(actual, measured).intercept, 0.2)
        self.assertFalse(skipped)
        self.assertEqual(group_values(groups[0])[1:3], (40, "1, 2, 5, 10, 20"))
        batch_only = group_runs(find_runs(self.live / "guided/Batch1_ANT1-ANT1"))
        self.assertEqual(len(batch_only), 1)
        self.assertEqual(len(batch_only[0].runs), 40)

    def test_batch_selection_skips_incomplete_trials_and_preserves_single_rows(self):
        self.add_run("guided/Batch1_ANT1-ANT1/complete")
        self.add_run("guided/Batch1_ANT1-ANT1/interrupted", target_reached=False)
        self.add_run("single-1", guided=False)
        self.add_run("single-2", actual=2, guided=False)
        groups = group_runs(find_runs(self.live))
        self.assertEqual(len(groups), 3)
        self.assertTrue(groups[0].guided)
        self.assertEqual(group_values(groups[0])[-1], "1 ready; 1 skipped")
        self.assertEqual([len(group.runs) for group in groups], [2, 1, 1])
        actual, _, _, skipped = regression_data(groups[0].runs)
        self.assertEqual(actual, [1])
        self.assertEqual(len(skipped), 1)

    def test_single_trials_use_captured_zero_and_nonzero_offsets(self):
        for old_offset in (0, 60):
            with self.subTest(offset=old_offset):
                firmware = copy.deepcopy(self.metadata["firmware"])
                for project in firmware["projects"].values():
                    project["wireless_offsets_cm"][0] = old_offset
                for distance in (1, 20):
                    self.add_run(f"single-{old_offset}/{distance}m", distance,
                                 distance + 1.6 - 2 * old_offset / 100,
                                 guided=False, firmware=firmware)
                runs = find_runs(self.live / f"single-{old_offset}")
                baseline = calibration_baseline(runs)
                self.assertIsNotNone(baseline)
                self.assertEqual(baseline["pair"], ANTENNA_PAIRS[0])
                self.assertEqual(baseline["offsets"][0][0], old_offset)
                actual, measured, pairs, skipped = regression_data(runs)
                self.assertFalse(skipped)
                offsets, _ = recommended_offsets(baseline["offsets"], baseline["pair"], USED_OFFSETS[1],
                                                 actual, distance_fit(actual, measured), pairs)
                self.assertEqual(offsets[0][0], 80)
                self.assertEqual(offsets[1][0], 80)

    def test_single_ant2_and_normal_modes_are_automatic(self):
        ant2 = copy.deepcopy(self.metadata["firmware"])
        for project in ant2["projects"].values():
            project["physical_antenna"] = 2
        self.add_run("single-ant2/near", guided=False, firmware=ant2)
        baseline = calibration_baseline(find_runs(self.live / "single-ant2"))
        self.assertEqual(baseline["pair"], ANTENNA_PAIRS[1])
        normal = self.normal_metadata()["firmware"]
        self.add_run("single-normal/near", guided=False, firmware=normal)
        baseline = calibration_baseline(find_runs(self.live / "single-normal"))
        self.assertEqual(baseline["mode"], "normal")
        self.assertIsNone(baseline["pair"])

    def test_single_trials_with_different_saved_offsets_are_rejected(self):
        firmware = copy.deepcopy(self.metadata["firmware"])
        self.add_run("single-before", guided=False, firmware=firmware)
        firmware["projects"]["reflector"]["wireless_offsets_cm"][0] = 60
        self.add_run("single-after", guided=False, firmware=firmware)
        with self.assertRaisesRegex(ValueError, "different antennas or firmware baselines"):
            regression_data(find_runs(self.live))

    def test_matching_guided_and_single_capture_settings_can_be_combined(self):
        self.add_run("guided")
        self.add_run("single", actual=2, measured=2.2, guided=False, firmware=self.metadata["firmware"])
        baseline = calibration_baseline(find_runs(self.live))
        self.assertEqual(baseline["pair"], ANTENNA_PAIRS[0])
        actual, _, _, skipped = regression_data(find_runs(self.live))
        self.assertEqual(actual, [1, 2])
        self.assertFalse(skipped)

    def test_simulated_and_real_captures_cannot_be_combined(self):
        self.add_run("real")
        self.add_run("simulated", simulated=True)
        with self.assertRaisesRegex(ValueError, "simulated and real"):
            regression_data(find_runs(self.live))

    def normal_metadata(self):
        metadata = copy.deepcopy(self.metadata)
        metadata["firmware"]["mode"] = "normal"
        for project in metadata["firmware"]["projects"].values():
            project.update(physical_antenna=None, available_antennas=2, aox_control_pins=1)
        return metadata

    def test_normal_guided_runs_support_accuracy_regression(self):
        metadata = self.normal_metadata()
        for actual in (1, 2, 5, 10, 20):
            for yaw in range(0, 360, 45):
                self.add_run(f"{actual}m-{yaw}", actual, actual + 0.2, metadata=metadata,
                             conditions={"initiator_yaw_deg": 0, "reflector_yaw_deg": yaw})
        runs = find_runs(self.live)
        baseline = calibration_baseline(runs)
        self.assertEqual(baseline["mode"], "normal")
        self.assertIsNone(baseline["pair"])
        actual, measured, pairs, skipped = regression_data(runs)
        self.assertEqual(actual, [1, 2, 5, 10, 20])
        self.assertFalse(skipped)
        fit = distance_fit(actual, measured)
        self.assertAlmostEqual(fit.slope, 1)
        self.assertAlmostEqual(fit.intercept, 0.2)
        offsets, _ = recommended_offsets(baseline["offsets"], baseline["pair"], USED_OFFSETS[1], actual, fit, pairs)
        self.assertIsNone(offsets)

    def test_normal_and_isolated_guided_runs_use_separate_baselines(self):
        self.add_run("isolated")
        self.add_run("normal", metadata=self.normal_metadata())
        with self.assertRaisesRegex(ValueError, "different antennas or firmware baselines"):
            regression_data(find_runs(self.live))

    def test_normal_metadata_requires_both_antennas_and_switching(self):
        metadata = self.normal_metadata()
        metadata["firmware"]["projects"]["reflector"]["aox_control_pins"] = 0
        self.add_run("broken-normal", metadata=metadata)
        with self.assertRaisesRegex(ValueError, "incomplete calibration baseline metadata"):
            regression_data(find_runs(self.live))

    def test_mixed_guided_and_legacy_runs_are_rejected(self):
        self.add_run("guided")
        self.add_run("legacy", guided=False)
        with self.assertRaisesRegex(ValueError, "older folders separately"):
            regression_data(find_runs(self.live))

    def test_ant2_retains_each_boards_captured_offset(self):
        ant2 = copy.deepcopy(self.metadata)
        for project in ant2["firmware"]["projects"].values():
            project["physical_antenna"] = 2
        ant2["firmware"]["projects"]["reflector"]["wireless_offsets_cm"][1] = 70
        self.add_run("ant2", metadata=ant2)
        runs = find_runs(self.live)
        baseline = calibration_baseline(runs)
        actual, measured, pairs, _ = regression_data(runs)
        self.assertEqual(baseline["pair"], ANTENNA_PAIRS[1])
        offsets, _ = recommended_offsets(baseline["offsets"], baseline["pair"], USED_OFFSETS[1],
                                         actual, distance_fit(actual, measured), pairs)
        self.assertEqual(offsets, ((270, 77, 0, 0), (270, 80, 0, 0)))

    def test_changed_offsets_are_rejected_even_with_identical_fingerprint(self):
        self.add_run("before")
        changed = copy.deepcopy(self.metadata)
        changed["firmware"]["projects"]["reflector"]["wireless_offsets_cm"][0] = 300
        self.add_run("after", metadata=changed)
        with self.assertRaisesRegex(ValueError, "different antennas or firmware baselines"):
            regression_data(find_runs(self.live))

    def test_different_physical_antennas_are_rejected(self):
        self.add_run("ant1")
        changed = copy.deepcopy(self.metadata)
        for project in changed["firmware"]["projects"].values():
            project["physical_antenna"] = 2
        self.add_run("ant2", metadata=changed)
        with self.assertRaisesRegex(ValueError, "different antennas or firmware baselines"):
            regression_data(find_runs(self.live))

    def test_identical_firmware_in_multiple_batches_is_compatible(self):
        self.add_run("one")
        second = copy.deepcopy(self.metadata)
        second["batch_id"] = "ant1-second-session"
        second["attempt_id"] = "trial-2"
        self.add_run("two", metadata=second)
        baseline = calibration_baseline(find_runs(self.live))
        self.assertEqual(baseline["batches"], ["ant1-baseline-270", "ant1-second-session"])

    def test_incomplete_guided_attempts_are_skipped(self):
        self.add_run("complete")
        self.add_run("short", finite_filtered_estimate_count=84, target_reached=False)
        self.add_run("interrupted", stop_reason="analyzer_interrupted")
        self.add_run("missing-plots", reports_saved=False)
        runs = find_runs(self.live)
        self.assertEqual(sum(bool(run.issue) for run in runs), 3)
        actual, _, _, skipped = regression_data(runs)
        self.assertEqual(actual, [1])
        self.assertEqual(len(skipped), 3)

    def test_malformed_guided_baseline_is_rejected(self):
        incomplete = copy.deepcopy(self.metadata)
        del incomplete["firmware"]["projects"]["initiator"]["wireless_offsets_cm"]
        self.add_run("broken", metadata=incomplete)
        with self.assertRaisesRegex(ValueError, "incomplete calibration baseline metadata"):
            regression_data(find_runs(self.live))

    def test_legacy_only_selection_preserves_existing_behavior(self):
        self.add_run("near", guided=False)
        self.add_run("far", actual=2, measured=2.3, guided=False)
        runs = find_runs(self.live)
        self.assertIsNone(calibration_baseline(runs))
        actual, measured, _, skipped = regression_data(runs)
        self.assertEqual(actual, [1, 2])
        self.assertEqual(measured, [1.2, 2.3])
        self.assertFalse(skipped)


if __name__ == "__main__":
    unittest.main()
