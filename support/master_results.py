"""Rebuild local trial comparisons from preserved captures and explicit evidence."""

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
from statistics import mean, median, pstdev
from tempfile import NamedTemporaryFile

from .trial_storage import trial_folders, linked
from .results_layout import (FIELDS, TERMS, STOP_LABELS, RESULT_COLUMNS, CONFIG_COLUMNS,
                             OVERVIEW_COLUMNS, CHECKLIST_COLUMNS)


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = json.loads(Path(__file__).with_name("test_protocol.json").read_text(encoding="utf-8"))
PROFILES = PROTOCOL["profiles"]
TEXT_FIELDS = ("environment", "obstruction", "polarization", "motion", "power_source",
               "operator", "notes", "latency_definition", "latency_evidence",
               "power_evidence", "tx_power_evidence")
NUMBER_FIELDS = ("initiator_tx_dbm", "reflector_tx_dbm", "measured_latency_ms")
CONTEXT_FIELDS = ("environment", "obstruction", "polarization", "motion", "power_source", "operator")


def test_context(data=ROOT / "data"):
    path = Path(data) / "settings/test_context.json"
    return load_json(path) if path.exists() else {}


def save_context(values, data=ROOT / "data"):
    atomic_json(Path(data) / "settings/test_context.json",
                {key: str(values.get(key, "")).strip() for key in CONTEXT_FIELDS if str(values.get(key, "")).strip()})


def number(value):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def load_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path}")
    return value


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def radio_profile(data=ROOT / "data"):
    path = Path(data) / "settings/radio.json"
    value = load_json(path).get("profile") if path.exists() else "baseline"
    if value not in PROFILES:
        raise ValueError("Unknown Analyzer profile. Select one in results.py.")
    return value


def select_profile(profile, data=ROOT / "data"):
    if profile not in PROFILES:
        raise ValueError("Choose baseline, accuracy or energy.")
    atomic_json(Path(data) / "settings/radio.json", {"profile": profile})


def details(folder):
    path = Path(folder) / "capture/test_details.json"
    return load_json(path) if path.exists() else {}


def save_details(folder, values):
    cleaned = details(folder)
    for key in TEXT_FIELDS:
        cleaned[key] = str(values.get(key, "")).strip() or None
    for key in NUMBER_FIELDS:
        raw = values.get(key)
        value = number(raw)
        if raw not in (None, "") and value is None:
            raise ValueError(f"{key} must be a finite number or blank.")
        if value is not None and ((key.endswith("tx_dbm") and not -127 <= value <= 10)
                                  or (key == "measured_latency_ms" and value < 0)):
            raise ValueError(f"{key} is outside its supported range.")
        cleaned[key] = value
    if any(cleaned.get(k) is not None for k in ("initiator_tx_dbm", "reflector_tx_dbm")) and not cleaned["tx_power_evidence"]:
        raise ValueError("Give the readback/log source for verified TX power; a firmware default is not readback.")
    if cleaned.get("measured_latency_ms") is not None and not (cleaned["latency_definition"] and cleaned["latency_evidence"]):
        raise ValueError("Measured latency needs its start/end definition and evidence source.")
    cleaned["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    atomic_json(Path(folder) / "capture/test_details.json", cleaned)


def attempt_metrics(path):
    """One observed terminal outcome per explicit attempt ID; no guessed attempts."""
    counts = {key: 0 for key in ("valid", "failed", "unresolved")}
    ids, clocks, latencies = set(), set(), []
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"attempt_id", "status"} <= set(reader.fieldnames or []):
            raise ValueError("Attempts CSV needs attempt_id,status (valid,failed,unresolved).")
        for row in reader:
            identity = (row.get("attempt_id") or "").strip()
            status = (row.get("status") or "").strip().lower()
            if not identity or identity in ids or status not in counts:
                raise ValueError("Attempt IDs must be present and unique; use valid, failed or unresolved status.")
            ids.add(identity)
            counts[status] += 1
            start, end = number(row.get("request_time_s")), number(row.get("result_time_s"))
            clock = (row.get("clock_id") or "").strip()
            if row.get("request_time_s") or row.get("result_time_s"):
                if start is None or end is None or end < start or not clock:
                    raise ValueError("Timing requires request_time_s,result_time_s and one shared clock_id per pair.")
                if status == "valid":
                    clocks.add(clock)
                    latencies.append((end - start) * 1000)
    if not ids:
        raise ValueError("The attempts file is empty.")
    if len(clocks) > 1:
        raise ValueError("Use one documented clock for the trial's latency samples.")
    return {"attempts": len(ids), **counts, "timed_valid": len(latencies),
            "mean_latency_ms": mean(latencies) if latencies else None}


