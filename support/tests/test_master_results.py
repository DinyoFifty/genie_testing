"""Evidence, statistics, refresh and missing-data checks without hardware."""

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

from openpyxl import load_workbook

from support.master_results import (attempt_metrics, attach_evidence, details, power_metrics,
    collect_results, overview_row, radio_profile, refresh_results, result_groups,
    save_context, save_details, select_profile, trial_row)
from support.results_layout import FIELDS, RESULT_COLUMNS, CONFIG_COLUMNS, OVERVIEW_COLUMNS, CHECKLIST_COLUMNS


class MasterResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.live = self.data / "live"

    def trial(self, name="1m_Ini0_Ref0", distance=1, yaw=0, *, mode="normal", simulated=False):
        folder = self.live / name
        capture = folder / "capture"
        capture.mkdir(parents=True)
        summary = {"actual_distance_m": distance, "session_started_at_utc": "2026-11-23T00:00:00Z",
                   "conditions": {"initiator_yaw_deg": 0, "reflector_yaw_deg": yaw},
                   "firmware": {"mode": mode, "fingerprint": "test-baseline", "projects": {}},
                   "board_pair": ["i", "r"], "simulated": simulated,
                   "target_reached": True, "reports_saved": True, "stop_reason": "target_reached"}
        (capture / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        records = [{"initiatorId": "i", "reflectorId": "r", "distanceMainRaw": distance + 0.2,
                    "distanceMain": distance - 1 if i % 2 else distance + 1} for i in range(100)]
        records += [{"initiatorId": "i", "reflectorId": "r", "distanceMain": value} for value in (None, True, "2")]
        (capture / "raw.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        with (capture / "measurements.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records[:100])
        return folder

    def evidence(self, name, contents):
        path = Path(self.temp.name) / name
        path.write_text(contents, encoding="utf-8")
        return path

    def test_statistics_include_zero_and_do_not_equate_finite_results_with_valid_attempts(self):
        row = trial_row(self.trial(), self.live)
        self.assertEqual(row["filtered_n"], 100)
        self.assertEqual(row["filtered_median_m"], 1)
        self.assertEqual(row["filtered_bias_m"], 0)
        self.assertEqual(row["filtered_mae_m"], 1)
        self.assertEqual(row["filtered_rmse_m"], 1)
        self.assertEqual(row["filtered_p95_error_m"], 1)
        self.assertEqual(row["unusable_results"], 3)
        self.assertEqual(row["observed_results"], 103)
        self.assertNotIn("valid_fraction", row)
        self.assertNotIn("failed", row)
        self.assertNotIn("mean_current_ma", row)

    def test_complete_ledger_counts_failures_and_request_result_latency(self):
        folder = self.trial()
        source = self.evidence("attempts.csv", "attempt_id,status,request_time_s,result_time_s,clock_id\na,valid,0,0.02,clock\nb,failed,,,\nc,valid,1,1.04,clock\n")
        original = source.read_bytes()
        attach_evidence(folder, source, "attempts", coverage_complete=True, note="Full radio ledger; request to usable result on one device clock")
        row = trial_row(folder, self.live)
        self.assertEqual(row["failed"], 1)
        self.assertEqual(row["attempts"], 3)
        self.assertAlmostEqual(row["valid_fraction"], 2 / 3)
        self.assertAlmostEqual(row["measured_latency_ms"], 30)
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue((folder / "capture" / details(folder)["attempts_file"]).exists())

    def test_incomplete_and_unresolved_ledgers_withhold_percentage(self):
        folder = self.trial()
        source = self.evidence("attempts.csv", "attempt_id,status\na,valid\nb,unresolved\n")
        for complete in (True, False):
            attach_evidence(folder, source, "attempts", coverage_complete=complete, note="Partial ledger")
            row = trial_row(folder, self.live)
            self.assertEqual(row["unresolved"], 1)
            self.assertNotIn("valid_fraction", row)

    def test_ledger_rejects_duplicate_ids_ambiguous_clocks_and_bad_times(self):
        for contents in ("attempt_id,status\na,valid\na,failed\n", "attempt_id,status\na,error\n",
                         "attempt_id,status\na\n", "attempt_id,status\n,valid\n",
                         "attempt_id,status,request_time_s,result_time_s,clock_id\na,valid,1,0,c\n",
                         "attempt_id,status,request_time_s,result_time_s,clock_id\na,valid,0,1,c\nb,valid,0,1,d\n"):
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                attempt_metrics(self.evidence("bad.csv", contents))

    def test_power_uses_time_weighted_current_and_explicit_cycle_count(self):
        source = self.evidence("power.csv", "time_s,current_ma,voltage_v\n0,1,3\n1,3,3\n3,5,3\n")
        metrics = power_metrics(source, 2)
        self.assertAlmostEqual(metrics["mean_current_ma"], 10 / 3)
        self.assertEqual(metrics["energy_mj"], 30)
        self.assertEqual(metrics["energy_per_cycle_mj"], 15)
        metrics = power_metrics(self.evidence("no-volts.csv", "time_s,current_ma\n0,0\n1,0\n"))
        self.assertEqual(metrics["mean_current_ma"], 0)
        self.assertIsNone(metrics["energy_mj"])
        with self.assertRaises(ValueError):
            power_metrics(self.evidence("backwards.csv", "time_s,current_ma\n1,1\n0,2\n"))

    def test_evidence_tampering_is_visible_and_does_not_silently_remove_warning(self):
        folder = self.trial()
        source = self.evidence("attempts.csv", "attempt_id,status\na,failed\n")
        attach_evidence(folder, source, "attempts", coverage_complete=True, note="Full ledger")
        (folder / "capture" / details(folder)["attempts_file"]).write_text("changed", encoding="utf-8")
        output, rows, warnings = refresh_results(self.data)
        self.assertEqual(rows, [])
        self.assertIn("changed after import", warnings[0])
        self.assertTrue(output.exists())

    def test_40_normal_conditions_keep_calibration_and_simulations_separate(self):
        for distance in (1, 2, 5, 10, 20):
            for yaw in range(0, 360, 45):
                self.trial(f"guided/Batch1/{distance}m_Ini0_Ref{yaw}", distance, yaw)
        self.trial("isolated", mode="isolated")
        self.trial("simulated", simulated=True)
        output, rows, warnings = refresh_results(self.data)
        self.assertEqual(len(rows), 42)
        self.assertEqual(warnings, [])
        workbook = load_workbook(output, data_only=True)
        self.addCleanup(workbook.close)
        self.assertEqual(workbook["Normal trials"].max_row, 43)
        self.assertEqual(workbook["Calibration"].max_row, 4)
        self.assertEqual(workbook["Simulated"].max_row, 4)
        self.assertEqual(workbook["Baseline checklist"].max_row, 43)
        self.assertTrue(all(row[5] == 1 for row in workbook["Baseline checklist"].iter_rows(min_row=4, values_only=True)))
        self.assertIsNone(workbook["Normal trials"].freeze_panes)

    def test_all_sheets_use_one_scrolling_area_without_stale_panes(self):
        self.trial()
        output, _, _ = refresh_results(self.data)
        workbook = load_workbook(output)
        self.addCleanup(workbook.close)
        for page in workbook:
            with self.subTest(sheet=page.title):
                self.assertIsNone(page.freeze_panes)
                self.assertIsNone(page.sheet_view.pane)
                self.assertEqual(page.sheet_view.topLeftCell, "A1")
                self.assertEqual([(s.pane, s.activeCell, s.sqref) for s in page.sheet_view.selection],
                                 [(None, "A4", "A4")])
                self.assertFalse(page.tables)
        with ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())
            for name in archive.namelist():
                if name.endswith(".xml"):
                    ElementTree.fromstring(archive.read(name))

    def test_refresh_removes_deleted_trials_and_never_overwrites_locked_workbook(self):
        folder = self.trial()
        output, _, _ = refresh_results(self.data)
        original = output.read_bytes()
        (folder / "capture/summary.json").unlink()
        with patch("support.master_results.os.replace", side_effect=PermissionError("Excel is open")):
            with self.assertRaises(PermissionError):
                refresh_results(self.data)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(list(output.parent.iterdir()), [output])
        _, rows, _ = refresh_results(self.data)
        self.assertEqual(rows, [])

    def test_local_profiles_context_and_evidence_requirements(self):
        self.assertEqual(radio_profile(self.data), "baseline")
        select_profile("accuracy", self.data)
        self.assertEqual(radio_profile(self.data), "accuracy")
        save_context({"environment": "garage LOS", "motion": "stationary"}, self.data)
        self.assertEqual(json.loads((self.data / "settings/test_context.json").read_text())["environment"], "garage LOS")
        folder = self.trial()
        with self.assertRaises(ValueError):
            save_details(folder, {"initiator_tx_dbm": 20})
        with self.assertRaises(ValueError):
            save_details(folder, {"measured_latency_ms": 10})
        save_details(folder, {"notes": "=1+1", "measured_latency_ms": 0,
                              "latency_definition": "request to result", "latency_evidence": "Instrument log"})
        output, _, _ = refresh_results(self.data)
        workbook = load_workbook(output)
        self.addCleanup(workbook.close)
        cell = next(cell for row in workbook["Configuration"] for cell in row if cell.value == "=1+1")
        self.assertEqual(cell.data_type, "s")

    def test_regression_rejects_mixed_runtime_profiles_and_changed_conditions(self):
        from linearreg import Run, calibration_baseline
        first = self.trial("first")
        second = self.trial("second")
        for folder, profile in ((first, "baseline"), (second, "accuracy")):
            (folder / "capture/analyzer_telemetry.jsonl").write_text(json.dumps({
                "kind": "configuration", "configuration": {"profile": profile}}) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "same Analyzer profile"):
            calibration_baseline([Run(first, 1, (0, 0)), Run(second, 2, (0, 0))])

    def test_guided_initiator_rotation_has_a_complete_checklist(self):
        folder = self.trial()
        path = folder / "capture/summary.json"
        summary = json.loads(path.read_text())
        summary["conditions"] = {"initiator_yaw_deg": 45, "reflector_yaw_deg": 90}
        summary["calibration"] = {"rotated_board": "initiator", "fixed_yaw_deg": 90}
        path.write_text(json.dumps(summary), encoding="utf-8")
        output, _, _ = refresh_results(self.data)
        workbook = load_workbook(output, data_only=True)
        self.addCleanup(workbook.close)
        matched = [row for row in workbook["Baseline checklist"].iter_rows(min_row=4, values_only=True) if row[3] == 1 and row[4] == 45]
        self.assertEqual(matched[0][5], 1)
        self.assertIn("Rotate initiator", matched[0][6])

    def test_fresh_teammate_gets_shared_headers_and_40_empty_planned_conditions(self):
        output, rows, warnings = refresh_results(self.data)
        self.assertEqual((rows, warnings), ([], []))
        workbook = load_workbook(output, data_only=True)
        self.addCleanup(workbook.close)
        self.assertEqual(workbook["Normal trials"].max_row, 3)
        self.assertEqual(workbook["Normal trials"]["A3"].value, "Trial")
        self.assertEqual(workbook["Baseline checklist"].max_row, 43)
        self.assertTrue(all(row[5] == 0 for row in workbook["Baseline checklist"].iter_rows(min_row=4, values_only=True)))
        self.assertTrue(all(row[0] is None for row in workbook["Baseline checklist"].iter_rows(min_row=4, values_only=True)))
        self.assertIn("No normal captures yet", workbook["Baseline checklist"]["A2"].value)
        self.assertEqual(workbook["Normal trials"]["A3"].fill.fgColor.rgb, "001F4E78")

    def test_calibration_setup_is_not_assigned_to_an_empty_normal_checklist(self):
        self.trial(mode="isolated")
        output, _, _ = refresh_results(self.data)
        workbook = load_workbook(output, data_only=True)
        self.addCleanup(workbook.close)
        self.assertEqual(workbook["Configuration"]["C4"].value, 1)
        self.assertTrue(all(row[0] is None for row in workbook["Baseline checklist"].iter_rows(min_row=4, values_only=True)))

    def test_overview_pools_readings_instead_of_averaging_trial_percentiles(self):
        self.trial("guided/Batch1/first")
        partial = self.trial("guided/Batch1/second", distance=2)
        path = partial / "capture/summary.json"
        summary = json.loads(path.read_text())
        summary.update(target_reached=False, stop_reason="duration")
        path.write_text(json.dumps(summary), encoding="utf-8")
        (partial / "capture/measurements.csv").write_text("distanceMain\n10\n", encoding="utf-8")
        rows, _ = collect_results(self.data)
        groups = result_groups(rows)
        self.assertEqual(len(groups), 1)
        overview = overview_row(groups[0])
        self.assertEqual(overview["filtered_n"], 101)
        self.assertEqual(overview["error_n"], 101)
        self.assertAlmostEqual(overview["filtered_mae_m"], 108 / 101)
        self.assertEqual(overview["filtered_p95_error_m"], 1)
        self.assertEqual(overview["complete_count"], 1)
        self.assertEqual(overview["condition_count"], 1)

    def test_changed_setups_and_simulations_stay_separate_inside_one_batch(self):
        self.trial("guided/Batch1/first")
        changed = self.trial("guided/Batch1/second")
        save_details(changed, {"environment": "Garage LOS"})
        self.trial("guided/Batch1/simulated", simulated=True)
        rows, _ = collect_results(self.data)
        groups = result_groups(rows)
        self.assertEqual(len(groups), 3)
        self.assertEqual(len({row["set"] for row in rows}), 3)
        self.assertEqual(sum(overview_row(group)["condition_count"] for group in groups), 3)

    def test_numeric_sort_orders_batches_distances_and_angles(self):
        for name, distance, yaw in (("guided/Batch10/1m", 1, 0), ("guided/Batch2/10m", 10, 0),
                                    ("guided/Batch2/2m_Ref135", 2, 135), ("guided/Batch2/2m_Ref45", 2, 45),
                                    ("guided/Batch2/1m", 1, 0)):
            self.trial(name, distance, yaw)
        rows, _ = collect_results(self.data)
        self.assertEqual([row["name"] for row in rows], ["1m", "2m_Ref45", "2m_Ref135", "10m", "1m"])
        self.assertEqual(rows[-1]["batch"], "Batch10")

    def test_definitions_cover_every_column_and_keep_unknowns_distinct_from_zero(self):
        folder = self.trial()
        save_details(folder, {"measured_latency_ms": 0, "latency_definition": "request to result", "latency_evidence": "Clock log"})
        output, rows, _ = refresh_results(self.data)
        self.assertEqual(rows[0]["latency_status"], "Measured")
        self.assertEqual(rows[0]["attempt_status"], "Missing")
        workbook = load_workbook(output, data_only=True)
        self.addCleanup(workbook.close)
        self.assertEqual(workbook.sheetnames[:2], ["Overview", "Definitions"])
        definitions = dict(workbook["Definitions"].iter_rows(min_row=4, values_only=True))
        for key, title in (*RESULT_COLUMNS, *CONFIG_COLUMNS, *OVERVIEW_COLUMNS, *CHECKLIST_COLUMNS):
            self.assertIn(title, definitions)
            self.assertLessEqual(len(FIELDS[key][1].split()), 20)
        for term in ("MAE", "RMSE", "SD", "P95", "Yaw", "LOS", "mA", "mJ", "dBm", "Blank cell"):
            self.assertIn(term, definitions)


if __name__ == "__main__":
    unittest.main()
