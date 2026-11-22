"""Guided progress tests use saved synthetic summaries and mocked recorders."""

from contextlib import nullcontext
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from support import genie_calibration as guided


class GuidedCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.live = self.root / "data" / "live"
        self.path = self.root / "data" / "calibration" / "batch" / "progress.json"
        self.snapshot = {"mode": "isolated", "fingerprint": "baseline", "projects": {
            role: {"project": project, "physical_antenna": 1, "available_antennas": 1,
                   "aox_control_pins": 0, "offset_config_type": "wireless",
                   "wireless_offsets_cm": [56, 67, 0, 0], "wired_offsets_cm": [25, 25, 0, 0]}
            for role, project in guided.PROJECTS.items()}}
        self.progress = {"version": 1, "batch_id": "batch", "created_at_utc": "2026-11-21T00:00:00Z",
                         "firmware": self.snapshot, "rotated_board": "reflector", "fixed_yaw_deg": 0,
                         "setup": {"initiator_height_m": 1.5, "reflector_height_m": 1.5},
                         "board_pair": None, "attempts": [], "completed_conditions": []}
        guided.save_progress(self.path, self.progress)

    def save_trial(self, distance=1, yaw=0, *, count=100, reports=True, pair=None,
                   conditions=None, progress=None, path=None):
        progress = self.progress if progress is None else progress
        path = self.path if path is None else path
        if conditions is None:
            conditions = {**progress["setup"], "initiator_yaw_deg": 0, "reflector_yaw_deg": yaw}
        metadata = guided.begin_attempt(progress, path, distance, conditions)
        session = self.live / f"trial-{len(list(self.live.glob('*'))) + 1:03d}"
        capture = session / "capture"
        capture.mkdir(parents=True)
        summary = {"calibration": metadata, "actual_distance_m": distance, "conditions": conditions,
                   "board_pair": pair or ["initiator-id", "reflector-id"],
                   "finite_filtered_estimate_count": count, "estimate_target": 100,
                   "target_reached": count == 100, "reports_saved": reports,
                   "stop_reason": "target_reached" if count == 100 else "analyzer_interrupted"}
        (capture / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        for name in ("capture/raw.jsonl", "capture/measurements.csv", "measurements.xlsx",
                     "readings_in_order.png", "filtered_distance_distribution.png"):
            (session / name).write_bytes(b"synthetic report")
        return session, summary

    def normal_snapshot(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.update(mode="normal", fingerprint="normal-baseline")
        for project in snapshot["projects"].values():
            project.update(physical_antenna=None, available_antennas=2, aox_control_pins=1)
        return snapshot

    def test_normal_mode_completes_and_resumes_all_40_conditions(self):
        self.snapshot = self.normal_snapshot()
        self.progress["firmware"] = self.snapshot
        guided.save_progress(self.path, self.progress)
        for distance in reversed(guided.DISTANCES):
            for yaw in reversed(guided.ANGLES):
                self.save_trial(distance, yaw)
        guided.recover_progress(self.progress, self.live)
        guided.save_progress(self.path, self.progress)
        self.assertEqual(len(self.progress["completed_conditions"]), 40)
        with patch("builtins.input", return_value=""):
            path, reopened = guided.choose_batch(self.snapshot, self.live, self.path.parent.parent)
        self.assertEqual(path, self.path)
        self.assertEqual(reopened["firmware"]["mode"], "normal")
        self.assertFalse(guided.pending_conditions(reopened))

    def test_modes_keep_separate_progress_and_resume_the_original_batch(self):
        self.save_trial(1, 0)
        normal = self.normal_snapshot()
        normal_progress = {**copy.deepcopy(self.progress), "batch_id": "normal-batch", "firmware": normal,
                           "attempts": [], "completed_conditions": []}
        with patch("support.genie_calibration._new_progress", return_value=normal_progress) as create:
            normal_path, _ = guided.choose_batch(normal, self.live, self.path.parent.parent)
        create.assert_called_once()
        self.save_trial(20, 90, progress=normal_progress, path=normal_path)
        with patch("builtins.input", return_value=""):
            _, isolated = guided.choose_batch(self.snapshot, self.live, self.path.parent.parent)
            _, dual = guided.choose_batch(normal, self.live, self.path.parent.parent)
        self.assertEqual(isolated["completed_conditions"], ["1m_0deg"])
        self.assertEqual(dual["completed_conditions"], ["20m_90deg"])
        self.assertEqual(len(isolated["attempts"]), 1)
        self.assertEqual(len(dual["attempts"]), 1)

    def test_out_of_order_conditions_and_equivalent_signed_yaws(self):
        self.save_trial(20, -270)
        guided.recover_progress(self.progress, self.live)
        self.assertEqual(self.progress["completed_conditions"], ["20m_90deg"])
        self.assertEqual(len(guided.pending_conditions(self.progress)), 39)
        self.assertEqual(guided.pending_conditions(self.progress)[0], (1, 0))
        self.save_trial(1, 360)
        guided.recover_progress(self.progress, self.live)
        self.assertEqual(len(self.progress["completed_conditions"]), 2)
        self.assertEqual(guided.normalize_yaw(-45), 315)

    def test_repeated_condition_is_preserved_without_counting_twice(self):
        self.save_trial(1, 0)
        self.save_trial(1, 360)
        guided.recover_progress(self.progress, self.live)
        self.assertEqual(len(self.progress["attempts"]), 2)
        self.assertEqual(self.progress["completed_conditions"], ["1m_0deg"])
        self.assertTrue(all(attempt["status"] == "complete" for attempt in self.progress["attempts"]))

    def test_same_distance_distinct_yaws_complete_different_points(self):
        for yaw in guided.ANGLES:
            self.save_trial(5, yaw)
        guided.recover_progress(self.progress, self.live)
        self.assertEqual(len(self.progress["completed_conditions"]), 8)
        self.assertFalse(any(distance == 5 for distance, _ in guided.pending_conditions(self.progress)))

    def test_all_40_points_complete_and_a_repeat_remains_40(self):
        for distance in reversed(guided.DISTANCES):
            for yaw in reversed(guided.ANGLES):
                self.save_trial(distance, yaw)
        self.save_trial(20, -270)
        guided.recover_progress(self.progress, self.live)
        self.assertEqual(len(self.progress["completed_conditions"]), 40)
        self.assertEqual(len(self.progress["attempts"]), 41)
        self.assertEqual(guided.pending_conditions(self.progress), [])
        guided.save_progress(self.path, self.progress)
        with patch("builtins.input", return_value=""):
            path, reopened = guided.choose_batch(self.snapshot, self.live, self.path.parent.parent)
        self.assertEqual(path, self.path)
        self.assertEqual(len(reopened["completed_conditions"]), 40)

    def test_partial_and_unsaved_reports_remain_pending(self):
        self.save_trial(1, 0, count=43)
        self.save_trial(1, 45, reports=False)
        guided.recover_progress(self.progress, self.live)
        self.assertFalse(self.progress["completed_conditions"])
        self.assertTrue(all(attempt["status"] == "incomplete" for attempt in self.progress["attempts"]))
        self.assertEqual(self.progress["attempts"][0]["finite_filtered_estimate_count"], 43)

    def test_interrupted_capture_with_100_values_still_remains_pending(self):
        session, summary = self.save_trial(1, 0)
        summary["stop_reason"] = "analyzer_interrupted"
        (session / "capture" / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        guided.recover_progress(self.progress, self.live)
        self.assertFalse(self.progress["completed_conditions"])

    def test_missing_report_files_prevent_completion(self):
        session, _ = self.save_trial(1, 0)
        (session / "readings_in_order.png").unlink()
        guided.recover_progress(self.progress, self.live)
        self.assertFalse(self.progress["completed_conditions"])
        self.assertIn("missing", self.progress["attempts"][0]["detail"])

    def test_deleted_summary_removes_previously_completed_point(self):
        session, _ = self.save_trial(1, 0)
        guided.recover_progress(self.progress, self.live)
        self.assertEqual(self.progress["completed_conditions"], ["1m_0deg"])
        (session / "capture" / "summary.json").unlink()
        guided.recover_progress(self.progress, self.live)
        self.assertEqual(self.progress["completed_conditions"], [])
        self.assertEqual(self.progress["attempts"][0]["status"], "incomplete")

    def test_resume_recovers_capture_saved_after_last_progress_write(self):
        session, _ = self.save_trial(10, 135)
        restarted = guided._load_progress(self.path)
        self.assertEqual(restarted["attempts"][0]["status"], "pending")
        guided.recover_progress(restarted, self.live)
        guided.save_progress(self.path, restarted)
        self.assertEqual(restarted["completed_conditions"], ["10m_135deg"])
        self.assertEqual(restarted["attempts"][0]["live_folder"], session.name)
        guided.recover_progress(restarted, self.live)
        self.assertEqual(len(restarted["attempts"]), 1)

    def test_summary_recovers_an_attempt_absent_from_progress(self):
        self.save_trial(2, 180)
        self.progress["attempts"] = []
        guided.recover_progress(self.progress, self.live)
        self.assertEqual(self.progress["completed_conditions"], ["2m_180deg"])
        self.assertEqual(len(self.progress["attempts"]), 1)

    def test_missing_and_off_grid_conditions_are_extras(self):
        self.save_trial(3, 0)
        self.save_trial(1, 12)
        self.save_trial(1, conditions={**self.progress["setup"], "initiator_yaw_deg": 0})
        self.save_trial(1, conditions={**self.progress["setup"], "initiator_yaw_deg": 45, "reflector_yaw_deg": 90})
        guided.recover_progress(self.progress, self.live)
        self.assertFalse(self.progress["completed_conditions"])
        self.assertTrue(all(attempt["status"] == "extra" for attempt in self.progress["attempts"]))

    def test_first_board_pair_is_bound_and_other_devices_are_excluded(self):
        self.save_trial(1, 0, count=30)
        self.save_trial(1, 45, pair=["different", "reflector-id"])
        self.save_trial(1, 90)
        guided.recover_progress(self.progress, self.live)
        self.assertEqual(self.progress["board_pair"], ["initiator-id", "reflector-id"])
        self.assertEqual(self.progress["attempts"][1]["status"], "excluded")
        self.assertEqual(self.progress["completed_conditions"], ["1m_90deg"])

    def test_captured_height_change_does_not_mix_setups(self):
        conditions = {**self.progress["setup"], "initiator_height_m": 2.0,
                      "initiator_yaw_deg": 0, "reflector_yaw_deg": 0}
        self.save_trial(1, conditions=conditions)
        guided.recover_progress(self.progress, self.live)
        self.assertEqual(self.progress["attempts"][0]["status"], "excluded")
        self.assertFalse(self.progress["completed_conditions"])

    def test_rotating_initiator_uses_reflector_as_fixed_board(self):
        self.progress["rotated_board"] = "initiator"
        self.progress["fixed_yaw_deg"] = 315
        conditions = {"initiator_yaw_deg": -270, "reflector_yaw_deg": -45}
        self.assertEqual(guided.condition_key(self.progress, 20, conditions), "20m_90deg")
        conditions["reflector_yaw_deg"] = 0
        self.assertIsNone(guided.condition_key(self.progress, 20, conditions))

    def test_changed_baseline_cannot_be_recovered_into_existing_batch(self):
        session, summary = self.save_trial(1, 0)
        summary["calibration"]["firmware"] = {**self.snapshot, "fingerprint": "changed"}
        (session / "capture" / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        guided.recover_progress(self.progress, self.live)
        self.assertFalse(self.progress["completed_conditions"])
        self.assertEqual(self.progress["attempts"][0]["status"], "excluded")
        changed = {**self.snapshot, "fingerprint": "changed"}
        with patch("support.genie_calibration._new_progress", return_value={**self.progress, "batch_id": "new-batch"}) as new:
            path, _ = guided.choose_batch(changed, self.live, self.path.parent.parent)
        new.assert_called_once()
        self.assertEqual(path.parent.name, "new-batch")

    def test_captured_offset_values_must_match_even_if_fingerprint_is_copied(self):
        session, summary = self.save_trial(1, 0)
        summary = json.loads(json.dumps(summary))
        summary["calibration"]["firmware"]["projects"]["initiator"]["wireless_offsets_cm"][0] = 99
        (session / "capture" / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        guided.recover_progress(self.progress, self.live)
        self.assertFalse(self.progress["completed_conditions"])
        self.assertEqual(self.progress["attempts"][0]["status"], "excluded")

    def test_save_progress_failure_preserves_last_document(self):
        original = self.path.read_bytes()
        with patch("pathlib.Path.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                guided.save_progress(self.path, {**self.progress, "completed_conditions": ["1m_0deg"]})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_compatible_batch_can_be_reopened(self):
        with patch("builtins.input", return_value=""):
            path, progress = guided.choose_batch(self.snapshot, self.live, self.path.parent.parent)
        self.assertEqual(path, self.path)
        self.assertEqual(progress["batch_id"], "batch")

    def test_an_existing_batch_lock_prevents_a_second_writer(self):
        with guided._batch_lock(self.path.with_name("progress.lock")):
            with self.assertRaises((OSError, RuntimeError)):
                with guided._batch_lock(self.path.with_name("progress.lock")):
                    self.fail("A second writer acquired the same batch.")

    def test_prompt_allows_jump_and_manual_signed_yaws(self):
        with patch("builtins.input", side_effect=["20", "0", "-270", ""]):
            distance, conditions = guided._prompt_trial(self.progress)
        self.assertEqual(distance, 20)
        self.assertEqual(conditions["reflector_yaw_deg"], -270)
        self.assertEqual(guided.condition_key(self.progress, distance, conditions), "20m_90deg")

    def test_prompt_stays_at_jumped_distance_until_its_eight_angles_finish(self):
        self.save_trial(20, 90)
        guided.recover_progress(self.progress, self.live)
        with patch("builtins.input", side_effect=["", "", "", ""]) as answer:
            distance, _ = guided._prompt_trial(self.progress)
        self.assertEqual(distance, 20)
        self.assertIn("Enter = 20", answer.call_args_list[0].args[0])
        for angle in guided.ANGLES:
            if angle != 90:
                self.save_trial(20, angle)
        guided.recover_progress(self.progress, self.live)
        with patch("builtins.input", side_effect=["", "", "", ""]):
            distance, _ = guided._prompt_trial(self.progress)
        self.assertEqual(distance, 1)

    def test_batch_setup_requires_fixed_yaw_and_preserves_missing_heights(self):
        with (patch("builtins.input", side_effect=["", "skip", "-360", "skip", "skip", "skip"]),
              patch("support.genie_calibration.last_board_heights", return_value={})):
            progress = guided._new_progress(self.snapshot, self.live)
        self.assertEqual(progress["fixed_yaw_deg"], 0)
        self.assertEqual(progress["setup"], {})

    def check_guided_loop_records_twice(self):
        client = Mock()
        capture = Mock()
        client.start.return_value = capture
        client.running.return_value = True
        trials = [(20.0, {**self.progress["setup"], "initiator_yaw_deg": 0, "reflector_yaw_deg": 90}),
                  (1.0, {**self.progress["setup"], "initiator_yaw_deg": 0, "reflector_yaw_deg": 0}), "done"]

        def record(**kwargs):
            stored = guided._load_progress(self.path)
            self.assertEqual(stored["attempts"][-1]["status"], "pending")
            self.assertEqual(kwargs["max_estimates"], 100)
            self.assertEqual(kwargs["field"], "distanceMain")
            self.assertIs(kwargs["recording"], capture)
            session = self.live / f"loop-{len(list(self.live.glob('*')))}"
            (session / "capture").mkdir(parents=True)
            summary = {"calibration": kwargs["calibration"], "actual_distance_m": kwargs["actual_distance"],
                       "conditions": kwargs["conditions"], "board_pair": ["initiator-id", "reflector-id"],
                       "finite_filtered_estimate_count": 100, "estimate_target": 100,
                       "target_reached": True, "reports_saved": True, "stop_reason": "target_reached"}
            (session / "capture" / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
            for name in ("capture/raw.jsonl", "capture/measurements.csv", "measurements.xlsx",
                         "readings_in_order.png", "filtered_distance_distribution.png"):
                (session / name).write_bytes(b"synthetic report")
            return session

        with (patch("support.genie_calibration.snapshot_firmware", return_value=self.snapshot),
              patch("support.genie_calibration.choose_batch", return_value=(self.path, self.progress)),
              patch("support.genie_calibration._prompt_trial", side_effect=trials),
              patch("support.genie_calibration.analyzer_session", return_value=nullcontext(client)) as connect,
              patch("support.genie_calibration.run_live", side_effect=record),
              patch("support.genie_calibration.show_status")):
            self.assertEqual(guided.run_guided(self.root / "data", self.live, plot=False), self.path)
        connect.assert_called_once()
        self.assertEqual(client.start.call_count, 2)
        saved = guided._load_progress(self.path)
        self.assertEqual(set(saved["completed_conditions"]), {"20m_90deg", "1m_0deg"})

    def test_guided_loop_records_twice_without_reopening_analyzer(self):
        self.check_guided_loop_records_twice()

    def test_normal_guided_loop_records_twice_without_reopening_analyzer(self):
        self.snapshot = self.normal_snapshot()
        self.progress["firmware"] = self.snapshot
        guided.save_progress(self.path, self.progress)
        self.check_guided_loop_records_twice()

    def test_guided_stops_when_recorder_dies_and_preserves_pending_attempt(self):
        client = Mock()
        client.start.side_effect = RuntimeError("recorder died")
        with (patch("support.genie_calibration.snapshot_firmware", return_value=self.snapshot),
              patch("support.genie_calibration.choose_batch", return_value=(self.path, self.progress)),
              patch("support.genie_calibration._prompt_trial", return_value=(1, {"initiator_yaw_deg": 0, "reflector_yaw_deg": 0})),
              patch("support.genie_calibration.analyzer_session", return_value=nullcontext(client)),
              patch("support.genie_calibration.show_status")):
            with self.assertRaisesRegex(RuntimeError, "recorder died"):
                guided.run_guided(self.root / "data", self.live, plot=False)
        saved = guided._load_progress(self.path)
        self.assertEqual(len(saved["attempts"]), 1)
        self.assertEqual(saved["attempts"][0]["status"], "pending")

    def test_guided_refuses_offset_change_before_recording(self):
        changed = {**self.snapshot, "fingerprint": "changed"}
        with (patch("support.genie_calibration.snapshot_firmware", side_effect=[self.snapshot, changed]),
              patch("support.genie_calibration.choose_batch", return_value=(self.path, self.progress)),
              patch("support.genie_calibration._prompt_trial", return_value=(1, {"initiator_yaw_deg": 0, "reflector_yaw_deg": 0})),
              patch("support.genie_calibration.analyzer_session") as connect,
              patch("support.genie_calibration.show_status")):
            guided.run_guided(self.root / "data", self.live, plot=False)
        connect.assert_not_called()
        self.assertFalse(guided._load_progress(self.path)["attempts"])

    def test_guided_exits_after_user_stops_capture(self):
        client = Mock()
        client.running.return_value = True

        def record(**kwargs):
            session = self.live / "stopped-trial"
            (session / "capture").mkdir(parents=True)
            summary = {"calibration": kwargs["calibration"], "actual_distance_m": kwargs["actual_distance"],
                       "conditions": kwargs["conditions"], "board_pair": ["initiator-id", "reflector-id"],
                       "finite_filtered_estimate_count": 20, "estimate_target": 100,
                       "target_reached": False, "reports_saved": True, "user_stopped": True,
                       "stop_reason": "user_stopped"}
            (session / "capture" / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
            return session

        conditions = {**self.progress["setup"], "initiator_yaw_deg": 0, "reflector_yaw_deg": 0}
        with (patch("support.genie_calibration.snapshot_firmware", return_value=self.snapshot),
              patch("support.genie_calibration.choose_batch", return_value=(self.path, self.progress)),
              patch("support.genie_calibration._prompt_trial", return_value=(1, conditions)) as prompt,
              patch("support.genie_calibration.analyzer_session", return_value=nullcontext(client)),
              patch("support.genie_calibration.run_live", side_effect=record),
              patch("support.genie_calibration.show_status")):
            guided.run_guided(self.root / "data", self.live, plot=False)
        self.assertEqual(prompt.call_count, 1)
        saved = guided._load_progress(self.path)
        self.assertTrue(saved["attempts"][0]["user_stopped"])
        self.assertFalse(saved["completed_conditions"])

    def test_guided_rechecks_baseline_after_analyzer_connects(self):
        changed = {**self.snapshot, "fingerprint": "changed"}
        client = Mock()
        with (patch("support.genie_calibration.snapshot_firmware", side_effect=[self.snapshot, self.snapshot, changed]),
              patch("support.genie_calibration.choose_batch", return_value=(self.path, self.progress)),
              patch("support.genie_calibration._prompt_trial", return_value=(1, {"initiator_yaw_deg": 0, "reflector_yaw_deg": 0})),
              patch("support.genie_calibration.analyzer_session", return_value=nullcontext(client)),
              patch("support.genie_calibration.show_status")):
            guided.run_guided(self.root / "data", self.live, plot=False)
        client.start.assert_not_called()
        saved = guided._load_progress(self.path)
        self.assertEqual(len(saved["attempts"]), 1)
        self.assertEqual(saved["attempts"][0]["status"], "pending")


class FirmwareSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.firmware = Path(self.temp.name)
        self.offset_headers = []
        self.aox_headers = []
        for project in guided.PROJECTS.values():
            config = self.firmware / project / "config"
            config.mkdir(parents=True)
            header = config / "sl_rail_util_cs_antenna_offset_config.h"
            header.write_text("\n".join([
                "#define GENIE_TEST_ANTENNA 1",
                "#define SL_RAIL_UTIL_CS_ANTENNA_COUNT 1",
                "#define SL_RAIL_UTIL_CS_ANTENNA_CONFIG_TYPE SL_RAIL_UTIL_CS_ANTENNA_OFFSET_CONFIG_TYPE_WIRELESS",
                "#define GENIE_ANT1_WIRELESS_OFFSET_CM 56",
                "#define GENIE_ANT2_WIRELESS_OFFSET_CM 67",
                "#define GENIE_ANT1_WIRED_OFFSET_CM 25",
                "#define GENIE_ANT2_WIRED_OFFSET_CM 25",
                "#define SL_RAIL_UTIL_CS_ANTENNA_OFFSET_WIRELESS_CM { GENIE_SELECTED_WIRELESS_OFFSET_CM, 0, 0, 0 }",
                "#define SL_RAIL_UTIL_CS_ANTENNA_OFFSET_WIRED_CM { GENIE_SELECTED_WIRED_OFFSET_CM, 0, 0, 0 }",
            ]), encoding="utf-8")
            aox = config / "sl_rail_util_aox_config.h"
            aox.write_text("#define SL_RAIL_UTIL_AOX_ANTENNA_PIN_COUNT 0\n", encoding="utf-8")
            self.offset_headers.append(header)
            self.aox_headers.append(aox)

    def test_snapshot_tracks_physical_offsets_and_config_type(self):
        original = guided.snapshot_firmware(self.firmware)
        self.assertEqual(original["projects"]["initiator"]["wireless_offsets_cm"], [56, 67, 0, 0])
        self.assertEqual(original["projects"]["reflector"]["offset_config_type"], "wireless")
        source = self.offset_headers[0].read_text().replace("OFFSET_CM 56", "OFFSET_CM 70")
        self.offset_headers[0].write_text(source, encoding="utf-8")
        self.assertNotEqual(guided.snapshot_firmware(self.firmware)["fingerprint"], original["fingerprint"])

    def set_normal_mode(self):
        for header in self.offset_headers:
            source = header.read_text().replace("SL_RAIL_UTIL_CS_ANTENNA_COUNT 1", "SL_RAIL_UTIL_CS_ANTENNA_COUNT 2")
            for kind in ("WIRELESS", "WIRED"):
                source = source.replace(f"GENIE_SELECTED_{kind}_OFFSET_CM, 0",
                                        f"GENIE_ANT1_{kind}_OFFSET_CM, GENIE_ANT2_{kind}_OFFSET_CM")
            header.write_text(source, encoding="utf-8")
        for header in self.aox_headers:
            header.write_text("#define SL_RAIL_UTIL_AOX_ANTENNA_PIN_COUNT 1\n", encoding="utf-8")

    def test_snapshot_accepts_normal_mode_and_ignores_inactive_antenna_selection(self):
        isolated = guided.snapshot_firmware(self.firmware)
        self.set_normal_mode()
        normal = guided.snapshot_firmware(self.firmware)
        self.assertEqual(normal["mode"], "normal")
        self.assertNotEqual(normal["fingerprint"], isolated["fingerprint"])
        for project in normal["projects"].values():
            self.assertEqual(project["available_antennas"], 2)
            self.assertEqual(project["aox_control_pins"], 1)
            self.assertIsNone(project["physical_antenna"])
            self.assertEqual(project["wireless_offsets_cm"], [56, 67, 0, 0])
        header = self.offset_headers[1]
        header.write_text(header.read_text().replace("GENIE_TEST_ANTENNA 1", "GENIE_TEST_ANTENNA 2"), encoding="utf-8")
        self.assertEqual(guided.snapshot_firmware(self.firmware), normal)

    def test_snapshot_refuses_mixed_modes_and_conflicting_dual_arrays(self):
        self.set_normal_mode()
        header = self.offset_headers[0]
        original = header.read_text()
        header.write_text(original.replace("SL_RAIL_UTIL_CS_ANTENNA_COUNT 2", "SL_RAIL_UTIL_CS_ANTENNA_COUNT 1"), encoding="utf-8")
        self.aox_headers[0].write_text("#define SL_RAIL_UTIL_AOX_ANTENNA_PIN_COUNT 0\n", encoding="utf-8")
        for kind in ("WIRELESS", "WIRED"):
            source = header.read_text().replace(f"GENIE_ANT1_{kind}_OFFSET_CM, GENIE_ANT2_{kind}_OFFSET_CM",
                                                f"GENIE_SELECTED_{kind}_OFFSET_CM, 0")
            header.write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "same firmware mode"):
            guided.snapshot_firmware(self.firmware)
        self.set_normal_mode()
        header.write_text(header.read_text().replace("GENIE_ANT2_WIRELESS_OFFSET_CM, 0", "999, 0"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "disagrees"):
            guided.snapshot_firmware(self.firmware)

    def test_snapshot_refuses_inconsistent_pin_count_or_conflicting_applied_array(self):
        header = self.aox_headers[0]
        header.write_text("#define SL_RAIL_UTIL_AOX_ANTENNA_PIN_COUNT 1\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "antenna count"):
            guided.snapshot_firmware(self.firmware)
        header.write_text("#define SL_RAIL_UTIL_AOX_ANTENNA_PIN_COUNT 0\n", encoding="utf-8")
        header = self.offset_headers[0]
        header.write_text(header.read_text().replace("GENIE_SELECTED_WIRELESS_OFFSET_CM, 0", "999, 0"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "disagrees"):
            guided.snapshot_firmware(self.firmware)

    def test_snapshot_refuses_mixed_physical_antennas(self):
        header = self.offset_headers[1]
        header.write_text(header.read_text().replace("GENIE_TEST_ANTENNA 1", "GENIE_TEST_ANTENNA 2"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "same physical"):
            guided.snapshot_firmware(self.firmware)

    def test_snapshot_refuses_wired_offset_mode(self):
        header = self.offset_headers[0]
        header.write_text(header.read_text().replace("CONFIG_TYPE_WIRELESS", "CONFIG_TYPE_WIRED"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "wireless"):
            guided.snapshot_firmware(self.firmware)


if __name__ == "__main__":
    unittest.main()