def power_metrics(path, cycles=None):
    """Time-weighted instrument current; optional energy from measured voltage."""
    samples = []
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"time_s", "current_ma"} <= set(reader.fieldnames or []):
            raise ValueError("Power CSV needs time_s,current_ma; voltage_v is optional.")
        for row in reader:
            t, current = number(row.get("time_s")), number(row.get("current_ma"))
            volts = number(row.get("voltage_v"))
            if t is None or current is None or current < 0 or (samples and t <= samples[-1][0]):
                raise ValueError("Instrument time must increase strictly; current_ma must be finite and nonnegative.")
            if row.get("voltage_v") and (volts is None or volts <= 0):
                raise ValueError("Measured voltage_v must be positive when supplied.")
            samples.append((t, current, volts))
    if len(samples) < 2:
        raise ValueError("At least two instrument samples are required.")
    duration = samples[-1][0] - samples[0][0]
    charge = sum((b[0] - a[0]) * (a[1] + b[1]) / 2 for a, b in zip(samples, samples[1:]))
    energy = (sum((b[0] - a[0]) * (a[1] * a[2] + b[1] * b[2]) / 2
                  for a, b in zip(samples, samples[1:])) if all(s[2] is not None for s in samples) else None)
    return {"mean_current_ma": charge / duration, "instrument_duration_s": duration,
            "energy_mj": energy,
            "energy_per_cycle_mj": energy / cycles if energy is not None and cycles else None}


def attach_evidence(folder, source, kind, *, coverage_complete=False, note="", cycles=None):
    """Copy user-selected evidence into the trial; never change its source file."""
    folder, source = Path(folder), Path(source)
    if not note.strip():
        raise ValueError("Describe the instrument/log source and the captured window.")
    if cycles is not None and (type(cycles) is not int or cycles <= 0):
        raise ValueError("Cycle count must be a positive integer or blank.")
    if kind == "attempts":
        attempt_metrics(source)
    elif kind == "power":
        power_metrics(source, cycles)
    else:
        raise ValueError("Choose attempts or power evidence.")
    blob = source.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    destination = folder / "capture" / f"{kind}_{digest[:12]}.csv"
    destination.write_bytes(blob)
    saved = details(folder)
    saved[kind + "_file"] = destination.name
    saved[kind + "_sha256"] = digest
    saved[kind + "_evidence"] = note.strip()
    if kind == "attempts":
        saved["attempt_coverage_complete"] = bool(coverage_complete)
    else:
        saved["power_cycles"] = cycles
    atomic_json(folder / "capture/test_details.json", saved)


def percentile(values, fraction):
    values = sorted(values)
    at = (len(values) - 1) * fraction
    lo = math.floor(at)
    return values[lo] + (values[math.ceil(at)] - values[lo]) * (at - lo)


def result_statistics(folder, actual):
    values = {"raw": [], "filtered": []}
    observed = unusable = malformed = 0
    path = Path(folder) / "capture/raw.jsonl"
    if path.exists():
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if (not isinstance(row, dict) or not row.get("initiatorId") or not row.get("reflectorId")
                            or not any(key in row for key in ("distanceMain", "distanceMainRaw", "distanceSub", "distanceSubRaw"))):
                        malformed += 1
                        continue
                except ValueError:
                    malformed += 1
                    continue
                observed += 1
                distance = row.get("distanceMain")
                unusable += not (type(distance) in (int, float) and math.isfinite(distance))
    csv_path = Path(folder) / "capture/measurements.csv"
    if csv_path.exists():
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                for label, key in (("raw", "distanceMainRaw"), ("filtered", "distanceMain")):
                    value = number(row.get(key))
                    if value is not None:
                        values[label].append(value)
    output = {"observed_results": observed if path.exists() else None,
              "unusable_results": unusable if path.exists() else None,
              "malformed_records": malformed}
    for label, samples in values.items():
        output[label + "_n"] = len(samples)
        output[label + "_median_m"] = median(samples) if samples else None
        output[label + "_std_m"] = pstdev(samples) if samples else None
        errors = [v - actual for v in samples] if actual is not None else []
        absolute = [abs(v) for v in errors]
        output[label + "_bias_m"] = mean(errors) if errors else None
        output[label + "_mae_m"] = mean(absolute) if errors else None
        output[label + "_rmse_m"] = math.sqrt(mean([e * e for e in errors])) if errors else None
        output[label + "_p95_error_m"] = percentile(absolute, 0.95) if errors else None
        if label == "filtered":
            output["_filtered_errors"] = errors
    return output


