"""Synthetic transport tests; these do not establish hardware performance."""
from contextlib import nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import analyzer as genie
from support.genie_live import (
    RecordingReader, create_live_session, find_recording, prompt_trial_details,
    recycle_duplicate_recording, run_live,
)


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "record-test.jsonl"
        self.swap = self.root / "record-test.jsonl.crswap"
        self.event = {"initiatorId": "synthetic-initiator", "reflectorId": "synthetic-reflector",
                      "distanceMainRaw": 0, "distanceMain": 0.1}

    def test_partial_append_and_atomic_rename_are_read_once(self):
        self.path.touch()
        self.swap.write_bytes(b'{"part":')
        reader = RecordingReader(self.path)
        self.assertEqual(reader.poll(), [])
        with self.swap.open("ab") as stream:
            stream.write(b'1}\n{"part":2}\n')
        self.assertEqual(reader.poll(), [(1, b'{"part":1}\n'), (2, b'{"part":2}\n')])
        self.swap.replace(self.path)
        self.assertEqual(reader.poll(), [])
        with self.path.open("ab") as stream:
            stream.write(b'{"part":3}\n')
        self.assertEqual(reader.poll(), [(3, b'{"part":3}\n')])

    def test_replacement_and_truncation_fail(self):
        self.swap.write_bytes(b'{"a":1}\n')
        reader = RecordingReader(self.path)
        reader.poll()
        self.swap.unlink()
        self.path.write_bytes(b'{"a":2}\n')
        with self.assertRaisesRegex(ValueError, "replaced"):
            reader.poll()
        self.path.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "truncated"):
            reader.poll()

    def test_find_temporary_recording(self):
        self.swap.touch()
        self.assertEqual(find_recording(self.root), self.path)

    def test_live_folders_use_local_start_and_allow_two_sessions_in_one_minute(self):
        out = self.root / "live"
        first = create_live_session(out, datetime(2026, 11, 19, 16, 10, 1))
        second = create_live_session(out, datetime(2026, 11, 19, 16, 10, 59))
        self.assertEqual(first.name, "2026-11-19_4-10PM")
        self.assertEqual(second.name, "2026-11-19_4-10PM-02")
        self.assertTrue(first.is_dir() and second.is_dir())

    def test_play_button_prompts_before_launching_analyzer(self):
        recording = Mock()
        with (
            patch.object(sys, "argv", ["analyzer.py"]),
            patch("builtins.input", side_effect=["1", "NaN", "1", "1.5", "1.6", "+45", "-45", "SKIP"]) as answer,
            patch("support.genie_live.last_board_heights", return_value={}),
            patch("support.genie_live.automatic_recording") as connect,
            patch("support.genie_live.run_live") as run,
        ):
            def launch(_folder):
                self.assertEqual(answer.call_count, 8)
                return nullcontext(recording)

            connect.side_effect = launch
            genie.main()
        self.assertEqual(run.call_args.kwargs["actual_distance"], 1.0)
        self.assertEqual(run.call_args.kwargs["conditions"],
                         {"initiator_height_m": 1.5, "reflector_height_m": 1.6,
                          "initiator_yaw_deg": 45.0, "reflector_yaw_deg": -45.0})
        self.assertIs(run.call_args.kwargs["recording"], recording)

    def test_yaw_prompts_allow_skips_and_preserve_zero(self):
        out = self.root / "live"
        with patch("builtins.input", side_effect=["skip", "", "", "0", "skip", ""]):
            distance, conditions = prompt_trial_details(out)
        self.assertIsNone(distance)
        self.assertEqual(conditions, {"initiator_yaw_deg": 0.0})
        with patch("builtins.input", side_effect=["skip"] * 6):
            self.assertEqual(prompt_trial_details(out), (None, {}))

    def test_play_button_can_select_guided_calibration(self):
        with (
            patch.object(sys, "argv", ["analyzer.py"]),
            patch("builtins.input", return_value="2"),
            patch("support.genie_calibration.run_guided") as guided,
            patch("support.genie_live.automatic_recording") as single,
        ):
            genie.main()
        guided.assert_called_once()
        self.assertTrue(guided.call_args.kwargs["plot"])
        single.assert_not_called()

    def test_guided_command_passes_paths_and_plot_option(self):
        out = self.root / "live"
        with (
            patch.object(sys, "argv", ["analyzer.py", "guided", "--folder", str(self.root),
                                       "--out", str(out), "--no-plot"]),
            patch("support.genie_calibration.run_guided") as guided,
        ):
            genie.main()
        guided.assert_called_once_with(str(self.root), str(out), plot=False, interval=0.5)

    def test_live_estimate_target_reaches_normal_entry_point(self):
        with (
            patch.object(sys, "argv", ["analyzer.py", "live", str(self.path), "--estimates", "100"]),
            patch("support.genie_live.run_live") as live,
        ):
            genie.main()
        self.assertEqual(live.call_args.kwargs["max_estimates"], 100)

    def test_invalid_estimate_target_is_rejected_before_connecting(self):
        with (
            patch.object(sys, "argv", ["analyzer.py", "live", "--auto-record", "--estimates", "0"]),
            patch("support.genie_live.automatic_recording") as connect,
            patch("sys.stderr"),
        ):
            with self.assertRaises(SystemExit) as stopped:
                genie.main()
        self.assertEqual(stopped.exception.code, 2)
        connect.assert_not_called()

    def test_height_reuse_skips_trials_without_heights_and_allows_edits(self):
        out = self.root / "live"
        original = {"initiator_height_m": 1.5, "reflector_height_m": 1.6}

        def save_trial(name, conditions, timestamp):
            capture = out / name / "capture"
            capture.mkdir(parents=True)
            summary = capture / "summary.json"
            summary.write_text(json.dumps({"conditions": conditions}), encoding="utf-8")
            os.utime(summary, (timestamp, timestamp))

        save_trial("old", original, 1_700_000_000)
        save_trial("skipped", {"reflector_yaw_deg": 45.0}, 1_700_000_001)
        for choice in ("", "no"):
            with patch("builtins.input", side_effect=["skip", choice, "skip", "skip", "skip"]) as answer:
                self.assertEqual(prompt_trial_details(out), (None, original))
                self.assertIn("Initiator 1.5 m, Reflector 1.6 m", answer.call_args_list[1].args[0])
        with patch("builtins.input", side_effect=["skip"] * 5):
            self.assertEqual(prompt_trial_details(out), (None, {}))

        with patch("builtins.input", side_effect=["skip", "yes", "", "-1", "1.8", "skip", "skip", "skip"]):
            _, changed = prompt_trial_details(out)
        self.assertEqual(changed, {"initiator_height_m": 1.5, "reflector_height_m": 1.8})
        save_trial("changed", changed, 1_700_000_002)
        with patch("builtins.input", side_effect=["skip", "", "skip", "skip", "skip"]):
            self.assertEqual(prompt_trial_details(out), (None, changed))

    def test_saved_plots_use_prompted_conditions_and_tape_distance(self):
        from matplotlib.figure import Figure

        original_savefig = Figure.savefig
        conditions = {"initiator_yaw_deg": 45.0, "reflector_yaw_deg": -45.0,
                      "initiator_height_m": 1.5, "reflector_height_m": 1.6}
        self.path.write_text(json.dumps(self.event) + "\n", encoding="utf-8")

        def savefig(fig, file, **kwargs):
            title = fig.axes[0].get_title()
            self.assertIn("Initiator yaw = +45\N{DEGREE SIGN}", title)
            self.assertIn("Reflector yaw = -45\N{DEGREE SIGN}", title)
            self.assertIn("Initiator height = 1.5 m", title)
            self.assertIn("Reflector height = 1.6 m", title)
            reference, = [line for line in fig.axes[0].lines if line.get_label() == "Tape distance (1 m)"]
            self.assertEqual(reference.get_color(), "#b63131")
            self.assertEqual(reference.get_linestyle(), ":")
            values = reference.get_ydata() if "order" in file.name else reference.get_xdata()
            self.assertEqual(list(values), [1.0, 1.0])
            return original_savefig(fig, file, **kwargs)

        with patch.object(Figure, "savefig", savefig):
            session = run_live(self.path, out=self.root / "live", plot=False,
                               duration=0.05, interval=0.01, actual_distance=1.0, conditions=conditions)
        summary = json.loads((session / "capture" / "summary.json").read_text())
        self.assertEqual(summary["conditions"], conditions)
        self.assertEqual(summary["actual_distance_m"], 1.0)

    def test_zero_and_error_metrics_preserve_raw_and_do_not_claim_success(self):
        raw = (json.dumps(self.event) + "\n").encode()
        self.swap.write_bytes(raw)
        session = run_live(self.path, out=self.root / "live", plot=False,
                           duration=0.03, interval=0.01, actual_distance=1)
        capture = session / "capture"
        self.assertEqual((capture / "raw.jsonl").read_bytes(), raw)
        self.assertEqual({path.name for path in capture.iterdir()},
                         {"raw.jsonl", "measurements.csv", "summary.json"})
        self.assertEqual({path.name for path in session.iterdir()},
                         {"capture", "measurements.xlsx", "readings_in_order.png",
                          "filtered_distance_distribution.png"})
        summary = json.loads((capture / "summary.json").read_text())
        self.assertEqual(summary["records"], 1)
        self.assertEqual(summary["finite_reported_distance_count"], 1)
        self.assertEqual(summary["mean_reported_distance_m"], 0)
        self.assertEqual(summary["mae_m"], 1)
        self.assertEqual(summary["rmse_m"], 1)
        self.assertNotIn("successful_measurement_pct", summary)

    def test_invalid_json_is_archived_before_error(self):
        raw = b"bad json\n" + (json.dumps(self.event) + "\n").encode()
        self.swap.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "Invalid JSON"):
            run_live(self.path, out=self.root / "live", plot=False, duration=0.02, interval=0.01)
        capture = next((self.root / "live").glob("*/capture/raw.jsonl"))
        self.assertEqual(capture.read_bytes(), raw)

    def test_automatic_stall_exports_readings_including_the_final_batch(self):
        raw = (json.dumps(self.event) + "\n").encode()
        self.swap.write_bytes(raw)
        recording = Mock(path=self.path)
        recording.running.return_value = False

        def finish():
            with self.swap.open("ab") as handle:
                handle.write(raw)
            self.swap.rename(self.path)
            return "no_readings"

        recording.stop.side_effect = finish
        session = run_live(out=self.root / "live", plot=False, recording=recording)
        recording.stop.assert_called_once()
        capture = session / "capture"
        self.assertEqual((capture / "raw.jsonl").read_bytes(), raw * 2)
        summary = json.loads((capture / "summary.json").read_text())
        self.assertEqual(summary["records"], 2)
        self.assertEqual(summary["stop_reason"], "no_readings")
        self.assertTrue((session / "measurements.xlsx").exists())
        self.assertTrue((session / "readings_in_order.png").exists())
        self.assertTrue((session / "filtered_distance_distribution.png").exists())

    def test_target_count_keeps_analyzer_interruption_reason(self):
        raw = (json.dumps(self.event) + "\n").encode() * 100
        self.path.write_bytes(raw)
        recording = Mock(path=self.path)
        recording.running.return_value = False
        recording.stop.return_value = "analyzer_interrupted"
        with patch("support.genie_live.write_measurements_xlsx", return_value=None):
            session = run_live(out=self.root / "live", plot=False, recording=recording,
                               max_estimates=100, interval=0.001)
        summary = json.loads((session / "capture" / "summary.json").read_text())
        self.assertEqual(summary["finite_filtered_estimate_count"], 100)
        self.assertEqual(summary["stop_reason"], "analyzer_interrupted")

    def test_target_count_with_partial_tail_retains_incomplete_status(self):
        raw = (json.dumps(self.event) + "\n").encode() * 100
        tail = b'{"unfinished":'
        self.path.write_bytes(raw + tail)
        recording = Mock(path=self.path)
        recording.running.return_value = False
        recording.stop.return_value = "target_reached"
        with patch("support.genie_live.write_measurements_xlsx", return_value=None):
            session = run_live(out=self.root / "live", plot=False, recording=recording,
                               max_estimates=100, interval=0.001)
        capture = session / "capture"
        self.assertEqual((capture / "raw.jsonl").read_bytes(), raw)
        self.assertEqual((capture / "partial_line.bin").read_bytes(), tail)
        summary = json.loads((capture / "summary.json").read_text())
        self.assertEqual(summary["stop_reason"], "partial_line")

    def test_keyboard_interrupt_marks_saved_trial_for_guided_session_shutdown(self):
        self.path.write_text(json.dumps(self.event) + "\n", encoding="utf-8")
        original_poll = RecordingReader.poll
        polls = 0

        def interrupt_after_read(reader):
            nonlocal polls
            polls += 1
            if polls == 2:
                raise KeyboardInterrupt
            return original_poll(reader)

        recording = Mock(path=self.path)
        recording.running.return_value = True
        recording.stop.return_value = "stopped"
        with (
            patch.object(RecordingReader, "poll", interrupt_after_read),
            patch("support.genie_live.write_measurements_xlsx", return_value=None),
        ):
            session = run_live(out=self.root / "live", plot=False, recording=recording,
                               max_estimates=100, interval=0.001)
        summary = json.loads((session / "capture" / "summary.json").read_text())
        self.assertTrue(summary["user_stopped"])
        self.assertFalse(summary["target_reached"])
        recording.stop.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Analyzer recycling uses Windows")
    def test_finalized_duplicate_is_recycled_after_reports_are_saved(self):
        raw = (json.dumps(self.event) + "\n").encode()
        self.path.write_bytes(raw)

        def recycle(path):
            self.assertEqual(path.read_bytes(), raw)
            self.assertTrue((path.parent.parent / "measurements.xlsx").is_file())
            self.assertTrue((path.parent.parent / "readings_in_order.png").is_file())
            self.assertTrue((path.parent.parent / "filtered_distance_distribution.png").is_file())
            path.unlink()

        with patch("support.genie_live.PROJECT_DATA", self.root), patch("support.genie_live.recycle_file", side_effect=recycle) as cleanup:
            session = run_live(self.path, out=self.root / "live", plot=False,
                               duration=0.05, interval=0.01)
        cleanup.assert_called_once()
        self.assertFalse(self.path.exists())
        self.assertEqual((session / "capture" / "raw.jsonl").read_bytes(), raw)

    @unittest.skipUnless(os.name == "nt", "Analyzer recycling uses Windows")
    def test_active_and_longer_recordings_are_kept(self):
        saved = self.root / "raw.jsonl"
        saved.write_bytes(b'{"reading":1}\n')
        source = self.path.resolve()
        with patch("support.genie_live.PROJECT_DATA", self.root), patch("support.genie_live.recycle_file") as cleanup:
            source.write_bytes(saved.read_bytes())
            self.swap.touch()
            recycle_duplicate_recording(source, saved)
            self.assertTrue(source.exists())

            self.swap.unlink()
            extra = saved.read_bytes() + b'{"reading":2}\n'
            source.write_bytes(extra)
            recycle_duplicate_recording(source, saved)
            self.assertEqual(source.read_bytes(), extra)
        cleanup.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Analyzer recycling uses Windows")
    def test_recycle_failure_restores_original(self):
        capture = self.root / "capture"
        capture.mkdir()
        saved = capture / "raw.jsonl"
        raw = b'{"reading":1}\n'
        saved.write_bytes(raw)
        self.path.write_bytes(raw)
        with patch("support.genie_live.PROJECT_DATA", self.root), patch("support.genie_live.recycle_file", side_effect=OSError("failed")):
            recycle_duplicate_recording(self.path.resolve(), saved)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse((capture / self.path.name).exists())

