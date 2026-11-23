"""Grouped trials, retries, migration, and batch deletion without hardware."""

import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from linearreg import find_runs, regression_data
from support import genie_calibration as guided
from support.genie_live import run_live
from support.trial_storage import batch_active, condition_name, trial_folders


class TrialStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.live = self.root / "data/live"
        self.batch = self.live / "guided/Trial1_ANT1-ANT1_2026-11-22_3-44PM"
        self.path = self.batch / "progress.json"
        firmware = {"mode": "isolated", "fingerprint": "baseline", "projects": {
            role: {"project": project, "physical_antenna": 1, "available_antennas": 1,
                   "aox_control_pins": 0, "offset_config_type": "wireless",
                   "wireless_offsets_cm": [60, 60, 0, 0], "wired_offsets_cm": [25, 25, 0, 0]}
            for role, project in guided.PROJECTS.items()}}
        self.progress = {"version": 1, "batch_id": "unique-batch", "batch_name": self.batch.name,
                         "created_at_utc": "2026-11-22T20:44:00Z", "firmware": firmware,
                         "rotated_board": "reflector", "fixed_yaw_deg": 0, "setup": {},
                         "board_pair": None, "attempts": [], "completed_conditions": []}
        guided.save_progress(self.path, self.progress)

    def capture(self, *, count=100, yaw=45, contents=b"new raw data", reports=True):
        conditions = {"initiator_yaw_deg": 0, "reflector_yaw_deg": yaw}
        metadata = guided.begin_attempt(self.progress, self.path, 1, conditions)
        session = self.batch / ".pending" / metadata["attempt_id"]
        (session / "capture").mkdir(parents=True)
        summary = {"calibration": metadata, "actual_distance_m": 1, "conditions": conditions,
                   "session_started_at_utc": metadata["started_at_utc"], "board_pair": ["ini", "ref"],
                   "finite_filtered_estimate_count": count, "estimate_target": 100,
                   "target_reached": count == 100, "reports_saved": reports,
                   "stop_reason": "target_reached" if count == 100 else "analyzer_interrupted"}
        (session / "capture/summary.json").write_text(json.dumps(summary), encoding="utf-8")
        for name in ("capture/raw.jsonl", "capture/measurements.csv", "measurements.xlsx",
                     "readings_in_order.png", "filtered_distance_distribution.png"):
            (session / name).write_bytes(contents)
        return session

    def recycle(self, folder):
        self.assertTrue(Path(folder).resolve().is_relative_to(self.live.resolve()))
        shutil.rmtree(folder)

    def publish(self, session):
        with patch.object(guided, "recycle_folder", side_effect=self.recycle):
            return guided.publish_trial(self.progress, self.path, session, self.live)

    def test_condition_names_and_numbered_antenna_batch_names(self):
        self.assertEqual(condition_name(1, {"initiator_yaw_deg": 360, "reflector_yaw_deg": -45}),
                         "1m_Ini0_Ref315")
        self.assertEqual(condition_name(None, {}), "Unknownm_IniUnknown_RefUnknown")
        folder = guided._create_batch_folder(self.progress, self.live)
        self.assertRegex(folder.name, r"^Trial2_ANT1-ANT1_2026-11-22_\d+-44(?:AM|PM)$")
        normal = copy.deepcopy(self.progress)
        normal["firmware"]["mode"] = "normal"
        self.assertTrue(guided._create_batch_folder(normal, self.live).name.startswith("Trial3_DUAL_"))

    def test_successful_retry_replaces_one_condition_and_keeps_timestamp(self):
        target = self.publish(self.capture(contents=b"old"))
        self.assertEqual(target, self.batch / "1m_Ini0_Ref45")
        self.publish(self.capture(contents=b"corrected"))
        self.assertEqual((target / "capture/raw.jsonl").read_bytes(), b"corrected")
        self.assertEqual(self.progress["completed_conditions"], ["1m_45deg"])
        self.assertEqual([item["status"] for item in self.progress["attempts"]], ["superseded", "complete"])
        summary = json.loads((target / "capture/summary.json").read_text())
        self.assertTrue(summary["session_started_at_utc"])
        self.assertEqual(summary["calibration"]["batch_name"], self.batch.name)
        self.assertEqual(list(trial_folders(self.live)), [target])

    def test_partial_or_unsaved_retry_keeps_previous_completed_capture(self):
        target = self.publish(self.capture(contents=b"keep original"))
        for options in ({"count": 17}, {"reports": False}):
            partial = self.publish(self.capture(contents=b"interrupted", **options))
            self.assertEqual(partial.parent, self.batch / "_incomplete")
            self.assertEqual((target / "capture/raw.jsonl").read_bytes(), b"keep original")
            guided.recover_progress(self.progress, self.live)
            self.assertEqual(self.progress["completed_conditions"], ["1m_45deg"])
        self.assertEqual(list(trial_folders(self.live)), [target])

    def test_failed_promotion_restores_the_previous_folder(self):
        target = self.publish(self.capture(contents=b"keep original"))
        new = self.capture(contents=b"new")
        rename = Path.rename

        def fail(source, destination):
            if source == new:
                raise OSError("simulated locked file")
            return rename(source, destination)

        with patch.object(Path, "rename", fail), self.assertRaisesRegex(OSError, "locked"):
            self.publish(new)
        self.assertEqual((target / "capture/raw.jsonl").read_bytes(), b"keep original")
        self.assertTrue(new.is_dir())

    def test_recycling_failure_keeps_backup_out_of_regression(self):
        self.publish(self.capture(contents=b"old"))
        new = self.capture(contents=b"corrected")
        with patch.object(guided, "recycle_folder", side_effect=OSError("Recycle Bin unavailable")):
            target = guided.publish_trial(self.progress, self.path, new, self.live)
        self.assertEqual((target / "capture/raw.jsonl").read_bytes(), b"corrected")
        self.assertEqual(len(list(self.batch.glob(".replaced-*"))), 1)
        self.assertEqual([run.folder for run in find_runs(self.live)], [target])

    def test_recovery_publishes_a_completed_pending_capture(self):
        pending = self.capture()
        with patch.object(guided, "recycle_folder", side_effect=self.recycle):
            guided.organize_batch(self.progress, self.path, self.live)
        self.assertFalse(pending.exists())
        self.assertTrue((self.batch / "1m_Ini0_Ref45/measurements.xlsx").is_file())
        restarted = guided._load_progress(self.path)
        guided.recover_progress(restarted, self.live)
        self.assertEqual(restarted["completed_conditions"], ["1m_45deg"])

    def test_recovery_never_replaces_newer_result_with_older_pending_capture(self):
        old = self.capture(contents=b"older unfinished publication")
        old_summary = old / "capture/summary.json"
        summary = json.loads(old_summary.read_text())
        summary["calibration"]["started_at_utc"] = "2026-11-20T20:00:00Z"
        old_summary.write_text(json.dumps(summary), encoding="utf-8")
        target = self.publish(self.capture(contents=b"newer completed capture"))
        with patch.object(guided, "recycle_folder", side_effect=self.recycle):
            guided.organize_batch(self.progress, self.path, self.live)
        self.assertEqual((target / "capture/raw.jsonl").read_bytes(), b"newer completed capture")
        self.assertFalse(old.exists())
        self.assertEqual(self.progress["completed_conditions"], ["1m_45deg"])

    def test_unspecified_conditions_do_not_overwrite_each_other(self):
        first = self.publish(self.capture(yaw=None, contents=b"unknown orientation one"))
        second = self.publish(self.capture(yaw=None, contents=b"unknown orientation two"))
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir() and second.is_dir())

    def test_real_exports_and_regression_use_only_the_completed_retry(self):
        conditions = {"initiator_yaw_deg": 0, "reflector_yaw_deg": 45}
        for value in (1.2, 1.05):
            metadata = guided.begin_attempt(self.progress, self.path, 1, conditions)
            recording = self.root / f"record-{metadata['attempt_id']}.jsonl"
            row = {"initiatorId": "ini", "reflectorId": "ref", "distanceMain": value, "distanceMainRaw": value}
            recording.write_text((json.dumps(row) + "\n") * 100, encoding="utf-8")
            session = run_live(recording, out=self.batch / ".pending", plot=False,
                               actual_distance=1, field="distanceMain", conditions=conditions,
                               max_estimates=100, calibration=metadata)
            self.publish(session)
        runs = find_runs(self.live)
        self.assertEqual(len(runs), 1)
        actual, measured, _, skipped = regression_data(runs)
        self.assertEqual(actual, [1])
        self.assertEqual(measured, [1.05])
        self.assertFalse(skipped)

    def test_legacy_migration_groups_matching_captures_and_preserves_singles(self):
        session = self.capture(contents=b"original raw bytes")
        flat = self.live / "old-guided-capture"
        session.rename(flat)
        single = self.live / "single-trial"
        (single / "capture").mkdir(parents=True)
        (single / "capture/summary.json").write_text('{"actual_distance_m":2}')
        legacy = self.root / "data/calibration/old-batch"
        legacy.parent.mkdir(parents=True)
        self.batch.rename(legacy)
        with patch.object(guided, "recycle_folder", side_effect=self.recycle):
            guided.migrate_batches(self.live, legacy.parent)
        paths = list((self.live / "guided").glob("*/progress.json"))
        self.assertEqual(len(paths), 1)
        folder = paths[0].parent
        self.assertTrue(folder.name.startswith("Trial1_ANT1-ANT1_"))
        self.assertEqual((folder / "1m_Ini0_Ref45/capture/raw.jsonl").read_bytes(), b"original raw bytes")
        self.assertFalse(flat.exists())
        self.assertTrue(single.is_dir())
        self.assertEqual(len(find_runs(self.live)), 2)

    def test_active_batch_cannot_be_deleted_and_marker_is_cleared(self):
        with guided._batch_lock(self.path.with_name("progress.lock")):
            self.assertTrue(batch_active(self.batch))
            with patch.object(guided, "recycle_folder") as recycle, self.assertRaisesRegex(RuntimeError, "recording"):
                guided.delete_batch(self.path, self.live)
            recycle.assert_not_called()
        self.assertFalse(batch_active(self.batch))
        self.assertFalse((self.batch / "progress.active.json").exists())

    def test_delete_menu_removes_progress_and_captures_together(self):
        self.publish(self.capture())
        with patch("builtins.input", side_effect=["delete 1", "yes", "done"]), patch.object(
                guided, "recycle_folder", side_effect=self.recycle):
            self.assertIsNone(guided.choose_batch(self.progress["firmware"], self.live, self.root / "data/calibration"))
        self.assertFalse(self.batch.exists())

    def test_batch_deletion_is_scoped_and_can_be_cancelled(self):
        with self.assertRaises(ValueError):
            guided.delete_batch(self.root / "elsewhere/progress.json", self.live)
        with patch("builtins.input", side_effect=["delete 1", "no", "done"]), patch.object(
                guided, "recycle_folder") as recycle:
            guided.choose_batch(self.progress["firmware"], self.live, self.root / "data/calibration")
        recycle.assert_not_called()
        self.assertTrue(self.path.is_file())


if __name__ == "__main__":
    unittest.main()