def analyzer_configuration(folder):
    path = Path(folder) / "capture/analyzer_telemetry.jsonl"
    latest = None
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("Malformed Analyzer telemetry event.")
            if event.get("kind") == "configuration" and event.get("configuration"):
                if not isinstance(event["configuration"], dict):
                    raise ValueError("Malformed requested Analyzer configuration.")
                latest = event["configuration"]
    return latest


def experiment_signature(folder):
    """Keep deliberate orientation sweeps together, but separate changed setups."""
    path = Path(folder) / "capture/summary.json"
    if not path.exists():
        return "{}"
    summary = load_json(path)
    context = {**(summary.get("conditions") or {}), **details(folder)}
    fields = (*CONTEXT_FIELDS, "initiator_height_m", "reflector_height_m", "initiator_tx_dbm", "reflector_tx_dbm")
    identity = {key: context[key] for key in fields if key != "operator" and context.get(key) not in (None, "")}
    configuration = analyzer_configuration(folder)
    if configuration:
        identity["analyzer"] = configuration
    return json.dumps(identity, sort_keys=True)


def trial_row(folder, live):
    folder = Path(folder)
    summary = load_json(folder / "capture/summary.json")
    saved = details(folder)
    conditions = summary.get("conditions") or {}
    firmware = summary.get("firmware") or (summary.get("calibration") or {}).get("firmware") or {}
    mode = firmware.get("mode", "unknown")
    group = "Simulated" if summary.get("simulated") else "Normal trials" if mode == "normal" else "Calibration" if mode == "isolated" else "Unclassified"
    actual = number(summary.get("actual_distance_m"))
    calibration = summary.get("calibration") or {}
    rotated = calibration.get("rotated_board") or ("reflector" if conditions.get("initiator_yaw_deg") == 0 else "initiator" if conditions.get("reflector_yaw_deg") == 0 else None)
    fixed_yaw = calibration.get("fixed_yaw_deg", conditions.get("initiator_yaw_deg" if rotated == "reflector" else "reflector_yaw_deg"))
    row = {"trial": str(folder.relative_to(live)), "group": group,
           "recorded": summary.get("session_started_at_utc"), "actual_m": actual,
           "initiator_yaw": conditions.get("initiator_yaw_deg"), "reflector_yaw": conditions.get("reflector_yaw_deg"),
           "initiator_height_m": conditions.get("initiator_height_m"), "reflector_height_m": conditions.get("reflector_height_m"),
           "board_pair": ", ".join(summary.get("board_pair") or []), "firmware": json.dumps(firmware, sort_keys=True),
           "fingerprint": firmware.get("fingerprint"), "stop_reason": summary.get("stop_reason"),
           "rotated_board": rotated, "fixed_yaw": fixed_yaw,
           "complete": bool(summary.get("target_reached") and summary.get("reports_saved") and summary.get("stop_reason") == "target_reached"),
           "capture_notes": conditions.get("setup_notes"), "source": str(folder / "capture/summary.json"),
           **{key: conditions.get(key) for key in TEXT_FIELDS}, **saved}
    descriptions = [{"isolated": "Calibration (isolated antenna)", "normal": "Normal (dual antenna)"}.get(mode, "Unknown mode")]
    for role, config in firmware.get("projects", {}).items():
        offsets = config.get("wireless_offsets_cm", [])
        antenna = config.get("physical_antenna")
        used = [antenna] if antenna in (1, 2) else [1, 2]
        descriptions.append(role.capitalize() + ": " + ", ".join(
            f"ANT{index} {offsets[index - 1]} cm" for index in used if len(offsets) >= index))
    row["firmware_summary"] = "\n".join(descriptions)
    row.update(result_statistics(folder, actual))
    row["reliability_note"] = "Unavailable: no complete, verified attempt ledger"
    for kind, calculate in (("attempts", attempt_metrics), ("power", power_metrics)):
        filename = saved.get(kind + "_file")
        if not filename:
            continue
        evidence = folder / "capture" / filename
        if evidence.parent.resolve() != (folder / "capture").resolve() or linked(evidence):
            raise ValueError("Evidence must remain inside the trial capture folder.")
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != saved.get(kind + "_sha256"):
            raise ValueError(f"{kind} evidence changed after import; import it again.")
        metrics = calculate(evidence) if kind == "attempts" else calculate(evidence, saved.get("power_cycles"))
        row.update(metrics)
        if kind == "attempts":
            if saved.get("attempt_coverage_complete") and not metrics["unresolved"]:
                row["valid_fraction"] = metrics["valid"] / metrics["attempts"]
                row["reliability_note"] = "User-confirmed complete attempt ledger"
            else:
                row["reliability_note"] = "Incomplete coverage or unresolved attempts; percentage withheld"
            if metrics["mean_latency_ms"] is not None:
                row["latency_definition"] = "Attempt request to valid result, paired clock_id; " + saved.get("attempts_evidence", "")
                row["measured_latency_ms"] = metrics["mean_latency_ms"]
    telemetry = folder / "capture/analyzer_telemetry.jsonl"
    row["diagnostics"] = None
    if telemetry.exists():
        diagnostics, runtime = 0, None
        for line in telemetry.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("Malformed Analyzer telemetry event.")
            if event.get("kind") == "state":
                diagnostics += 1
            elif event.get("kind") == "configuration":
                runtime = event.get("configuration")
        row["diagnostics"] = diagnostics
        row["analyzer_configuration"] = json.dumps(runtime, sort_keys=True) if runtime else None
        if runtime:
            if not isinstance(runtime, dict) or not isinstance(runtime.get("requested", {}), dict):
                raise ValueError("Malformed requested Analyzer configuration.")
            requested = runtime.get("requested") or {}
            algorithm = {0: "Accuracy tracking", 1: "Stationary", 2: "Latency tracking"}.get(requested.get("algoMode"), "Unknown algorithm")
            channels = {1: "37 channels", 2: "72 channels"}.get(requested.get("chMapPreset"), "Unknown channels")
            scheduling = {0: "Frequency scheduling", 1: "Energy scheduling"}.get(requested.get("scheduling"), "Custom/unknown scheduling")
            row["analyzer_summary"] = (f"{runtime.get('profile', 'Unknown profile')}\n"
                f"{algorithm}, {channels}\n{scheduling}; start accepted: {runtime.get('start_accepted')}")
    relative = folder.relative_to(live)
    row["name"] = relative.name
    row["batch"] = relative.parts[1] if len(relative.parts) > 2 and relative.parts[0] == "guided" else "Single trials"
    row["recording_status"] = "Complete" if row["complete"] else "Partial"
    row["stop_label"] = STOP_LABELS.get(row.get("stop_reason"), row.get("stop_reason") or "Unknown")
    row.update(evidence_status(row))
    return row