class FirmwareModeTests(unittest.TestCase):
    def setUp(self):
        from flash import PROJECTS

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.firmware = Path(self.temp.name)
        self.headers = []
        for index, project in enumerate(PROJECTS.values()):
            config = self.firmware / project / "config"
            config.mkdir(parents=True)
            definitions = {
                "sl_rail_util_cs_antenna_offset_config.h": {
                    "GENIE_TEST_ANTENNA": "1",
                    "GENIE_ANT1_WIRELESS_OFFSET_CM": str(270 + index),
                    "GENIE_ANT2_WIRELESS_OFFSET_CM": "67",
                    "GENIE_ANT1_WIRED_OFFSET_CM": "25",
                    "GENIE_ANT2_WIRED_OFFSET_CM": "26",
                    "SL_RAIL_UTIL_CS_ANTENNA_COUNT": "1",
                    "SL_RAIL_UTIL_CS_ANTENNA_OFFSET_WIRELESS_CM": "{ GENIE_SELECTED_WIRELESS_OFFSET_CM, 0, 0, 0 }",
                    "SL_RAIL_UTIL_CS_ANTENNA_OFFSET_WIRED_CM": "{ GENIE_SELECTED_WIRED_OFFSET_CM, 0, 0, 0 }",
                },
                "sl_rail_util_aox_config.h": {
                    "SL_RAIL_UTIL_AOX_ANTENNA_PIN_COUNT": "0",
                    "SL_RAIL_UTIL_AOX_ANTENNA_PIN0_PORT": "SL_GPIO_PORT_A",
                    "SL_RAIL_UTIL_AOX_ANTENNA_PIN0_PIN": "0",
                },
                "cs_reflector_config.h": {"CS_REFLECTOR_CS_SYNC_ANTENNA": "CS_SYNC_ANTENNA_1"},
            }
            if project == PROJECTS["initiator"]:
                definitions["cs_initiator_config.h"] = {
                    "CS_INITIATOR_DEFAULT_CS_TONE_ANTENNA_CONFIG_IDX_REQ": "CS_ANTENNA_CONFIG_INDEX_SINGLE_ONLY",
                    "CS_INITIATOR_DEFAULT_CS_SYNC_ANTENNA_REQ": "CS_SYNC_ANTENNA_1",
                    "CS_INITIATOR_DEFAULT_PREFERRED_PEER_ANTENNA": "1",
                }
            for filename, values in definitions.items():
                header = config / filename
                header.write_bytes("".join(f"#define {key} {value}\r\n" for key, value in values.items()).encode())
                self.headers.append(header)

    def test_mode_round_trip_preserves_offsets_and_line_endings(self):
        from calibration import configure_mode

        original = {path: path.read_bytes() for path in self.headers}
        configure_mode("normal", firmware=self.firmware)
        for path in self.headers:
            source = path.read_text()
            if path.name == "sl_rail_util_cs_antenna_offset_config.h":
                self.assertIn("#define SL_RAIL_UTIL_CS_ANTENNA_COUNT 2", source)
                self.assertIn("{ GENIE_ANT1_WIRELESS_OFFSET_CM, GENIE_ANT2_WIRELESS_OFFSET_CM, 0, 0 }", source)
            if path.name == "sl_rail_util_aox_config.h":
                self.assertIn("#define SL_RAIL_UTIL_AOX_ANTENNA_PIN_COUNT 1", source)
            if path.name == "cs_reflector_config.h":
                self.assertIn("CS_SYNC_SWITCHING", source)
            if path.name == "cs_initiator_config.h":
                self.assertIn("CS_ANTENNA_CONFIG_INDEX_DUAL_ONLY", source)
                self.assertIn("#define CS_INITIATOR_DEFAULT_PREFERRED_PEER_ANTENNA 3", source)
        self.assertEqual(configure_mode("normal", firmware=self.firmware), [])
        configure_mode("calibration", antenna=2, firmware=self.firmware)
        for path in self.headers:
            if path.name == "sl_rail_util_cs_antenna_offset_config.h":
                self.assertIn("#define GENIE_TEST_ANTENNA 2", path.read_text())
                self.assertIn("{ GENIE_SELECTED_WIRELESS_OFFSET_CM, 0, 0, 0 }", path.read_text())
        configure_mode("calibration", antenna=1, firmware=self.firmware)
        self.assertEqual({path: path.read_bytes() for path in self.headers}, original)

    def test_missing_macro_does_not_change_other_headers(self):
        from calibration import configure_mode

        malformed = self.headers[-1]
        malformed.write_bytes(b"// configuration definition missing\r\n")
        original = {path: path.read_bytes() for path in self.headers}
        with self.assertRaises(ValueError):
            configure_mode("normal", firmware=self.firmware)
        self.assertEqual({path: path.read_bytes() for path in self.headers}, original)

    def test_failed_write_rolls_back_completed_changes(self):
        from calibration import configure_mode, write_header

        original = {path: path.read_bytes() for path in self.headers}
        writes = 0

        def fail_second_write(path, contents):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise PermissionError("simulated locked header")
            write_header(path, contents)

        with patch("calibration.write_header", side_effect=fail_second_write):
            with self.assertRaises(PermissionError):
                configure_mode("normal", firmware=self.firmware)
        self.assertEqual({path: path.read_bytes() for path in self.headers}, original)

    def test_concurrent_switch_is_rejected(self):
        from calibration import configuration_lock, configure_mode

        original = {path: path.read_bytes() for path in self.headers}
        with configuration_lock(self.firmware):
            with self.assertRaises((OSError, RuntimeError)):
                configure_mode("normal", firmware=self.firmware)
        self.assertEqual({path: path.read_bytes() for path in self.headers}, original)

    def test_continued_array_definition_is_replaced_completely(self):
        from calibration import replace_defines

        source = "#define OFFSET \\\r\n  { 1, 2, 0, 0 }\r\n#define OTHER 3\r\n"
        updated = replace_defines(source, {"OFFSET": "{ 4, 5, 0, 0 }"}, "header.h")
        self.assertEqual(updated, "#define OFFSET { 4, 5, 0, 0 }\r\n#define OTHER 3\r\n")


