"""All captures in these tests are SYNTHETIC, not an Analyzer schema or lab data."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import analyzer as g


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.meta = dict.fromkeys(g.METADATA, "")
        self.meta.update(run_id="SYNTHETIC-ONLY", trial_number="1", actual_distance_m="1",
                         accuracy_tolerance_m="0.25", actual_duration_s="10",
                         duration_evidence="Synthetic fixture explicit 10 s window",
                         reflector_id="synthetic-peer", condition_id="synthetic-baseline",
                         run_purpose="evaluation", attempt_accounting="attempt_events_complete",
                         attempt_accounting_evidence="Synthetic fixture emits one start per attempt",
                         connection_events_complete="true", connection_events_evidence="Synthetic full event stream",
                         initial_connection_state="disconnected", startup_capture_complete="true",
                         startup_capture_evidence="Synthetic fixture captures the complete startup")
        self.events = [
            {"tag": "P", "tick": 0}, {"tag": "O", "tick": .1},
            {"tag": "Q", "tick": .2, "req": "q1"},
            {"tag": "A", "tick": .25, "attempt": "a1"},
            {"tag": "M", "tick": .3, "attempt": "a1", "req": "q1", "range": 120, "state": "OK", "score": .9, "signal": -60},
            {"tag": "D", "tick": .4, "range": 120},
            {"tag": "A", "tick": 1, "attempt": "a2"},
            {"tag": "M", "tick": 1.2, "attempt": "a2", "range": 80, "state": "OK"},
            {"tag": "A", "tick": 1.5, "attempt": "a3"},
            {"tag": "M", "tick": 1.8, "attempt": "a3", "state": "FAIL", "err": "synthetic-error"},
            {"tag": "A", "tick": 2, "attempt": "a4"},
            {"tag": "M", "tick": 2.3, "attempt": "a4", "range": 100, "state": "OK"},
            {"tag": "A", "tick": 3, "attempt": "a5"},
            {"tag": "M", "tick": 3.2, "attempt": "a5", "range": 0, "state": "OK"},
            {"tag": "C", "tick": 4}, {"tag": "O", "tick": 5}, {"tag": "C", "tick": 8},
        ]
        fields = {
            "elapsed_s": {"path": "/tick", "unit": "s", "origin": "recording_start"},
            "reflector_id": {"constant": "synthetic-peer"},
            "connection_id": {"constant": "synthetic-unique-session"},
        }
        rules = {}
        for tag, kind in {"P": "power_on", "O": "connection_open", "C": "connection_closed",
                          "Q": "request", "A": "attempt", "M": "measurement", "D": "display"}.items():
            ff = copy.deepcopy(fields)
            if kind in {"attempt", "measurement"}:
                ff["attempt_id"] = {"path": "/attempt"}
            if kind in {"request", "measurement"}:
                ff["request_id"] = {"path": "/req"}
            if kind == "measurement":
                ff.update(cs_distance_m={"path": "/range", "unit": "cm"},
                          status={"path": "/state", "values": {"OK": "valid", "FAIL": "failed"}},
                          quality_score={"path": "/score", "scale": "SYNTHETIC ONLY: 0..1"},
                          rssi_dbm={"path": "/signal", "unit": "dBm"}, error_code={"path": "/err"})
            rules[tag] = {"kind": kind, "fields": ff}
        self.mapping = {"reviewed": True, "evidence": "SYNTHETIC ONLY; defined by this test generator",
                        "clock_id": "synthetic-monotonic", "event_type_path": "/tag", "events": rules}
        self.source, self.mp, self.metadata_path = self.root / "synthetic.jsonl", self.root / "mapping.json", self.root / "metadata.csv"

    def save(self):
        self.source.write_text("".join(json.dumps(e) + "\n" for e in self.events), encoding="utf-8")
        g.write_json(self.mp, self.mapping)
        g.write_csv(self.metadata_path, g.METADATA, [self.meta])

    def imported(self):
        self.save()
        g.import_log(self.source, self.metadata_path, self.meta["run_id"], self.mp, self.root / "imported")
        return g.read_csv(self.root / "imported" / "measurements.csv")

    def test_known_statistics_zero_missing_and_display(self):
        rows = self.imported()
        s, dr = g.analyze_run(self.meta, rows, {})
        self.assertEqual(s["valid_distance_count"], 4)
        self.assertEqual(s["attempted_measurement_count"], 5)
        self.assertEqual(s["failed_or_invalid_count"], 1)
        self.assertEqual(s["successful_measurement_pct"], 80)
        self.assertEqual(s["valid_estimates_within_tolerance_pct"], 75)
        self.assertAlmostEqual(s["mean_signed_error_m"], -.25)
        self.assertAlmostEqual(s["mean_absolute_error_m"], .35)
        self.assertAlmostEqual(s["rmse_m"], math.sqrt(1.08 / 4))
        self.assertAlmostEqual(s["p95_absolute_error_m"], .88)
        self.assertAlmostEqual(s["sample_stddev_distance_m"], math.sqrt(.83 / 3))
        self.assertAlmostEqual(s["inter_estimate_median_s"], .9)
        self.assertAlmostEqual(s["inter_estimate_p95_s"], 1.08)
        self.assertAlmostEqual(s["inter_estimate_max_s"], 1.1)
        self.assertAlmostEqual(s["valid_estimate_rate_hz"], .4)
        self.assertAlmostEqual(s["power_on_to_first_valid_s"], .3)
        self.assertAlmostEqual(s["request_to_result_median_s"], .1)
        self.assertEqual(s["disconnect_count"], 2)
        self.assertAlmostEqual(s["disconnected_duration_s"], 3.1)
        failed = next(r for r in dr if r["status"] == "failed")
        self.assertEqual(failed["cs_distance_m"], "")
        self.assertEqual(failed["error_code"], "synthetic-error")
        self.assertTrue(any(r["cs_distance_m"] == "0.0" and r["included_in_accuracy"] == "true" for r in dr))
        self.assertTrue(all(r["rssi_distance_m"] == "" for r in dr))
        self.assertEqual(next(r["rssi_dbm"] for r in dr if r["rssi_dbm"]), "-60.0")

    def test_no_attempt_denominator_from_quality_or_results(self):
        self.meta["attempt_accounting"] = ""
        s, _ = g.analyze_run(self.meta, self.imported(), {})
        self.assertIsNone(s["attempted_measurement_count"])
        self.assertIsNone(s["successful_measurement_pct"])
        self.assertEqual(s["observed_failed_result_count"], 1)

    def test_exclusions_do_not_change_reliability(self):
        rows = self.imported()
        r = next(r for r in rows if r["cs_distance_m"] == "1.2")
        key = (r["run_id"], r["source_sha256"], r["source_line"])
        s, _ = g.analyze_run(self.meta, rows, {key: "Synthetic movement exclusion"})
        self.assertEqual(s["valid_distance_count"], 3)
        self.assertEqual(s["successful_measurement_pct"], 80)

    def test_unresolved_attempts_not_failed(self):
        self.events.append({"tag": "A", "tick": 9, "attempt": "a6"})
        s, _ = g.analyze_run(self.meta, self.imported(), {})
        self.assertEqual(s["unresolved_attempt_count"], 1)
        self.assertEqual(s["failed_or_invalid_count"], 1)
        self.assertAlmostEqual(s["successful_measurement_pct"], 400 / 6)

    def test_ambiguous_attempt_ids_disable_success_rate(self):
        self.events.append({"tag": "A", "tick": 9, "attempt": "a1"})
        s, _ = g.analyze_run(self.meta, self.imported(), {})
        self.assertIsNone(s["successful_measurement_pct"])

    def test_no_window_no_invented_duration_or_rate(self):
        self.meta["actual_duration_s"] = ""
        s, _ = g.analyze_run(self.meta, self.imported(), {})
        self.assertIsNone(s["valid_estimate_rate_hz"])
        self.assertIsNone(s["disconnected_duration_s"])
        self.assertEqual(s["valid_distance_count"], 4)

    def test_startup_requires_complete_capture(self):
        self.meta["startup_capture_complete"] = ""
        s, _ = g.analyze_run(self.meta, self.imported(), {})
        self.assertIsNone(s["power_on_to_first_valid_s"])

    def test_schema_unreviewed_or_units_unknown_rejected(self):
        self.mapping["reviewed"] = False
        with self.assertRaisesRegex(ValueError, "reviewed"):
            self.imported()
        self.mapping["reviewed"] = True
        del self.mapping["events"]["M"]["fields"]["cs_distance_m"]["unit"]
        with self.assertRaisesRegex(ValueError, "distance unit"):
            self.imported()

    def test_inventory_and_empty_log(self):
        self.save()
        report = g.inspect_log(self.source, self.root / "inventory", "/tag")
        self.assertEqual(report["json_object_count"], len(self.events))
        self.assertEqual(report["event_counts"]['"M"'], 5)
        self.assertIn("/tick", report["fields"])
        self.source.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "empty"):
            g.inspect_log(self.source, self.root / "empty")

    def test_duplicate_import_rejected(self):
        self.imported()
        with self.assertRaisesRegex(ValueError, "already imported"):
            g.import_log(self.source, self.metadata_path, self.meta["run_id"], self.mp, self.root / "imported")

    def test_traceability_and_full_csv_exports(self):
        rows = self.imported()
        with patch.object(g, "plot_results"):
            summaries = g.analyze(self.metadata_path, self.root / "imported" / "measurements.csv", self.root / "report")
        self.assertEqual(summaries[0]["valid_distance_count"], 4)
        self.assertEqual(len(g.read_csv(self.root / "report" / "measurements.csv")), len(rows))
        self.assertTrue((self.root / "report" / "analysis_manifest.json").is_file())
        Path(rows[0]["source_file"]).write_text("altered\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            g.analyze(self.metadata_path, self.root / "imported" / "measurements.csv", self.root / "tampered_report")

    def test_plots_render(self):
        rows = self.imported()
        summary, derived = g.analyze_run(self.meta, rows, {})
        g.plot_results({self.meta["run_id"]: self.meta}, [summary], derived, self.root / "plots")
        self.assertEqual(len(list((self.root / "plots").glob("*.png"))), 3)
        self.assertEqual(len(list((self.root / "plots").glob("*.svg"))), 3)

    def test_init_plan_keeps_actual_values_blank_and_changes_one_factor(self):
        self.assertEqual(g.init(self.root / "plan", 3), 90)
        metadata = g.read_csv(self.root / "plan" / "run_metadata.csv")
        plan = g.read_csv(self.root / "plan" / "test_plan.csv")
        self.assertEqual({r["planned_distance_m"] for r in plan}, {"1", "2", "5", "10", "20"})
        self.assertTrue(all(not r["actual_distance_m"] and not r["configured_initiator_tx_power_dbm"] for r in metadata))
        by_condition = {r["condition_id"]: r for r in plan}
        for m in metadata:
            if m["condition_id"] == "baseline":
                continue
            r, b = by_condition[m["condition_id"]], by_condition[m["baseline_condition_id"]]
            differences = [k for k in ("planned_initiator_tx_power_dbm", "planned_reflector_tx_power_dbm", "planned_initiator_yaw_deg", "planned_obstacle") if r[k] != b[k]]
            self.assertEqual(len(differences), 1)

    def test_power_integration_and_no_active_only_battery_claim(self):
        m = dict.fromkeys(g.POWER_META, "")
        m.update(power_run_id="SYNTHETIC-POWER", run_id="SYNTHETIC-ONLY", board_role="initiator", board_id="fake",
                 instrument_model="SYNTHETIC-INSTRUMENT", sampling_settings="synthetic 1 Hz", measurement_location="synthetic board input",
                 peak_measurement_limits="Synthetic sampled peak only", window_start_s="0", window_end_s="2", battery_capacity_mah="1000")
        mp, sp = self.root / "pm.csv", self.root / "ps.csv"
        g.write_csv(mp, g.POWER_META, [m])
        g.write_csv(sp, g.POWER_SAMPLES, [dict(power_run_id=m["power_run_id"], time_s=t, voltage_v=3, current_a=i, state=s)
                    for t, i, s in [(0, .01, "active"), (1, .02, "sleep"), (2, 0, "sleep")]])
        row = g.power(mp, sp, self.root / "power")[0]
        self.assertAlmostEqual(row["average_current_a"], .0125)
        self.assertEqual(row["peak_sampled_current_a"], .02)
        self.assertEqual(row["average_voltage_v"], 3)
        self.assertIsNone(row["battery_life_days_estimate"])

    def test_calibration_uses_separate_captures_and_preserves_raw(self):
        rows = self.imported()
        _, analyzed = g.analyze_run(self.meta, rows, {})
        self.meta["run_purpose"] = "calibration"
        validation = dict(self.meta, run_id="SYNTHETIC-VALIDATE", run_purpose="validation")
        g.write_csv(self.metadata_path, g.METADATA, [self.meta, validation])
        separate_capture = self.root / "synthetic-validation.jsonl"
        separate_capture.write_text(self.source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        valid_rows = [dict(r, run_id=validation["run_id"], source_sha256=g.digest(separate_capture), source_file=str(separate_capture)) for r in analyzed]
        cp = self.root / "analyzed.csv"
        columns = g.MEASUREMENTS + ["included_in_accuracy", "exclusion_reason", "signed_error_m", "absolute_error_m", "request_to_result_latency_s"]
        g.write_csv(cp, columns, analyzed + valid_rows)
        before = g.digest(cp)
        model = g.calibrate(self.metadata_path, cp, [self.meta["run_id"]], [validation["run_id"]], self.root / "calibration")
        self.assertAlmostEqual(model["training_mean_signed_error_m"], -.25)
        self.assertEqual(before, g.digest(cp))
        overlap = [dict(r, source_sha256=rows[0]["source_sha256"], source_file=rows[0]["source_file"]) for r in valid_rows]
        g.write_csv(cp, columns, analyzed + overlap)
        with self.assertRaisesRegex(ValueError, "separate validation logs"):
            g.calibrate(self.metadata_path, cp, [self.meta["run_id"]], [validation["run_id"]], self.root / "bad-calibration")

    def test_invalid_numeric_and_timezone(self):
        for value in ("NaN", "inf", True):
            with self.assertRaises(ValueError):
                g.number(value)
        with self.assertRaisesRegex(ValueError, "Timezone"):
            g.utc("2026-11-10T10:00:00")
        self.assertIsNone(g.number(""))
        self.assertEqual(g.number("0"), 0)


if __name__ == "__main__":
    unittest.main()