def evidence_status(row):
    """Report evidence availability, never infer radio success from a distance."""
    return {
        "attempt_status": "Missing" if row.get("attempts") is None else
                          "Complete" if row.get("valid_fraction") is not None else "Incomplete",
        "latency_status": "Missing" if number(row.get("measured_latency_ms")) is None else "Measured",
        "power_status": "Missing" if number(row.get("mean_current_ma")) is None else
                        "Current only" if number(row.get("energy_per_cycle_mj")) is None else "Current + energy",
        "tx_status": "Verified" if all(number(row.get(key)) is not None for key in
                                      ("initiator_tx_dbm", "reflector_tx_dbm")) and row.get("tx_power_evidence") else "Missing",
    }


def natural_key(value):
    return tuple((1, float(part)) if re.fullmatch(r"\d+(?:\.\d+)?", part) else (0, part.casefold())
                 for part in re.split(r"(\d+(?:\.\d+)?)", str(value)))


def trial_sort_key(row):
    return (natural_key(row["batch"]), *(number(row.get(key)) if number(row.get(key)) is not None else math.inf
            for key in ("actual_m", "initiator_yaw", "reflector_yaw")), natural_key(row["name"]))


def setup_signature(row):
    fields = ("fingerprint", "board_pair", "firmware", "environment", "obstruction", "polarization", "motion", "power_source",
              "initiator_height_m", "reflector_height_m", "analyzer_configuration", "initiator_tx_dbm",
              "reflector_tx_dbm", "rotated_board", "fixed_yaw")
    return tuple(str(row.get(key) or "").strip().casefold() if key in CONTEXT_FIELDS else str(row.get(key)) for key in fields)