class RegressionOffsetTests(unittest.TestCase):
    def test_same_distance_trials_produce_an_offset_estimate(self):
        from linearreg import (ANTENNA_PAIRS, USED_OFFSETS, Run, distance_fit,
                               recommended_offsets, regression_data)

        runs = [Run(Path(str(index)), 1.0, (index * 45, 0)) for index in range(5)]
        medians = [1.1, 1.2, 1.3, 1.4, 10.0]
        pairs = {("initiator", "reflector")}
        with patch("linearreg.run_measurement", side_effect=[(value, pairs) for value in medians]):
            actual, measured, board_pairs, skipped = regression_data(runs)
        self.assertEqual(actual, [1.0] * 5)
        self.assertEqual(measured, medians)
        self.assertFalse(skipped)
        fit = distance_fit(actual, measured)
        self.assertEqual(fit.slope, 1.0)
        self.assertAlmostEqual(fit.intercept, 0.3)
        old = ((56, 67, 0, 0), (56, 67, 0, 0))
        updated, _ = recommended_offsets(old, ANTENNA_PAIRS[0], USED_OFFSETS[1],
                                         actual, fit, board_pairs)
        self.assertEqual(updated, ((71, 67, 0, 0), (71, 67, 0, 0)))
        blocked, _ = recommended_offsets(old, ANTENNA_PAIRS[0], USED_OFFSETS[0],
                                         actual, fit, board_pairs)
        self.assertIsNone(blocked)

    def test_multiple_distances_keep_equal_orientation_weighting(self):
        from linearreg import Run, distance_fit, regression_data

        runs = [Run(Path(str(index)), 1.0, (0, 0)) for index in range(3)]
        runs += [Run(Path("turned"), 1.0, (45, 0)), Run(Path("farther"), 2.0, (0, 0))]
        pairs = {("initiator", "reflector")}
        medians = [1.1, 1.2, 1.3, 1.9, 2.4]
        with patch("linearreg.run_measurement", side_effect=[(value, pairs) for value in medians]):
            actual, measured, _, skipped = regression_data(runs)
        self.assertEqual(actual, [1.0, 2.0])
        self.assertAlmostEqual(measured[0], 1.55)
        self.assertAlmostEqual(measured[1], 2.4)
        self.assertFalse(skipped)
        fit = distance_fit(actual, measured)
        self.assertAlmostEqual(fit.slope, 0.85)
        self.assertAlmostEqual(fit.intercept, 0.70)

    def test_same_distance_negative_bias_and_zero_baseline(self):
        from linearreg import ANTENNA_PAIRS, USED_OFFSETS, distance_fit, recommended_offsets

        actual = [5.0] * 5
        fit = distance_fit(actual, [4.6, 4.8, 4.9, 5.0, 5.1])
        old = ((56, 67, 0, 0), (60, 70, 0, 0))
        pairs = {("initiator", "reflector")}
        updated, _ = recommended_offsets(old, ANTENNA_PAIRS[1], USED_OFFSETS[2],
                                         actual, fit, pairs)
        self.assertEqual(updated, ((56, -5, 0, 0), (60, -5, 0, 0)))
        blocked, _ = recommended_offsets(old, ANTENNA_PAIRS[1], USED_OFFSETS[2],
                                         actual, fit, pairs | {("another", "pair")})
        self.assertIsNone(blocked)

    def test_two_distinct_distances_allow_an_offset_estimate(self):
        from statistics import linear_regression
        from linearreg import ANTENNA_PAIRS, USED_OFFSETS, recommended_offsets

        old = ((56, 67, 0, 0), (56, 67, 0, 0))
        actual = [1, 2]
        fit = linear_regression(actual, [1.2, 2.2])
        updated, _ = recommended_offsets(old, ANTENNA_PAIRS[0], USED_OFFSETS[1],
                                         actual, fit, {("initiator", "reflector")})
        self.assertEqual(updated, ((66, 67, 0, 0), (66, 67, 0, 0)))

    def test_physical_and_legacy_offset_headers(self):
        from linearreg import BOARD_PROJECTS, OFFSET_HEADER, project_offsets

        headers = (
            "#define SL_RAIL_UTIL_CS_ANTENNA_OFFSET_WIRELESS_CM { 56, 67, 0, 0 }\n",
            "#define GENIE_ANT1_WIRELESS_OFFSET_CM 56\n"
            "#define GENIE_ANT2_WIRELESS_OFFSET_CM 67\n"
            "#define SL_RAIL_UTIL_CS_ANTENNA_OFFSET_WIRELESS_CM "
            "{ GENIE_SELECTED_WIRELESS_OFFSET_CM, 0, 0, 0 }\n",
        )
        with tempfile.TemporaryDirectory() as folder:
            for source in headers:
                for project in BOARD_PROJECTS:
                    header = Path(folder) / project / OFFSET_HEADER
                    header.parent.mkdir(parents=True, exist_ok=True)
                    header.write_text(source, encoding="utf-8")
                self.assertEqual(project_offsets(Path(folder)),
                                 ((56, 67, 0, 0), (56, 67, 0, 0)))


if __name__ == "__main__":
    unittest.main()
