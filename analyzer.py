"""Collect and analyze traceable BLE measurements."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics as stats
import sys
import uuid

VERSION = "1.0.0"
DISTANCES = (1, 2, 5, 10, 20)
METADATA = [
    "run_id", "trial_number", "test_date", "test_location", "condition_id",
    "changed_variable", "baseline_condition_id", "run_purpose", "calibration_id",
    "initiator_id", "reflector_id", "initiator_board_model", "reflector_board_model",
    "initiator_board_revision", "reflector_board_revision",
    "initiator_firmware_name", "reflector_firmware_name",
    "initiator_firmware_version", "reflector_firmware_version", "sdk_version",
    "analyzer_version", "actual_distance_m", "initiator_reference_point",
    "reflector_reference_point", "initiator_height_m", "reflector_height_m",
    "orientation_reference_frame", "initiator_roll_deg", "initiator_pitch_deg",
    "initiator_yaw_deg", "reflector_roll_deg", "reflector_pitch_deg",
    "reflector_yaw_deg", "environment", "visibility", "obstacle_material",
    "obstacle_thickness_m", "obstacle_placement", "initiator_power_source",
    "reflector_power_source", "planned_initiator_tx_power_dbm",
    "planned_reflector_tx_power_dbm", "configured_initiator_tx_power_dbm",
    "configured_reflector_tx_power_dbm", "reported_initiator_tx_power_dbm",
    "reported_reflector_tx_power_dbm", "settings_verification_evidence",
    "unsupported_settings", "ranging_mode", "algorithm_mode", "channel_map",
    "channel_count", "antenna_configuration", "connection_interval_ms",
    "procedure_interval_ms", "filtering_settings", "calibration_settings",
    "planned_duration_s", "recording_start_utc", "recording_end_utc",
    "actual_duration_s", "duration_evidence", "accuracy_tolerance_m",
    "attempt_accounting", "attempt_accounting_evidence",
    "connection_events_complete", "connection_events_evidence",
    "initial_connection_state", "power_on_timestamp_utc", "power_on_evidence",
    "startup_capture_complete", "startup_capture_evidence",
    "observations",
]
MEASUREMENTS = [
    "run_id", "timestamp_utc", "elapsed_s", "clock_id", "source_timestamp_raw",
    "event_kind", "measurement_id", "procedure_id", "attempt_id", "request_id", "reflector_id",
    "connection_id", "cs_distance_m", "rtt_distance_m", "rssi_distance_m",
    "rssi_dbm", "quality_score", "quality_scale", "status", "error_code",
    "source_sha256", "source_file", "source_line", "mapping_sha256",
]
POWER_META = [
    "power_run_id", "run_id", "board_role", "board_id", "instrument_model",
    "instrument_serial", "sampling_settings", "sample_rate_hz",
    "instrument_bandwidth_hz", "current_range_a", "peak_measurement_limits",
    "measurement_location", "instrument_source_file", "instrument_source_sha256",
    "normalization_method", "window_start_s", "window_end_s",
    "measurement_frequency_hz", "state_classification_method",
    "battery_capacity_mah", "usable_capacity_fraction", "daily_active_hours",
    "daily_idle_hours", "daily_sleep_hours", "battery_life_assumptions",
]
POWER_SAMPLES = ["power_run_id", "time_s", "voltage_v", "current_a", "state"]
AUDIT = ["run_id", "source_sha256", "source_line", "reason"]
KINDS = {"measurement", "attempt", "request", "connection_open", "connection_closed",
         "power_on", "command", "diagnostic", "display", "unmapped"}
NUMERIC = {"elapsed_s", "cs_distance_m", "rtt_distance_m", "rssi_distance_m",
           "rssi_dbm", "quality_score"}
SCALES = {"m": 1, "cm": 0.01, "mm": 0.001, "s": 1, "ms": .001, "us": .000001}
REQUIRED_METADATA = [
    "trial_number", "test_date", "test_location", "condition_id", "run_purpose",
    "initiator_id", "reflector_id", "initiator_board_model", "reflector_board_model",
    "initiator_board_revision", "reflector_board_revision", "initiator_firmware_name",
    "reflector_firmware_name", "initiator_firmware_version", "reflector_firmware_version",
    "sdk_version", "analyzer_version", "actual_distance_m", "initiator_reference_point",
    "reflector_reference_point", "initiator_height_m", "reflector_height_m",
    "orientation_reference_frame", "initiator_roll_deg", "initiator_pitch_deg",
    "initiator_yaw_deg", "reflector_roll_deg", "reflector_pitch_deg", "reflector_yaw_deg",
    "environment", "visibility", "initiator_power_source", "reflector_power_source",
    "configured_initiator_tx_power_dbm", "configured_reflector_tx_power_dbm",
    "settings_verification_evidence", "ranging_mode", "algorithm_mode", "channel_map",
    "channel_count", "antenna_configuration", "connection_interval_ms", "procedure_interval_ms",
    "filtering_settings", "calibration_settings", "planned_duration_s", "duration_evidence",
    "accuracy_tolerance_m",
]


def number(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a numeric measurement")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Nonfinite numeric value: {value!r}; map documented sentinels to missing")
    return result


def utc(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Timezone required for timestamp {value!r}")
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, columns, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="raise")
        w.writeheader()
        w.writerows({k: "" if v is None else v for k, v in row.items()} for row in rows)


def write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def archive(path, folder):
    path, folder = Path(path), Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    sha = digest(path)
    dest = folder / f"{sha}_{path.name}"
    if not dest.exists():
        shutil.copyfile(path, dest)
    if digest(dest) != sha:
        raise ValueError("Archive hash mismatch")
    return sha, str(dest.resolve())


def json_lines(path):
    with Path(path).open(encoding="utf-8-sig") as f:
        for line, text in enumerate(f, 1):
            if text.strip():
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{path}:{line}: invalid JSON: {e.msg}") from e
                if not isinstance(obj, dict):
                    raise ValueError(f"{path}:{line}: expected JSON object")
                yield line, obj


def pointer(obj, path):
    if path == "":
        return obj
    if not path.startswith("/"):
        raise ValueError("Field paths must be JSON pointers beginning with /")
    for key in path[1:].split("/"):
        key = key.replace("~1", "/").replace("~0", "~")
        try:
            obj = obj[int(key)] if isinstance(obj, list) else obj[key]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return obj


def flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from flatten(value, prefix + "/" + key.replace("~", "~0").replace("/", "~1"))
    elif isinstance(obj, list):
        yield prefix, obj  # Arrays are inventoried without guessing subevent semantics.
    else:
        yield prefix, obj


def inspect_log(path, out, event_path=None):
    sha, archived = archive(path, Path(out) / "raw")
    fields, events, count = {}, Counter(), 0
    for line, obj in json_lines(path):
        count += 1
        if event_path:
            events[json.dumps(pointer(obj, event_path), sort_keys=True)] += 1
        for name, value in flatten(obj):
            entry = fields.setdefault(name, {"present": 0, "types": Counter(), "examples": []})
            entry["present"] += 1
            entry["types"][type(value).__name__] += 1
            example = json.dumps(value, ensure_ascii=True)
            if len(entry["examples"]) < 5 and example[:400] not in entry["examples"]:
                entry["examples"].append(example[:400])
    report = {"source_sha256": sha, "archived_file": archived, "json_object_count": count,
              "event_discriminator_path": event_path, "event_counts": dict(events), "fields": fields,
              "semantics": "Inventory only. Units, clock origin, event meaning, and attempt completeness are unverified."}
    write_json(Path(out) / "inspection.json", report)
    if count == 0:
        raise ValueError("Log is empty; archived it and recorded an empty inventory. No schema inferred.")
    return report


def init(folder, trials=3, duration=60):
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise ValueError("Choose an empty output directory; init does not overwrite existing data")
    folder.mkdir(parents=True, exist_ok=True)
    # Each alternative differs from baseline in one experimental factor.
    conditions = [
        ("baseline", "separation", "0", "0", "0", "none"),
        ("initiator_tx_10", "initiator_tx_power", "10", "0", "0", "none"),
        ("reflector_tx_10", "reflector_tx_power", "0", "10", "0", "none"),
        ("both_tx_10", "reflector_tx_power", "10", "10", "0", "none"),
        ("initiator_yaw_90", "initiator_yaw", "0", "0", "90", "none"),
        ("obstacle", "obstacle", "0", "0", "0", "TO_BE_SPECIFIED"),
    ]
    rows = []
    for condition, changed, ip, rp, yaw, obstacle in conditions:
        for distance in DISTANCES:
            for trial in range(1, trials + 1):
                row = dict.fromkeys(METADATA, "")
                row.update(run_id=f"genie-{uuid.uuid4().hex[:16]}", trial_number=trial,
                           condition_id=condition, changed_variable=changed,
                           baseline_condition_id="initiator_tx_10" if condition == "both_tx_10" else "baseline", run_purpose="evaluation",
                           initiator_board_model="BRD2606A", reflector_board_model="BRD2606A",
                           initiator_firmware_name="Bluetooth - NCP CS",
                           reflector_firmware_name="Bluetooth - SoC CS Reflector",
                           planned_initiator_tx_power_dbm=ip, planned_reflector_tx_power_dbm=rp,
                           planned_duration_s=duration,
                           observations="PLANNED ONLY: complete measured metadata before recording.")
                # Planned factor values are kept separate from actual run metadata.
                rows.append((row, distance, yaw, obstacle))
    write_csv(folder / "run_metadata.csv", METADATA, [r[0] for r in rows])
    write_csv(folder / "test_plan.csv", ["run_id", "condition_id", "trial_number",
              "planned_distance_m", "planned_initiator_tx_power_dbm",
              "planned_reflector_tx_power_dbm", "planned_initiator_yaw_deg", "planned_obstacle"],
              [dict(run_id=r["run_id"], condition_id=r["condition_id"], trial_number=r["trial_number"],
                    planned_distance_m=d, planned_initiator_tx_power_dbm=r["planned_initiator_tx_power_dbm"],
                    planned_reflector_tx_power_dbm=r["planned_reflector_tx_power_dbm"],
                    planned_initiator_yaw_deg=yaw, planned_obstacle=obstacle) for r, d, yaw, obstacle in rows])
    write_csv(folder / "measurements.csv", MEASUREMENTS, [])
    write_csv(folder / "exclusions.csv", AUDIT, [])
    write_csv(folder / "power_metadata.csv", POWER_META, [])
    write_csv(folder / "power_samples.csv", POWER_SAMPLES, [])
    write_json(folder / "analyzer_mapping.json", {
        "reviewed": False,
        "evidence": "REQUIRED: actual log inspection plus documentation for units and event semantics",
        "event_type_path": "", "clock_id": "", "events": {},
        "notes": "No Analyzer schema supplied. Do not enable until a real log is inspected. See README."
    })
    write_json(folder / "inspection_status.json", {
        "status": "pending_real_log", "reason": "User deferred actual log inspection. No Analyzer fields or units assumed."
    })
    return len(rows)


def load_metadata(path):
    rows = read_csv(path)
    result = {}
    for row in rows:
        rid = row.get("run_id")
        if not rid or rid in result:
            raise ValueError("Every metadata row needs a unique nonempty run_id")
        result[rid] = row
    return result


def convert(value, spec, field):
    if value is None or value == "" or value in spec.get("missing_values", []):
        return None
    unit = spec.get("unit")
    if field == "timestamp_utc":
        if unit == "iso8601":
            return iso(utc(value))
        if unit in {"unix_s", "unix_ms", "unix_us"}:
            factor = {"unix_s": 1, "unix_ms": .001, "unix_us": .000001}[unit]
            return iso(datetime.fromtimestamp(number(value) * factor, timezone.utc))
        raise ValueError("Timestamp needs explicit iso8601 or unix_s/ms/us unit")
    if field in NUMERIC:
        value = number(value)
        if field.endswith("distance_m") and unit not in {"m", "cm", "mm"}:
            raise ValueError(f"{field}: document source distance unit m/cm/mm")
        if field == "elapsed_s" and (unit not in {"s", "ms", "us"} or spec.get("origin") != "recording_start"):
            raise ValueError("Elapsed time needs s/ms/us unit and origin=recording_start")
        if field == "rssi_dbm" and unit != "dBm":
            raise ValueError("Actual RSSI needs documented dBm unit")
        if field == "quality_score" and not spec.get("scale"):
            raise ValueError("Quality requires a documented scale; never assume percentage")
        return value * SCALES.get(unit, 1)
    if field == "status":
        mapping = spec.get("values", {})
        key = str(value)
        if key not in mapping or mapping[key] not in {"valid", "invalid", "failed"}:
            raise ValueError(f"Unknown measurement status {value!r}; explicitly map its semantics")
        return mapping[key]
    return str(value)


def import_log(path, metadata_path, rid, mapping_path, out):
    meta = load_metadata(metadata_path)[rid]
    mapping = json.loads(Path(mapping_path).read_text(encoding="utf-8-sig"))
    if mapping.get("reviewed") is not True or not mapping.get("evidence") or not mapping.get("clock_id"):
        raise ValueError("Import requires a reviewed schema, semantic/unit evidence, and a clock_id")
    if not mapping.get("event_type_path") or not mapping.get("events"):
        raise ValueError("Explicit event discriminator and event rules required")
    out = Path(out)
    sha, archived = archive(path, out / "raw")
    msha, _ = archive(mapping_path, out / "mappings")
    archive(metadata_path, out / "metadata_snapshots")
    rows, counts = [], Counter()
    for line, obj in json_lines(path):
        event = pointer(obj, mapping["event_type_path"])
        rule = mapping["events"].get(str(event), {})
        kind = rule.get("kind", "unmapped")
        if kind not in KINDS:
            raise ValueError(f"Invalid event kind {kind!r}")
        row = dict.fromkeys(MEASUREMENTS, "")
        row.update(run_id=rid, event_kind=kind, clock_id=mapping["clock_id"],
                   source_sha256=sha, source_file=archived, source_line=line, mapping_sha256=msha)
        for field, spec in rule.get("fields", {}).items():
            if field not in MEASUREMENTS or field in {"run_id", "source_sha256", "source_file", "source_line", "mapping_sha256", "event_kind", "clock_id"}:
                raise ValueError(f"Unsupported or reserved mapping field {field!r}")
            raw = pointer(obj, spec["path"]) if "path" in spec else spec.get("constant")
            row[field] = convert(raw, spec, field)
            if field == "timestamp_utc":
                row["source_timestamp_raw"] = raw
            if field == "quality_score" and row[field] is not None:
                row["quality_scale"] = spec["scale"]
        if row["timestamp_utc"] and meta.get("recording_start_utc"):
            derived = (utc(row["timestamp_utc"]) - utc(meta["recording_start_utc"])).total_seconds()
            if row["elapsed_s"] not in (None, "") and abs(number(row["elapsed_s"]) - derived) > .01:
                raise ValueError(f"Line {line}: conflicting elapsed and UTC timestamps")
            row["elapsed_s"] = derived
        # No blanket nonzero/finite status rule: the reviewed schema supplies status semantics.
        counts[kind] += 1
        rows.append(row)
    if not rows:
        raise ValueError("Empty log: no measurements imported")
    target = out / "measurements.csv"
    existing = read_csv(target) if target.exists() else []
    if any(r["run_id"] == rid and r["source_sha256"] == sha for r in existing):
        raise ValueError("This source was already imported for this run")
    write_csv(target, MEASUREMENTS, existing + rows)
    safe_rid = hashlib.sha256(rid.encode()).hexdigest()[:16]
    write_json(out / f"import_{safe_rid}_{sha[:12]}.json", {
        "tool_version": VERSION, "source_sha256": sha, "mapping_sha256": msha,
        "run_id": rid, "event_counts": counts, "schema_evidence": mapping["evidence"],
        "limitation": "Import does not prove complete attempts, failures, settings, or connection coverage. Document these in metadata."
    })
    return counts


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * p
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def mean(values):
    return stats.mean(values) if values else None


def clock_time(row):
    if not row.get("clock_id"):
        return None
    # Prefer UTC across files; elapsed uses the recording origin by contract.
    if row.get("timestamp_utc"):
        return (row["clock_id"], "utc"), utc(row["timestamp_utc"]).timestamp()
    if row.get("elapsed_s") not in (None, ""):
        return (row["clock_id"], "elapsed"), number(row["elapsed_s"])
    return None


def duration(meta):
    value = number(meta.get("actual_duration_s"))
    if meta.get("recording_start_utc") and meta.get("recording_end_utc"):
        derived = (utc(meta["recording_end_utc"]) - utc(meta["recording_start_utc"])).total_seconds()
        if value is not None and abs(value - derived) > .01:
            raise ValueError(f"{meta['run_id']}: actual duration disagrees with recording boundaries")
        value = derived
    if value is not None and value <= 0:
        raise ValueError("Recording duration must be positive")
    return value


def event_key(row, field):
    # Require explicit peer and connection identities for safe correlation.
    values = tuple(row.get(k, "") for k in ("reflector_id", "connection_id", field))
    return values if all(values) else None


def attempt_counts(meta, rows, notes):
    method = meta.get("attempt_accounting", "")
    if not method or not meta.get("attempt_accounting_evidence"):
        notes.append("Attempts/failures/success rate unavailable: complete attempt coverage not documented")
        return None, None, None, None
    terminal = [r for r in rows if r["event_kind"] == "measurement"]
    attempts = [r for r in rows if r["event_kind"] == "attempt"]
    if method == "terminal_events_complete":
        attempts = terminal
    elif method != "attempt_events_complete":
        raise ValueError(f"Unknown attempt accounting {method!r}")
    ak = [event_key(r, "attempt_id") for r in attempts]
    tk = [event_key(r, "attempt_id") for r in terminal]
    if None in ak or None in tk or len(ak) != len(set(ak)) or len(tk) != len(set(tk)):
        notes.append("Attempts unavailable: IDs/peer/connection missing or ambiguous; do not assume procedure ID is unique")
        return None, None, None, None
    if not set(tk).issubset(set(ak)):
        notes.append("Attempts unavailable: terminal events lack corresponding observed attempts")
        return None, None, None, None
    successes = sum(r.get("status") == "valid" and number(r.get("cs_distance_m")) is not None for r in terminal)
    failures = sum(r.get("status") in {"failed", "invalid"} for r in terminal)
    unresolved = len(attempts) - successes - failures
    if unresolved:
        notes.append("Unresolved attempts retained in denominator; not labeled failed")
    return len(attempts), failures, unresolved, successes / len(attempts) * 100 if attempts else None


def disconnections(meta, rows, window, notes):
    if meta.get("connection_events_complete") != "true" or not meta.get("connection_events_evidence"):
        notes.append("Disconnect count/duration unavailable: connection-event completeness unverified")
        return None, None, None
    events = [r for r in rows if r["event_kind"] in {"connection_open", "connection_closed"}]
    count = sum(r["event_kind"] == "connection_closed" for r in events)
    # With one reflector per run, intervals refer to that evaluated link.
    if window is None or any(r.get("elapsed_s") in (None, "") for r in events):
        notes.append("Disconnected duration unavailable: elapsed connection timestamps or recording window missing")
        return count, None, None
    state = meta.get("initial_connection_state")
    if state not in {"connected", "disconnected"}:
        notes.append("Disconnected duration unavailable: initial connection state missing")
        return count, None, None
    start = 0 if state == "disconnected" else None
    total, intervals, censored = 0, [], 0
    for r in sorted(events, key=lambda r: number(r["elapsed_s"])):
        t = number(r["elapsed_s"])
        if not 0 <= t <= window:
            raise ValueError("Connection event outside recording window")
        if r["event_kind"] == "connection_closed":
            if start is not None:
                notes.append("Repeated close while disconnected; duration unavailable")
                return count, None, None
            start = t
        elif start is not None:
            total += t - start
            intervals.append(t - start)
            if start == 0:
                censored += 1
            start = None
    if start is not None:
        total += window - start
        intervals.append(window - start)
        censored += 1
    return count, total, json.dumps({"observed_interval_durations_s": intervals, "boundary_censored_intervals": censored})


def analyze_run(meta, rows, exclusions):
    notes, derived, valid = [], [], []
    window = duration(meta)
    actual, tolerance = number(meta.get("actual_distance_m")), number(meta.get("accuracy_tolerance_m"))
    if actual is not None and actual < 0 or tolerance is not None and tolerance < 0:
        raise ValueError("Separation and tolerance must be nonnegative")
    if actual is None:
        notes.append("Tape-measured actual distance missing; errors unavailable")
    if tolerance is None:
        notes.append("Agreed accuracy tolerance missing; within-tolerance percentage unavailable")
    if window is None:
        notes.append("Actual recording window missing; valid estimate rate unavailable (event span is not duration)")
    if not rows:
        notes.append("No source events for this run; planned metadata is not recorded data")
    requests, results = defaultdict(list), defaultdict(list)
    for r in rows:
        key = event_key(r, "request_id")
        if key and r["event_kind"] == "request":
            requests[key].append(r)
        if key and r["event_kind"] == "measurement":
            results[key].append(r)
    latencies = []
    for r in rows:
        if r["event_kind"] not in KINDS:
            raise ValueError(f"Unknown event_kind {r['event_kind']!r}")
        if r.get("status") and r["status"] not in {"valid", "invalid", "failed"}:
            raise ValueError("Unknown normalized status")
        for field in NUMERIC:
            number(r.get(field))
        if r.get("reflector_id") and meta.get("reflector_id") and r["reflector_id"] != meta["reflector_id"]:
            raise ValueError(f"{meta['run_id']}: mixed reflector IDs; split into separate runs")
        elapsed = number(r.get("elapsed_s"))
        if elapsed is None and r.get("timestamp_utc") and meta.get("recording_start_utc"):
            elapsed = (utc(r["timestamp_utc"]) - utc(meta["recording_start_utc"])).total_seconds()
        reason = exclusions.get((r["run_id"], r["source_sha256"], str(r["source_line"])), "")
        if elapsed is not None and (elapsed < 0 or window is not None and elapsed > window):
            reason = "; ".join(filter(None, [reason, "outside documented recording window"]))
        dr = dict(r, elapsed_s=elapsed, included_in_accuracy="false", exclusion_reason=reason,
                  signed_error_m=None, absolute_error_m=None, request_to_result_latency_s=None)
        if r["event_kind"] == "measurement" and not reason:
            key = event_key(r, "request_id")
            if key and len(requests[key]) == 1 and len(results[key]) == 1:
                rt, qt = clock_time(r), clock_time(requests[key][0])
                if rt and qt and rt[0] == qt[0] and rt[1] >= qt[1]:
                    dr["request_to_result_latency_s"] = rt[1] - qt[1]
                    latencies.append(dr["request_to_result_latency_s"])
            distance = number(r.get("cs_distance_m"))
            if r.get("status") == "valid" and distance is not None:
                dr["included_in_accuracy"] = "true"
                if actual is not None:
                    dr["signed_error_m"] = distance - actual
                    dr["absolute_error_m"] = abs(distance - actual)
                valid.append(dr)
            else:
                dr["exclusion_reason"] = f"status={r.get('status') or 'missing'} or CS distance missing"
        elif not reason:
            dr["exclusion_reason"] = f"{r['event_kind']} event; not a terminal distance estimate"
        derived.append(dr)
    # Attempts use the entire recording window, independent of user accuracy exclusions.
    observable_rows = [r for r in derived if "outside documented recording window" not in r["exclusion_reason"]]
    attempts, failed, unresolved, success = attempt_counts(meta, observable_rows, notes)
    dc, disconnected, intervals = disconnections(meta, observable_rows, window, notes)
    errors = [r["signed_error_m"] for r in valid if r["signed_error_m"] is not None]
    absolute = [abs(e) for e in errors]
    distances = [number(r["cs_distance_m"]) for r in valid]
    timed = [clock_time(r) for r in valid]
    gaps = []
    if timed and all(t is not None for t in timed) and len({t[0] for t in timed}) == 1:
        ordered = sorted(t[1] for t in timed)
        gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    elif valid:
        notes.append("Inter-estimate intervals unavailable: missing timestamps or mixed clocks")
    if not latencies:
        notes.append("Request-to-result latency unavailable: need unique paired request/result IDs, peer, connection, and shared clock")
    power_latency = None
    power_events = [r for r in derived if r["event_kind"] == "power_on"]
    startup_complete = meta.get("startup_capture_complete") == "true" and meta.get("startup_capture_evidence")
    if len(power_events) == 1 and startup_complete:
        pt = clock_time(power_events[0])
        candidates = [t[1] - pt[1] for t in timed if pt and t and t[0] == pt[0] and t[1] >= pt[1]]
        power_latency = min(candidates) if candidates else None
    elif meta.get("power_on_timestamp_utc") and meta.get("power_on_evidence") and startup_complete:
        start = utc(meta["power_on_timestamp_utc"]).timestamp()
        candidates = [utc(r["timestamp_utc"]).timestamp() - start for r in valid if r.get("timestamp_utc") and utc(r["timestamp_utc"]).timestamp() >= start]
        power_latency = min(candidates) if candidates else None
    if power_latency is None:
        notes.append("Power-on latency unavailable: synchronized power-on timestamp plus complete startup/result capture required")
    if any(r.get("event_kind") == "unmapped" for r in rows):
        notes.append("Unmapped events retained; inspect before interpreting coverage")
    summary = {"run_id": meta["run_id"], "trial_number": meta.get("trial_number"),
               "condition_id": meta.get("condition_id"), "run_purpose": meta.get("run_purpose"),
               "data_status": "recorded_events" if rows else "no_source_events",
               "actual_distance_m": actual, "accuracy_tolerance_m": tolerance,
               "actual_duration_s": window, "valid_distance_count": len(valid) if rows else None,
               "observed_valid_cs_result_count_before_accuracy_exclusions": sum(r["event_kind"] == "measurement" and r.get("status") == "valid" and number(r.get("cs_distance_m")) is not None for r in observable_rows) if rows else None,
               "observed_failed_result_count": sum(r["event_kind"] == "measurement" and r.get("status") == "failed" for r in observable_rows) if rows else None,
               "observed_invalid_result_count": sum(r["event_kind"] == "measurement" and r.get("status") == "invalid" for r in observable_rows) if rows else None,
               "attempted_measurement_count": attempts, "failed_or_invalid_count": failed,
               "unresolved_attempt_count": unresolved, "successful_measurement_pct": success,
               "mean_estimated_distance_m": mean(distances), "mean_signed_error_m": mean(errors),
               "mean_absolute_error_m": mean(absolute),
               "rmse_m": math.sqrt(mean([e * e for e in errors])) if errors else None,
               "sample_stddev_distance_m": stats.stdev(distances) if len(distances) > 1 else None,
               "p95_absolute_error_m": percentile(absolute, .95),
               "valid_estimates_within_tolerance_pct": sum(e <= tolerance for e in absolute) / len(absolute) * 100 if absolute and tolerance is not None else None,
               "inter_estimate_median_s": percentile(gaps, .5), "inter_estimate_p95_s": percentile(gaps, .95),
               "inter_estimate_max_s": max(gaps) if gaps else None,
               "valid_estimate_rate_hz": len(valid) / window if window is not None and rows else None,
               "disconnect_count": dc, "disconnected_duration_s": disconnected,
               "disconnect_intervals_json": intervals, "power_on_to_first_valid_s": power_latency,
               "request_to_result_median_s": percentile(latencies, .5),
               "request_to_result_p95_s": percentile(latencies, .95),
               "paired_request_result_count": len(latencies),
               "accuracy_excluded_terminal_count": sum(r["event_kind"] == "measurement" and r["included_in_accuracy"] == "false" for r in derived),
               "limitations": " | ".join(notes)}
    return summary, derived


def analyze(metadata_path, measurements_path, out, exclusions_path=None):
    out = Path(out)
    inputs = [Path(p).resolve() for p in (metadata_path, measurements_path, exclusions_path) if p]
    if any((out / name).resolve() in inputs for name in ("run_metadata.csv", "measurements.csv", "run_summary.csv")):
        raise ValueError("Analysis outputs must be separate from input CSVs")
    metadata = load_metadata(metadata_path)
    rows = read_csv(measurements_path)
    grouped, exclusions = defaultdict(list), {}
    for row in read_csv(exclusions_path) if exclusions_path else []:
        if not row.get("reason"):
            raise ValueError("Each explicit exclusion requires a reason")
        key = (row["run_id"], row["source_sha256"], row["source_line"])
        if key in exclusions:
            raise ValueError("Duplicate exclusion")
        exclusions[key] = row["reason"]
    seen, verified_sources, source_line_counts = set(), set(), {}
    for row in rows:
        if row["run_id"] not in metadata:
            raise ValueError(f"Unknown run {row['run_id']}")
        if not row.get("source_sha256") or not row.get("source_line"):
            raise ValueError("Every row requires source_sha256 and source_line for traceability")
        if not row.get("source_file") or not Path(row["source_file"]).is_file():
            raise ValueError("Every row requires an accessible preserved source_file")
        if int(row["source_line"]) < 1:
            raise ValueError("Source lines are 1-based")
        source_key = (row["source_file"], row["source_sha256"])
        if source_key not in verified_sources:
            if digest(row["source_file"]) != row["source_sha256"]:
                raise ValueError("Source hash mismatch; original capture changed")
            verified_sources.add(source_key)
            with Path(row["source_file"]).open(encoding="utf-8-sig") as source:
                source_line_counts[row["source_file"]] = sum(1 for _ in source)
        if int(row["source_line"]) > source_line_counts[row["source_file"]]:
            raise ValueError("source_line exceeds preserved source length")
        key = (row["run_id"], row["source_sha256"], row["source_line"])
        if key in seen:
            raise ValueError("Duplicate source row; resolve the duplicate import before analysis")
        seen.add(key)
        grouped[row["run_id"]].append(row)
    if set(exclusions) - seen:
        raise ValueError("Exclusions reference nonexistent source rows")
    summaries, derived = [], []
    for rid, meta in metadata.items():
        summary, dr = analyze_run(meta, grouped[rid], exclusions)
        summaries.append(summary)
        derived.extend(dr)
    for path in inputs:
        archive(path, out / "input_snapshots")
    write_csv(out / "run_summary.csv", list(summaries[0]) if summaries else ["run_id"], summaries)
    write_csv(out / "measurements.csv", MEASUREMENTS + ["included_in_accuracy", "exclusion_reason", "signed_error_m", "absolute_error_m", "request_to_result_latency_s"], derived)
    write_csv(out / "run_metadata.csv", METADATA, [{k: r.get(k, "") for k in METADATA} for r in metadata.values()])
    audits = []
    for rid, m in metadata.items():
        missing = [k for k in REQUIRED_METADATA if not m.get(k)]
        if duration(m) is None:
            missing.append("actual_duration_s or recording_start_utc + recording_end_utc")
        if m.get("visibility") and "obstruct" in m["visibility"].lower():
            missing.extend(k for k in ("obstacle_material", "obstacle_placement") if not m.get(k))
        audits.append({"run_id": rid, "data_status": "recorded_events" if grouped[rid] else "no_source_events",
                       "missing_required_count": len(missing), "missing_required_fields": " | ".join(missing)})
    write_csv(out / "metadata_audit.csv", ["run_id", "data_status", "missing_required_count", "missing_required_fields"], audits)
    write_json(out / "analysis_manifest.json", {
        "tool_version": VERSION, "created_utc": iso(datetime.now(timezone.utc)),
        "inputs": [{"path": str(Path(p).resolve()), "sha256": digest(p)} for p in [metadata_path, measurements_path, exclusions_path] if p],
        "policies": {"filter": "Explicit exclusions plus status=valid and nonmissing CS distance; no automatic outlier or quality filtering",
                     "attempts": "All in-window source events, unaffected by accuracy exclusions; requires documented complete coverage",
                     "percentile": "Linear interpolation at (n-1)*p", "stddev": "Sample standard deviation (n-1); missing for n<2",
                     "distance": "Raw CS distances; RTT/RSSI distance and actual RSSI preserved separately",
                     "reliability": "Valid CS terminal results / observed attempts only; quality never used as denominator"}})
    plot_results(metadata, summaries, derived, out / "plots")
    return summaries


def plot_results(metadata, summaries, rows, folder):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    folder.mkdir(parents=True, exist_ok=True)
    previous_index = folder / "plot_index.json"
    if previous_index.exists():
        old = json.loads(previous_index.read_text(encoding="utf-8"))
        for key in old:
            if len(key) == 16 and all(c in "0123456789abcdef" for c in key):
                for extension in ("png", "svg"):
                    # Delete only our prior generated filenames, never captures or user files.
                    (folder / f"distance_vs_elapsed_{key}.{extension}").unlink(missing_ok=True)
    for extension in ("png", "svg"):
        (folder / f"distance_vs_elapsed_no_data.{extension}").unlink(missing_ok=True)
    for yfield, ylabel, filename in [
        ("mean_estimated_distance_m", "Mean CS estimated distance (m)", "estimated_vs_actual"),
        ("mean_signed_error_m", "Mean signed error: estimate - tape (m)", "error_vs_distance"),
    ]:
        fig, ax = plt.subplots(figsize=(9, 6))
        groups = defaultdict(list)
        for s in summaries:
            if s[yfield] is not None and s["actual_distance_m"] is not None:
                groups[(s["condition_id"], s["trial_number"], s["run_purpose"])].append(s)
        for (condition, trial, purpose), series in groups.items():
            series.sort(key=lambda s: s["actual_distance_m"])
            ax.plot([s["actual_distance_m"] for s in series], [s[yfield] for s in series],
                    "o--", label=f"{condition}; trial {trial}; {purpose}")
        if groups:
            if yfield.startswith("mean_estimated"):
                values = [s["actual_distance_m"] for series in groups.values() for s in series]
                ax.plot([min(values), max(values)], [min(values), max(values)], "k:", label="Ideal estimate = tape")
            else:
                ax.axhline(0, color="black", linestyle=":", label="Zero error")
            ax.legend(fontsize=7)
        else:
            ax.text(.5, .5, "No eligible measured data", transform=ax.transAxes, ha="center")
        ax.set(xlabel="Tape-measured separation (m)", ylabel=ylabel,
               title="Genie BLE Channel Sounding — raw results by trial")
        ax.grid(alpha=.25)
        fig.tight_layout()
        fig.savefig(folder / f"{filename}.png", dpi=180)
        fig.savefig(folder / f"{filename}.svg")
        plt.close(fig)
    plot_index = {}
    for rid, meta in metadata.items():
        series = [r for r in rows if r["run_id"] == rid and r["included_in_accuracy"] == "true" and r["elapsed_s"] is not None]
        if not series:
            continue
        series.sort(key=lambda r: r["elapsed_s"])
        fig, ax = plt.subplots(figsize=(10, 5))
        for field, label in [("cs_distance_m", "CS distance"), ("rtt_distance_m", "RTT distance"), ("rssi_distance_m", "RSSI-based distance")]:
            selected = [r for r in series if number(r.get(field)) is not None]
            if selected:
                ax.plot([r["elapsed_s"] for r in selected], [number(r[field]) for r in selected], ".", label=label)
        actual = number(meta.get("actual_distance_m"))
        if actual is not None:
            ax.axhline(actual, color="black", linestyle="--", label="Tape-measured separation")
        ax.set(xlabel="Elapsed since recording began (s)", ylabel="Distance (m)",
               title=f"{rid} — trial {meta.get('trial_number', '')}; {meta.get('condition_id', '')}")
        ax.legend()
        ax.grid(alpha=.25)
        fig.tight_layout()
        safe_name = hashlib.sha256(rid.encode()).hexdigest()[:16]
        plot_index[safe_name] = rid
        fig.savefig(folder / f"distance_vs_elapsed_{safe_name}.png", dpi=180)
        fig.savefig(folder / f"distance_vs_elapsed_{safe_name}.svg")
        plt.close(fig)
    if not plot_index:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.text(.5, .5, "No eligible estimates with verified elapsed time\nReal recording data required", transform=ax.transAxes, ha="center")
        ax.set(xlabel="Elapsed since recording began (s)", ylabel="Distance (m)",
               title="Genie BLE Channel Sounding — distance versus elapsed time (no data)")
        fig.tight_layout()
        for extension in ("png", "svg"):
            fig.savefig(folder / f"distance_vs_elapsed_no_data.{extension}", dpi=180)
        plt.close(fig)
    write_json(folder / "plot_index.json", plot_index)


def power(metadata_path, samples_path, out):
    metadata = read_csv(metadata_path)
    samples = read_csv(samples_path)
    ids = [m["power_run_id"] for m in metadata]
    if any(not x for x in ids) or len(ids) != len(set(ids)):
        raise ValueError("Unique nonempty power_run_id required")
    if any(s["power_run_id"] not in ids for s in samples):
        raise ValueError("Power sample references unknown power_run_id")
    summaries = []
    for m in metadata:
        required = ["board_role", "board_id", "instrument_model", "sampling_settings", "measurement_location", "peak_measurement_limits"]
        if any(not m.get(k) for k in required):
            raise ValueError("Document instrument, sampling, location, and peak measurement limits")
        if m.get("instrument_source_file"):
            if not m.get("instrument_source_sha256") or digest(m["instrument_source_file"]) != m["instrument_source_sha256"]:
                raise ValueError("Original instrument export hash missing or mismatched")
            archive(m["instrument_source_file"], Path(out) / "power_sources")
        series = sorted([s for s in samples if s["power_run_id"] == m["power_run_id"]], key=lambda r: number(r["time_s"]))
        start, end = number(m["window_start_s"]), number(m["window_end_s"])
        if start is None or end is None or end <= start:
            raise ValueError("Define a positive power recording window")
        if len(series) < 2:
            raise ValueError("At least two instrument samples required")
        # Never integrate over a missing-current gap or silently interpolate missing voltages.
        times = [number(s["time_s"]) for s in series]
        if any(t is None for t in times) or any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError("Power times must be finite and strictly increasing")
        if times[0] > start or times[-1] < end:
            raise ValueError("Instrument samples must cover the defined recording window")
        charge, volt_area, volt_time, state_charge, state_time = 0., 0., 0., defaultdict(float), defaultdict(float)
        observed = [number(s["current_a"]) for s in series if start <= number(s["time_s"]) <= end and s.get("current_a") not in (None, "")]
        gaps = []
        for a, b in zip(series, series[1:]):
            ta, tb = number(a["time_s"]), number(b["time_s"])
            lo, hi = max(start, ta), min(end, tb)
            if hi <= lo:
                continue
            ia, ib = number(a["current_a"]), number(b["current_a"])
            if ia is None or ib is None:
                raise ValueError("Missing instrument current inside window; choose a complete window")
            ilo = ia + (ib - ia) * (lo - ta) / (tb - ta)
            ihi = ia + (ib - ia) * (hi - ta) / (tb - ta)
            dt, area = hi - lo, (ilo + ihi) / 2 * (hi - lo)
            gaps.append(tb - ta)
            charge += area
            state = a.get("state", "")  # Left sample labels [ta, tb).
            if state not in {"active", "idle", "sleep", ""}:
                raise ValueError("Instrument state must be active/idle/sleep or missing")
            state_charge[state] += area
            state_time[state] += dt
            va, vb = number(a.get("voltage_v")), number(b.get("voltage_v"))
            if va is not None and vb is not None:
                vlo, vhi = va + (vb - va) * (lo - ta) / (tb - ta), va + (vb - va) * (hi - ta) / (tb - ta)
                volt_area += (vlo + vhi) / 2 * dt
                volt_time += dt
        window = end - start
        row = {"power_run_id": m["power_run_id"], "run_id": m.get("run_id"), "board_role": m.get("board_role"),
               "window_duration_s": window, "average_current_a": charge / window,
               "peak_sampled_current_a": max(observed) if observed else None, "average_voltage_v": volt_area / window if abs(volt_time - window) < 1e-9 else None,
               "max_sample_gap_s": max(gaps), "battery_life_days_estimate": None,
               "battery_life_limitation": "Need measured state currents, capacity, usable fraction, and a full daily usage budget; sampled peak is bandwidth limited"}
        for state in ("active", "idle", "sleep"):
            row[f"{state}_current_a"] = state_charge[state] / state_time[state] if state_time[state] else None
            row[f"{state}_duty_cycle_pct"] = state_time[state] / window * 100 if m.get("state_classification_method") else None
        row["unclassified_duration_s"] = state_time[""]
        cap, fraction = number(m.get("battery_capacity_mah")), number(m.get("usable_capacity_fraction"))
        hours = {state: number(m.get(f"daily_{state}_hours")) for state in ("active", "idle", "sleep")}
        if cap is not None and fraction is not None and all(h is not None for h in hours.values()) and m.get("battery_life_assumptions"):
            if cap <= 0 or not 0 < fraction <= 1 or any(h < 0 for h in hours.values()) or abs(sum(hours.values()) - 24) > 1e-6:
                raise ValueError("Battery estimate requires positive capacity, fraction in (0,1], and 24 daily hours")
            if all(h == 0 or row[f"{state}_current_a"] is not None for state, h in hours.items()) and m.get("state_classification_method"):
                daily_mah = sum((row[f"{state}_current_a"] or 0) * 1000 * h for state, h in hours.items())
                if daily_mah > 0:
                    row["battery_life_days_estimate"] = cap * fraction / daily_mah
                    row["battery_life_limitation"] = "Usage model only: document regulator losses, voltage/capacity dependence, self-discharge, temperature, and instrument limits"
        summaries.append(row)
    out = Path(out)
    write_csv(out / "power_summary.csv", list(summaries[0]) if summaries else ["power_run_id"], summaries)
    for path in (metadata_path, samples_path):
        archive(path, out / "power_sources")
    write_json(out / "power_method.json", {"integration": "Piecewise linear trapezoidal integration, clipped to explicit window; state held from left sample",
              "inputs": {str(p): digest(p) for p in (metadata_path, samples_path)}})
    if samples:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        for m in metadata:
            subset = sorted([s for s in samples if s["power_run_id"] == m["power_run_id"]], key=lambda s: number(s["time_s"]))
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.plot([number(s["time_s"]) for s in subset], [number(s["current_a"]) if s["current_a"] else math.nan for s in subset])
            ax.axvspan(number(m["window_start_s"]), number(m["window_end_s"]), alpha=.15, label="Analysis window")
            ax.set(xlabel="Instrument time (s)", ylabel="Measured current (A)", title=f"{m['power_run_id']} — {m['instrument_model']}")
            ax.legend()
            fig.tight_layout()
            fig.savefig(out / f"current_{hashlib.sha256(m['power_run_id'].encode()).hexdigest()[:16]}.png", dpi=180)
            plt.close(fig)
    return summaries


def calibrate(metadata_path, measurements_path, training_ids, validation_ids, out):
    train, validate = set(training_ids), set(validation_ids)
    if not train or not validate or train & validate:
        raise ValueError("Calibration requires disjoint, nonempty training and validation run IDs")
    metadata = load_metadata(metadata_path)
    for rid in train | validate:
        if rid not in metadata:
            raise ValueError(f"Unknown calibration/validation run {rid}")
        purpose = "calibration" if rid in train else "validation"
        if metadata[rid].get("run_purpose") != purpose:
            raise ValueError(f"{rid}: mark run_purpose={purpose} using separately collected measurements")
    rows = read_csv(measurements_path)
    if any("included_in_accuracy" not in r for r in rows):
        raise ValueError("Calibrate the analyzed measurements.csv so exclusions are honored")
    selected = [r for r in rows if r["run_id"] in train | validate and r["included_in_accuracy"] == "true"]
    sources = defaultdict(set)
    for r in selected:
        if not r.get("source_file") or digest(r["source_file"]) != r["source_sha256"]:
            raise ValueError("Calibration source missing or source hash mismatched")
        sources[r["source_sha256"]].add("train" if r["run_id"] in train else "validate")
    if any(len(v) > 1 for v in sources.values()):
        raise ValueError("Training and validation share a source capture; collect separate validation logs")
    train_rows = [r for r in selected if r["run_id"] in train]
    validation_rows = [r for r in selected if r["run_id"] in validate]
    errors = []
    for r in train_rows:
        actual = number(metadata[r["run_id"]].get("actual_distance_m"))
        if actual is None:
            raise ValueError("Calibration needs tape-measured distances")
        errors.append(number(r["cs_distance_m"]) - actual)
    if not errors or not validation_rows:
        raise ValueError("Both calibration and validation need eligible data")
    offset = stats.mean(errors)
    model = {"method": "Additive bias correction: corrected_m = raw_cs_distance_m - training_mean_signed_error_m",
             "training_mean_signed_error_m": offset, "training_run_ids": sorted(train),
             "validation_run_ids": sorted(validate), "training_sample_count": len(errors),
             "weighting": "Equal weight per eligible training estimate", "clipping": "None",
             "limitations": "Applies only to documented tested conditions; do not tune on validation results",
             "inputs": {str(p): digest(p) for p in (metadata_path, measurements_path)}}
    corrected, grouped = [], defaultdict(list)
    for r in validation_rows:
        actual = number(metadata[r["run_id"]].get("actual_distance_m"))
        if actual is None:
            raise ValueError("Validation needs tape-measured distances")
        raw = number(r["cs_distance_m"])
        row = {"run_id": r["run_id"], "source_sha256": r["source_sha256"], "source_line": r["source_line"],
               "raw_cs_distance_m": raw, "corrected_cs_distance_m": raw - offset,
               "actual_distance_m": actual, "raw_signed_error_m": raw - actual,
               "corrected_signed_error_m": raw - offset - actual}
        corrected.append(row)
        grouped[r["run_id"]].append(row)
    summaries = []
    for rid, values in grouped.items():
        s = {"run_id": rid, "trial_number": metadata[rid].get("trial_number"), "valid_count": len(values)}
        tol = number(metadata[rid].get("accuracy_tolerance_m"))
        for label in ("raw", "corrected"):
            ee = [r[f"{label}_signed_error_m"] for r in values]
            s.update({f"{label}_mean_signed_error_m": mean(ee), f"{label}_mae_m": mean([abs(e) for e in ee]),
                      f"{label}_rmse_m": math.sqrt(mean([e * e for e in ee])),
                      f"{label}_p95_absolute_error_m": percentile([abs(e) for e in ee], .95),
                      f"{label}_within_tolerance_pct": sum(abs(e) <= tol for e in ee) / len(ee) * 100 if tol is not None else None})
        summaries.append(s)
    write_json(Path(out) / "calibration_model.json", model)
    for p in (metadata_path, measurements_path):
        archive(p, Path(out) / "input_snapshots")
    write_csv(Path(out) / "validation_measurements.csv", list(corrected[0]), corrected)
    write_csv(Path(out) / "validation_summary.csv", list(summaries[0]), summaries)
    return model


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("live", help="Follow the Analyzer's active per-reflector recording and plot live distances")
    q.add_argument("log", nargs="?", help="JSONL recording; defaults to newest record-*.jsonl in data")
    q.add_argument("--folder", default=str(Path(__file__).resolve().parent / "data"))
    q.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "live"))
    q.add_argument("--duration", type=float, help="Stop monitoring after this many seconds; otherwise Ctrl+C")
    q.add_argument("--estimates", type=int, help="Stop after this many finite filtered estimates")
    q.add_argument("--interval", type=float, default=0.5)
    q.add_argument("--no-plot", action="store_true", help="Hide the live graph; still save report PNGs")
    q.add_argument("--auto-record", action="store_true", help="Open Analyzer, connect the boards, and start recording")
    q.add_argument("--actual-distance", type=float, help="Actual tape-measured separation in meters")
    q.add_argument("--field", choices=["distanceMainRaw", "distanceMain", "distanceSubRaw", "distanceSub"], default="distanceMainRaw")
    q = sub.add_parser("guided", help="Collect and resume 40 conditions in the configured antenna mode")
    q.add_argument("--folder", default=str(Path(__file__).resolve().parent / "data"))
    q.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "live"))
    q.add_argument("--interval", type=float, default=0.5)
    q.add_argument("--no-plot", action="store_true", help="Hide the live graph; still save report PNGs")
    q = sub.add_parser("init", help="Create blank data templates and proposed OFAT test plan")
    q.add_argument("folder"); q.add_argument("--trials", type=int, default=3); q.add_argument("--duration", type=float, default=60)
    q = sub.add_parser("inspect", help="Inventory a real JSONL capture without assuming semantics")
    q.add_argument("log"); q.add_argument("--out", required=True); q.add_argument("--event-path")
    q = sub.add_parser("import", help="Normalize using an explicitly reviewed mapping")
    q.add_argument("log"); q.add_argument("--metadata", required=True); q.add_argument("--run-id", required=True)
    q.add_argument("--mapping", required=True); q.add_argument("--out", required=True)
    q = sub.add_parser("analyze", help="Produce traceable per-trial summaries and plots")
    q.add_argument("--metadata", required=True); q.add_argument("--measurements", required=True)
    q.add_argument("--exclusions"); q.add_argument("--out", required=True)
    q = sub.add_parser("power", help="Analyze separately measured instrument current/voltage")
    q.add_argument("--metadata", required=True); q.add_argument("--samples", required=True); q.add_argument("--out", required=True)
    q = sub.add_parser("calibrate", help="Fit bias correction and evaluate separate validation runs")
    q.add_argument("--metadata", required=True); q.add_argument("--measurements", required=True)
    q.add_argument("--training", nargs="+", required=True); q.add_argument("--validation", nargs="+", required=True); q.add_argument("--out", required=True)
    args = sys.argv[1:]
    a = p.parse_args(args or ["live", "--auto-record"])
    try:
        if not args:
            while True:
                choice = input("Capture mode [1 = single trial, 2 = guided trials, Enter = 1]? ").strip().casefold()
                if choice in ("", "1", "single"):
                    break
                if choice in ("2", "guided", "calibration"):
                    from support.genie_calibration import run_guided
                    run_guided(a.folder, a.out, plot=not a.no_plot, interval=a.interval)
                    return
                print("Enter 1 or 2.")
        if a.command == "guided":
            from support.genie_calibration import run_guided
            run_guided(a.folder, a.out, plot=not a.no_plot, interval=a.interval)
            return
        if a.command == "live":
            from support.genie_live import automatic_recording, prompt_trial_details, run_live
            settings = dict(log=a.log, folder=a.folder, out=a.out, duration=a.duration,
                            interval=a.interval, plot=not a.no_plot,
                            actual_distance=a.actual_distance, field=a.field)
            if a.estimates is not None:
                if a.estimates <= 0:
                    raise ValueError("Estimate target must be a positive integer")
                settings["max_estimates"] = a.estimates
            if a.auto_record:
                from support.firmware_state import snapshot_firmware
                if a.log:
                    raise ValueError("Choose --auto-record or an existing recording path.")
                try:
                    settings.setdefault("max_estimates", 100)
                    if not args:
                        settings["actual_distance"], settings["conditions"] = prompt_trial_details(a.out)
                    settings["firmware"] = snapshot_firmware()
                    from support.capture_setup import print_setup_summary
                    print_setup_summary(settings["firmware"], settings["actual_distance"],
                                        settings.get("conditions"), data=Path(a.out).parent)
                    with automatic_recording(a.folder) as recording:
                        if snapshot_firmware() != settings["firmware"]:
                            raise ValueError("Firmware settings changed while connecting. Flash both boards and start a new capture.")
                        run_live(**settings, recording=recording)
                except EOFError:
                    raise ValueError("Trial setup needs terminal input. Run analyzer.py in the terminal or use analyzer.py live --auto-record to skip the questions.") from None
                except KeyboardInterrupt:
                    print("Automatic Analyzer capture stopped.", flush=True)
            else:
                run_live(**settings)
        elif a.command == "init":
            if a.trials < 1 or a.duration <= 0:
                raise ValueError("Positive trial count and duration required")
            print(f"Created {init(a.folder, a.trials, a.duration)} planned runs; measured fields remain blank")
        elif a.command == "inspect":
            report = inspect_log(a.log, a.out, a.event_path)
            print(f"Inspected {report['json_object_count']} JSON objects; review inspection.json before mapping")
        elif a.command == "import":
            print(dict(import_log(a.log, a.metadata, a.run_id, a.mapping, a.out)))
        elif a.command == "analyze":
            print(f"Summarized {len(analyze(a.metadata, a.measurements, a.out, a.exclusions))} runs")
        elif a.command == "power":
            print(f"Summarized {len(power(a.metadata, a.samples, a.out))} instrument windows")
        elif a.command == "calibrate":
            print(f"Wrote correction and independent validation: {calibrate(a.metadata, a.measurements, a.training, a.validation, a.out)['method']}")
    except (EOFError, KeyboardInterrupt):
        print("\nCapture stopped.", flush=True)
    except (ValueError, KeyError, OSError, RuntimeError) as e:
        p.exit(2, f"Error: {e}\n")


if __name__ == "__main__":
    main()