def result_groups(rows):
    """Keep each batch/setup/type separate; reuse the same grouping in UI and Excel."""
    setups, grouped = {}, {}
    for row in rows:
        signature = (row["group"], setup_signature(row))
        if signature not in setups:
            setups[signature] = len(setups) + 1
        row["set"] = setups[signature]
        key = (row["batch"], signature)
        if key not in grouped:
            grouped[key] = {"id": hashlib.sha256(repr(key).encode()).hexdigest()[:16],
                            "name": row["batch"], "set": row["set"], "group": row["group"], "rows": []}
        grouped[key]["rows"].append(row)
    return list(grouped.values())


def planned_condition(row):
    rotated = row.get("rotated_board")
    if not row.get("complete") or rotated not in ("initiator", "reflector"):
        return None
    fixed = "reflector" if rotated == "initiator" else "initiator"
    distance, yaw = row.get("actual_m"), row.get(rotated + "_yaw")
    if (distance in PROTOCOL["distances_m"] and yaw in PROTOCOL["yaw_degrees"]
            and row.get(fixed + "_yaw") == row.get("fixed_yaw")):
        return distance, yaw
    return None


def overview_row(group):
    rows = group["rows"]
    errors = [error for row in rows for error in row["_filtered_errors"]]
    absolute = [abs(error) for error in errors]
    conditions = {point for row in rows if (point := planned_condition(row)) is not None}
    report = {"batch": group["name"], "set": group["set"], "group": group["group"],
            "environment": rows[0].get("environment"), "obstruction": rows[0].get("obstruction"),
            "capture_count": len(rows), "complete_count": sum(row["complete"] for row in rows),
            "condition_count": len(conditions) if rows[0].get("rotated_board") else None,
            "filtered_n": sum(row["filtered_n"] for row in rows), "error_n": len(errors),
            "filtered_mae_m": mean(absolute) if errors else None,
            "filtered_bias_m": mean(errors) if errors else None,
            "filtered_p95_error_m": percentile(absolute, 0.95) if errors else None,
            "missing_attempts": sum(row["attempt_status"] == "Missing" for row in rows),
            "incomplete_attempts": sum(row["attempt_status"] == "Incomplete" for row in rows),
            "missing_latency": sum(row["latency_status"] == "Missing" for row in rows),
            "missing_power": sum(row["power_status"] == "Missing" for row in rows),
            "missing_energy": sum(row.get("energy_per_cycle_mj") is None for row in rows),
            "missing_tx": sum(row["tx_status"] == "Missing" for row in rows)}
    gaps = [("attempt log", report["missing_attempts"]), ("incomplete log", report["incomplete_attempts"]),
            ("timing", report["missing_latency"]), ("current", report["missing_power"]),
            ("cycle energy", report["missing_energy"]), ("TX readback", report["missing_tx"])]
    report["evidence_summary"] = "; ".join(f"{label}: {count}" for label, count in gaps if count) or "None missing"
    return report


def collect_results(data=ROOT / "data"):
    live = Path(data) / "live"
    rows, warnings = [], []
    for folder in trial_folders(live):
        try:
            rows.append(trial_row(folder, live))
        except (OSError, ValueError, TypeError, csv.Error) as error:
            warnings.append(f"{folder.relative_to(live)}: {error}")
    rows.sort(key=trial_sort_key)
    result_groups(rows)
    return rows, warnings


def write_workbook(data, rows, warnings):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.views import Selection

    data = Path(data)
    groups = result_groups(rows)
    book = Workbook()
    book.remove(book.active)
    body_font = Font(name="Calibri", size=11, color="243746")
    heading_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    heading_fill = PatternFill("solid", fgColor="1F4E78")
    stripe_fill = PatternFill("solid", fgColor="EDF3F8")
    missing_fill = PatternFill("solid", fgColor="FFF2CC")
    body_alignment = Alignment(vertical="center", wrap_text=True)
    text_alignment = Alignment(wrap_text=True, vertical="top")

    def sheet(title, columns, values, note):
        page = book.create_sheet(title)
        page.append([title + " - refreshed " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")])
        page.append([note])
        page.merge_cells(start_row=2, start_column=1, end_row=2, end_column=min(6, len(columns)))
        page.row_dimensions[2].height = 38
        page.cell(2, 1).alignment = Alignment(wrap_text=True, vertical="center")
        page.append([label for _, label in columns])
        for row in values:
            record = []
            for key, _ in columns:
                value = row.get(key)
                if key == "recorded" and value:
                    value = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
                record.append(value)
            page.append(record)
        # Use one scrolling area to avoid duplicated rows/columns at frozen boundaries.
        page.freeze_panes = None
        page.sheet_view.topLeftCell = "A1"
        page.sheet_view.selection = [Selection(activeCell="A4", sqref="A4")]
        page.auto_filter.ref = f"A3:{get_column_letter(len(columns))}{max(3, page.max_row)}"
        for index, (key, label) in enumerate(columns, 1):
            page.column_dimensions[get_column_letter(index)].width = (
                32 if key in ("batch", "name") else 8 if key == "set" else 25 if key == "recorded" else
                36 if key == "evidence_summary" else
                40 if key == "plan" else 32 if key in ("board_pair", "fingerprint") else
                48 if key in ("source", "trial", "firmware_summary", "analyzer_summary") else
                38 if key in ("notes", "capture_notes", "tx_power_evidence", "latency_evidence", "latency_definition", "power_evidence") else
                min(26, max(14, len(label) * 0.7)))
            cell = page.cell(3, index)
            cell.font = heading_font
            cell.fill = heading_fill
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            for cells in page.iter_rows(min_row=4, max_row=page.max_row, min_col=index, max_col=index):
                cell = cells[0]
                cell.font = body_font
                cell.alignment = body_alignment
                if isinstance(cell.value, str):
                    cell.data_type = "s"  # Operator text and paths must never become Excel formulas.
                    cell.alignment = text_alignment
                elif isinstance(cell.value, datetime):
                    cell.number_format = "yyyy-mm-dd hh:mm:ss"
                elif isinstance(cell.value, float):
                    cell.number_format = "0.0%" if key == "valid_fraction" else "0.0" if "yaw" in key else "0.000"
                elif isinstance(cell.value, int):
                    cell.number_format = "0"
                if cell.row % 2 == 0:
                    cell.fill = stripe_fill
                if key == "evidence_summary" and cell.value != "None missing":
                    cell.fill = missing_fill
                if key.startswith("missing_") or key == "incomplete_attempts":
                    if cell.value:
                        cell.fill = missing_fill
        for row_index in range(4, page.max_row + 1):
            page.row_dimensions[row_index].height = 92 if title == "Configuration" else 60 if title == "Overview" else 36
        page.row_dimensions[3].height = 48
        page.sheet_view.zoomScale = 90
        page.sheet_view.showGridLines = False
        page.row_dimensions[1].height = 28
        page.cell(1, 1).font = Font(name="Calibri", size=16, bold=True, color="1F4E78")
        page.print_title_rows = "1:3"
        page.sheet_properties.pageSetUpPr.fitToPage = True
        page.page_setup.orientation = "landscape"
        page.page_setup.paperSize = page.PAPERSIZE_A3
        page.page_setup.fitToWidth = 1
        page.page_setup.fitToHeight = 0
        page.print_options.horizontalCentered = True
        page.sheet_properties.outlinePr.summaryRight = False
        return page

    sheet("Overview", OVERVIEW_COLUMNS, [overview_row(group) for group in groups],
          "One row per batch/setup. Errors use all retained readings with known distance, including partial captures. Yellow cells show missing evidence. See Definitions for short explanations.")
    note = "Filtered distance results. Blank means unavailable. Edit notes in results.py; Excel edits are replaced on refresh. Messages include the final recording tail. See Definitions for every label."
    for group in ("Normal trials", "Calibration", "Simulated", "Unclassified"):
        sheet(group, RESULT_COLUMNS, [row for row in rows if row["group"] == group], note)
    sheet("Configuration", CONFIG_COLUMNS, rows, "Requested settings are not flashed-firmware or RF readback. State messages are diagnostics, not unique failed attempts. Instrument current is averaged over its documented window.")
    normal_sets = {}
    for row in rows:
        if row["group"] == "Normal trials":
            normal_sets.setdefault(row["set"], []).append(row)
    coverage = []
    for index, values in (list(normal_sets.items()) or [(None, [])]):
        plan = values[0] if values else {"rotated_board": "reflector", "fixed_yaw": 0}
        rotated = plan["rotated_board"]
        fixed = "initiator" if rotated == "reflector" else "reflector"
        counts = {}
        for row in values:
            point = planned_condition(row)
            if point is not None:
                counts[point] = counts.get(point, 0) + 1
        for distance in PROTOCOL["distances_m"]:
            for yaw in PROTOCOL["yaw_degrees"]:
                coverage.append({"set": index, "environment": plan.get("environment"), "obstruction": plan.get("obstruction"),
                                 "actual_m": distance, "yaw": yaw, "count": counts.get((distance, yaw), 0),
                                 "plan": f"Rotate {rotated}; fixed {fixed} {plan['fixed_yaw']} deg" if rotated else "Rotation plan unknown"})
    checklist_note = ("Normal trials only. Each setup has 5 distances x 8 angles. A complete capture fills a condition; "
                      "it does not establish an accuracy pass. Setup numbers match Configuration." if normal_sets else
                      "No normal captures yet. These 40 conditions are the planned checklist; Setup stays blank until a normal capture exists.")
    sheet("Baseline checklist", CHECKLIST_COLUMNS, coverage, checklist_note)
    labels = {"valid_estimate_fraction": "Minimum valid estimate fraction",
              "mean_range_error_m": "Maximum mean range error (m)",
              "end_to_end_latency_ms": "Maximum end-to-end latency (ms)",
              "mean_reflector_current_ma": "Maximum mean reflector current (mA)"}
    definitions = {title: meaning for title, meaning in FIELDS.values()}
    definitions.update(TERMS)
    protocol_rows = [{"item": title, "value": meaning} for title, meaning in sorted(definitions.items(), key=lambda pair: pair[0].casefold())]
    for key, value in PROTOCOL["provisional_targets"].items():
        display = f"{value:.0%}" if key == "valid_estimate_fraction" else str(value)
        protocol_rows.append({"item": labels[key], "value": f"Proposed target: {display}. Confirm with Genie."})
    protocol_rows += [{"item": "Targets confirmed by Genie", "value": "Yes" if PROTOCOL["targets_confirmed_with_sponsor"] else "No; definitions and limits still need confirmation."},
                      {"item": "Target definitions to confirm", "value": "Mean error: signed or absolute? Timing: which endpoints? Current: which measurement window?"},
                      {"item": "Requirements source", "value": PROTOCOL["source"]}]
    protocol_rows.extend({"item": "Skipped trial", "value": warning} for warning in warnings)
    page = sheet("Definitions", [("item", "Label or term"), ("value", "Simple meaning")], protocol_rows,
                 "Main results use filtered readings. Empty cells mean unknown. Proposed targets are not automatic pass/fail rules.")
    page.column_dimensions["A"].width = 38
    page.column_dimensions["B"].width = 88
    for cells in page.iter_rows(min_row=4):
        cells[1].alignment = Alignment(wrap_text=True, vertical="top")
        page.row_dimensions[cells[0].row].height = 32
    book.move_sheet(page, offset=1 - book.index(page))
    output = data / "results/master_results.xlsx"
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, suffix=".xlsx", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        book.save(temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
        book.close()
    return output


def refresh_results(data=ROOT / "data"):
    rows, warnings = collect_results(data)
    return write_workbook(data, rows, warnings), rows, warnings


def refresh_after_capture(data):
    try:
        output, _, warnings = refresh_results(data)
        print(f"Master results updated: {output}", flush=True)
        for warning in warnings:
            print(f"Master results skipped {warning}", flush=True)
    except (OSError, ValueError, ImportError) as error:
        print(f"Capture saved. Master results could not refresh: {error}. Close Excel and run results.py to retry.", flush=True)
