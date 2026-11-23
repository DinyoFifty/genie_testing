"""Guide the 40 distance/yaw conditions and retain resumable capture progress."""

from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from uuid import uuid4

from flash import FIRMWARE, PROJECTS
from .genie_live import analyzer_session, finite, last_board_heights, run_live
from .trial_storage import (batch_active, condition_name, linked, local_timestamp,
                            migrate_single_trials, normalize_conditions, normalize_yaw,
                            recycle_folder, trial_folders)


DISTANCES = (1, 2, 5, 10, 20)
ANGLES = tuple(range(0, 360, 45))
ESTIMATE_TARGET = 100
PROGRESS_VERSION = 1


def _definition(source, name, path):
    matches = re.findall(rf"^[ \t]*#define[ \t]+{re.escape(name)}[ \t]+([^\r\n]+)",
                         source, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"Expected one definition of {name} in {path}.")
    return matches[0].split("//", 1)[0].strip()


def snapshot_firmware(firmware=FIRMWARE):
    """Read the applied antenna mode and physical offset baseline."""
    projects = {}
    for role, project in PROJECTS.items():
        config = Path(firmware) / project / "config"
        header = config / "sl_rail_util_cs_antenna_offset_config.h"
        source = header.read_text(encoding="utf-8-sig")
        aox_path = config / "sl_rail_util_aox_config.h"
        aox = aox_path.read_text(encoding="utf-8-sig")
        count = int(_definition(source, "SL_RAIL_UTIL_CS_ANTENNA_COUNT", header), 0)
        pins = int(_definition(aox, "SL_RAIL_UTIL_AOX_ANTENNA_PIN_COUNT", aox_path), 0)
        if count not in (1, 2) or pins != count - 1:
            raise ValueError("The antenna count and switch control settings disagree. "
                             "Run calibration.py or normal.py, then flash.py before collecting this batch.")
        antenna = int(_definition(source, "GENIE_TEST_ANTENNA", header), 0) if count == 1 else None
        if count == 1 and antenna not in (1, 2):
            raise ValueError("Select physical ANT1 or ANT2 for isolated calibration.")
        kind = _definition(source, "SL_RAIL_UTIL_CS_ANTENNA_CONFIG_TYPE", header)
        kinds = {f"SL_RAIL_UTIL_CS_ANTENNA_OFFSET_CONFIG_TYPE_{name.upper()}": name
                 for name in ("wireless", "wired")}
        if kind not in kinds:
            raise ValueError(f"Unknown antenna offset configuration in {header}.")
        if kinds[kind] != "wireless":
            raise ValueError("Guided distance calibration requires wireless antenna offsets on both boards.")
        values = {"project": project, "physical_antenna": antenna,
                  "available_antennas": count, "aox_control_pins": pins,
                  "offset_config_type": kinds[kind]}
        for connection in ("wireless", "wired"):
            offsets = [int(_definition(source, f"GENIE_ANT{number}_{connection.upper()}_OFFSET_CM",
                                       header), 0) for number in (1, 2)]
            if any(not -32768 <= value <= 32767 for value in offsets):
                raise ValueError(f"An antenna offset exceeds the firmware range in {header}.")
            array = _definition(source, f"SL_RAIL_UTIL_CS_ANTENNA_OFFSET_{connection.upper()}_CM", header)
            elements = [value.strip() for value in array.strip("{} ").split(",")]
            allowed = [{f"GENIE_ANT{number}_{connection.upper()}_OFFSET_CM", str(value)}
                       for number, value in enumerate(offsets, 1)]
            if count == 1:
                allowed = [allowed[antenna - 1] | {f"GENIE_SELECTED_{connection.upper()}_OFFSET_CM"}, {"0"}]
            if (len(elements) != 4 or elements[0] not in allowed[0]
                    or elements[1] not in allowed[1] or elements[2:] != ["0", "0"]):
                raise ValueError(f"The applied {connection} offset array disagrees with the selected antenna in {header}.")
            values[f"{connection}_offsets_cm"] = [*offsets, 0, 0]
        projects[role] = values
    if len({values["available_antennas"] for values in projects.values()}) != 1:
        raise ValueError("Select the same firmware mode on both boards with calibration.py or normal.py, then flash.py.")
    mode = "isolated" if count == 1 else "normal"
    if mode == "isolated" and len({values["physical_antenna"] for values in projects.values()}) != 1:
        raise ValueError("Select the same physical antenna on both boards for guided calibration.")
    snapshot = {"mode": mode, "projects": projects}
    snapshot["fingerprint"] = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return snapshot


def save_progress(path, progress):
    """Replace progress atomically after flushing the complete JSON document."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                prefix=".progress-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(progress, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _batch_lock(path):
    """Keep two guided processes from advancing the same batch simultaneously."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        marker = path.with_name(".progress.active.json" if path.name.startswith(".") else "progress.active.json")
        if os.name == "nt":
            import msvcrt

            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("This guided batch is already open in another Genie process.") from None
            try:
                marker.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
                yield
            finally:
                marker.unlink(missing_ok=True)
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                marker.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
                yield
            finally:
                marker.unlink(missing_ok=True)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def condition_key(progress, distance, conditions):
    """Return a planned point only when distance and both board yaws agree."""
    rotated = progress["rotated_board"]
    fixed = "reflector" if rotated == "initiator" else "initiator"
    angle = normalize_yaw(conditions.get(f"{rotated}_yaw_deg"))
    fixed_angle = normalize_yaw(conditions.get(f"{fixed}_yaw_deg"))
    if (not finite(distance) or distance not in DISTANCES or angle not in ANGLES
            or fixed_angle is None or fixed_angle != progress["fixed_yaw_deg"]):
        return None
    return f"{distance:g}m_{angle:g}deg"


def pending_conditions(progress):
    completed = set(progress.get("completed_conditions", []))
    return [(distance, angle) for distance in DISTANCES for angle in ANGLES
            if f"{distance:g}m_{angle:g}deg" not in completed]


def _refresh_completed(progress):
    progress["completed_conditions"] = sorted({attempt["condition"] for attempt in progress["attempts"]
                                                if attempt.get("status") == "complete"})


def _board_pair(value):
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(item, str) and item for item in value):
        return list(value)
    return None


def _apply_summary(progress, summary, session, live_dir=None):
    metadata = summary.get("calibration")
    if not isinstance(metadata, dict) or metadata.get("batch_id") != progress["batch_id"]:
        return
    attempt_id = metadata.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return
    attempt = next((item for item in progress["attempts"] if item["attempt_id"] == attempt_id), None)
    if attempt is None:
        attempt = {"attempt_id": attempt_id, "started_at_utc": metadata.get("started_at_utc")}
        progress["attempts"].append(attempt)
    conditions = summary.get("conditions")
    conditions = conditions if isinstance(conditions, dict) else {}
    count = summary.get("finite_filtered_estimate_count")
    count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
    session = Path(session)
    live_folder = session.relative_to(live_dir).as_posix() if live_dir is not None else session.name
    attempt.update({"live_folder": live_folder, "actual_distance_m": summary.get("actual_distance_m"),
                    "conditions": conditions, "finite_filtered_estimate_count": count,
                    "user_stopped": summary.get("user_stopped") is True,
                    "condition": condition_key(progress, summary.get("actual_distance_m"), conditions),
                    "status": "incomplete", "detail": "Capture has fewer than 100 saved filtered estimates."})
    captured = metadata.get("firmware")
    if captured != progress["firmware"]:
        attempt.update(status="excluded", detail="Captured firmware baseline differs from this batch.")
        return
    if any(metadata.get(key) != progress[key] for key in ("rotated_board", "fixed_yaw_deg", "setup")):
        attempt.update(status="excluded", detail="Captured setup differs from this batch.")
        return
    setup = {key: value for key, value in conditions.items()
             if key in ("initiator_height_m", "reflector_height_m", "setup_notes")}
    if setup != progress["setup"]:
        attempt.update(status="excluded", detail="Board heights or setup notes differ from this batch.")
        return
    pair = _board_pair(summary.get("board_pair"))
    if pair is not None and progress.get("board_pair") is None:
        progress["board_pair"] = pair
    if pair is None:
        attempt.update(detail="Capture does not identify both boards.")
        return
    if pair != progress.get("board_pair"):
        attempt.update(status="excluded", detail="Capture uses different boards from this batch.")
        return
    if (summary.get("estimate_target") != ESTIMATE_TARGET or count != ESTIMATE_TARGET
            or summary.get("target_reached") is not True or summary.get("stop_reason") != "target_reached"):
        return
    if summary.get("reports_saved") is not True:
        attempt.update(detail="Capture reports were interrupted before saving finished.")
        return
    reports = ("capture/raw.jsonl", "capture/measurements.csv", "measurements.xlsx",
               "readings_in_order.png", "filtered_distance_distribution.png")
    if not all((session / name).is_file() for name in reports):
        attempt.update(detail="Some capture or report files are missing.")
        return
    if attempt["condition"] is None:
        attempt.update(status="extra", detail="Saved outside the planned distance/yaw conditions.")
        return
    attempt.update(status="complete", detail="100 filtered estimates and reports saved.")


def recover_progress(progress, live_dir):
    """Recover finalized captures even when progress was interrupted after export."""
    for attempt in progress["attempts"]:
        if attempt.get("live_folder") and attempt.get("status") in ("complete", "extra", "incomplete"):
            attempt.update(status="incomplete", detail="Saved capture summary is unavailable.")
    summaries = []
    for session in trial_folders(live_dir, include_temporary=True):
        path = session / "capture/summary.json"
        try:
            summary = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(summary, dict):
            continue
        metadata = summary.get("calibration")
        if isinstance(metadata, dict) and metadata.get("batch_id") == progress["batch_id"]:
            started = metadata.get("started_at_utc") or summary.get("session_started_at_utc") or ""
            summaries.append((str(started), path.parent.parent, summary))
    for _, session, summary in sorted(summaries, key=lambda item: (item[0], item[1].name)):
        _apply_summary(progress, summary, session, Path(live_dir))
    _refresh_completed(progress)
    return progress


def begin_attempt(progress, path, distance, conditions):
    """Persist the intended condition before asking Analyzer to record."""
    conditions = normalize_conditions(conditions)
    started = datetime.now(timezone.utc).isoformat()
    attempt_id = uuid4().hex
    progress["attempts"].append({"attempt_id": attempt_id, "started_at_utc": started,
                                 "actual_distance_m": distance, "conditions": conditions,
                                 "condition": condition_key(progress, distance, conditions),
                                 "status": "pending", "finite_filtered_estimate_count": 0,
                                 "detail": "Recording has not been finalized."})
    save_progress(path, progress)
    return {key: progress[key] for key in ("batch_id", "firmware", "rotated_board", "fixed_yaw_deg", "setup")} | {
        "batch_name": progress.get("batch_name", progress["batch_id"]),
        "attempt_id": attempt_id, "started_at_utc": started}


def _number(question, default=None, *, nonnegative=False):
    while True:
        answer = input(question).strip()
        if answer.casefold() == "skip":
            return None
        if not answer:
            return default
        try:
            value = float(answer)
        except ValueError:
            value = None
        if finite(value) and (not nonnegative or value >= 0):
            return value
        print("Enter a finite number or skip.", flush=True)


def _new_progress(snapshot, live_dir):
    print("Yaw is measured clockwise from each board's 0-degree baseline, viewed from above (0 to 359 degrees).", flush=True)
    while True:
        rotated = input("Board to rotate [reflector/initiator, Enter = reflector]? ").strip().casefold()
        if rotated in ("", "reflector", "initiator"):
            rotated = rotated or "reflector"
            break
        print("Enter reflector or initiator.", flush=True)
    fixed = "reflector" if rotated == "initiator" else "initiator"
    fixed_yaw = None
    while fixed_yaw is None:
        fixed_yaw = normalize_yaw(_number(f"Fixed {fixed} yaw in degrees [Enter = 0]? ", 0.0))
        if fixed_yaw is None:
            print("Enter the fixed board's yaw to define the 40 planned conditions. Enter uses 0 degrees.", flush=True)
    previous = last_board_heights(live_dir)
    setup = {}
    for role in PROJECTS:
        key = f"{role}_height_m"
        default = previous.get(key)
        hint = f" [Enter = {default:g}]" if default is not None else ""
        value = _number(f"{role.capitalize()} height in meters{hint}, or skip? ", default, nonnegative=True)
        if value is not None:
            setup[key] = value
    notes = input("Setup notes, or skip? ").strip()
    if notes and notes.casefold() != "skip":
        setup["setup_notes"] = notes
    now = datetime.now(timezone.utc)
    return {"version": PROGRESS_VERSION, "batch_id": uuid4().hex,
            "created_at_utc": now.isoformat(), "firmware": snapshot, "rotated_board": rotated,
            "fixed_yaw_deg": fixed_yaw, "setup": setup, "board_pair": None,
            "attempts": [], "completed_conditions": []}


def _load_progress(path):
    progress = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if (not isinstance(progress, dict) or progress.get("version") != PROGRESS_VERSION
            or not isinstance(progress.get("batch_id"), str)
            or not isinstance(progress.get("firmware"), dict)
            or not isinstance(progress["firmware"].get("fingerprint"), str)
            or not isinstance(progress.get("setup"), dict)
            or not isinstance(progress.get("attempts"), list)
            or any(not isinstance(attempt, dict) or not isinstance(attempt.get("attempt_id"), str)
                   for attempt in progress["attempts"])
            or "fixed_yaw_deg" not in progress
            or progress.get("rotated_board") not in PROJECTS):
        raise ValueError("Unsupported guided progress document.")
    return progress


def _batch_folder(path):
    path = Path(path)
    return next((parent for parent in path.parents if parent.parent.name in ("guided", "calibration")), path.parent)


def _progress_path(batch):
    batch = Path(batch)
    paths = [path for pattern in (".progress.json", "*/capture/progress.json", "capture/progress.json", "progress.json")
             for path in sorted(batch.glob(pattern))]
    if len(paths) > 1:
        raise ValueError(f"Multiple progress files in {batch.name}; none was replaced.")
    return paths[0] if paths else batch / ".progress.json"


def _lock_path(path):
    return _batch_folder(path) / ".progress.lock"


def _place_progress(path, progress):
    """Keep batch progress in the first saved condition's existing capture folder."""
    path = Path(path)
    batch = _batch_folder(path)
    destination = batch / ".progress.json"
    if path.parent.name == "capture" and path.parent.parent.parent == batch:
        return path
    for summary_path in sorted(batch.glob("*/capture/summary.json")):
        if linked(summary_path.parent.parent) or summary_path.parent.parent.name.startswith((".", "_")):
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        metadata = summary.get("calibration")
        if isinstance(metadata, dict) and metadata.get("batch_id") == progress["batch_id"]:
            destination = summary_path.with_name("progress.json")
            break
    if path != destination:
        if destination.exists():
            raise ValueError("The destination progress file already exists; neither was replaced.")
        path.rename(destination)
    return destination


def _create_batch_folder(progress, live_dir, number=None):
    guided = Path(live_dir) / "guided"
    guided.mkdir(parents=True, exist_ok=True)
    numbers = [int(match[1]) for path in guided.iterdir()
               if (match := re.match(r"(?:Batch|Trial)(\d+)_", path.name))]
    number = max(numbers, default=0) + 1 if number is None else number
    snapshot = progress["firmware"]
    pair = ("DUAL" if snapshot["mode"] == "normal" else
            "-".join(f"ANT{snapshot['projects'][role]['physical_antenna']}" for role in PROJECTS))
    while True:
        path = guided / f"Batch{number}_{pair}"
        try:
            path.mkdir()
        except FileExistsError:
            number += 1
        else:
            return path


def _retire_capture(folder):
    try:
        recycle_folder(folder)
    except OSError as error:
        print(f"The replaced result is retained at {folder}: {error}", flush=True)


def publish_trial(progress, path, session, live_dir, *, recovering=False):
    """Promote a finished capture; a failed retry never replaces a completed trial."""
    session, live_dir = Path(session).resolve(), Path(live_dir).resolve()
    batch = _batch_folder(path).resolve()
    path = _progress_path(batch)
    if not session.is_relative_to(live_dir) or linked(session) or not batch.is_relative_to(live_dir / "guided"):
        raise ValueError("Guided captures must stay inside this project's live folder.")
    summary_path = session / "capture/summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    _apply_summary(progress, summary, session, live_dir)
    attempt_id = summary["calibration"]["attempt_id"]
    attempt = next(item for item in progress["attempts"] if item["attempt_id"] == attempt_id)
    name = condition_name(summary.get("actual_distance_m"), summary.get("conditions", {}))
    complete = attempt["status"] in ("complete", "extra")
    if complete and "Unknown" in name:
        # Unspecified conditions cannot establish that two captures are the same test.
        name += "_" + attempt_id[:8]
    if complete:
        target = batch / name
    else:
        incomplete = batch / "_incomplete"
        incomplete.mkdir(exist_ok=True)
        stamp = local_timestamp(summary.get("session_started_at_utc") or summary["calibration"]["started_at_utc"])
        target = incomplete / f"{name}_{stamp}_{attempt_id[:8]}"
    if session == target:
        return session
    if target.exists() and not complete:
        raise ValueError(f"An interrupted capture already exists at {target}.")
    backup = None
    if target.exists():
        if linked(target):
            raise ValueError("Cannot replace a linked trial folder.")
        previous = json.loads((target / "capture/summary.json").read_text(encoding="utf-8"))
        if previous.get("calibration", {}).get("batch_id") != progress["batch_id"]:
            raise ValueError("The existing condition folder belongs to another batch.")
        if recovering:
            def started(value):
                text = value.get("calibration", {}).get("started_at_utc") or value.get("session_started_at_utc")
                try:
                    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
                except (AttributeError, ValueError):
                    return datetime.min.replace(tzinfo=timezone.utc)

            if started(previous) > started(summary):
                retired = batch / f".replaced-{uuid4().hex}"
                session.rename(retired)
                attempt.update(status="superseded", detail="A newer completed result already exists.")
                _apply_summary(progress, previous, target, live_dir)
                _refresh_completed(progress)
                save_progress(path, progress)
                _retire_capture(retired)
                return target
        if path.is_relative_to(target):
            # The condition being retried also holds the whole batch's progress.
            # Keep it outside the replaced capture until the new capture is in place.
            temporary_state = batch / ".progress.json"
            if temporary_state.exists():
                raise ValueError("A saved batch progress file already exists outside this condition.")
            path.rename(temporary_state)
            path = temporary_state
        backup = batch / f".replaced-{uuid4().hex}"
        target.rename(backup)
    try:
        session.rename(target)
    except BaseException:
        if backup is not None:
            backup.rename(target)
        raise
    if complete:
        for older in progress["attempts"]:
            if (older["attempt_id"] != attempt_id and older.get("status") in ("complete", "extra")
                    and condition_name(older.get("actual_distance_m"), older.get("conditions", {})) == name):
                older.update(status="superseded", detail="Replaced by a completed retry.")
    summary["calibration"]["batch_name"] = progress.get("batch_name", batch.name)
    save_progress(target / "capture/summary.json", summary)
    _apply_summary(progress, summary, target, live_dir)
    _refresh_completed(progress)
    save_progress(path, progress)
    _place_progress(path, progress)
    if backup is not None:
        _retire_capture(backup)
    return target


def organize_batch(progress, path, live_dir):
    """Group old flat captures and recover captures interrupted during publication."""
    captures = []
    for session in trial_folders(live_dir, include_temporary=True):
        try:
            summary = json.loads((session / "capture/summary.json").read_text(encoding="utf-8"))
            metadata = summary.get("calibration")
            if isinstance(metadata, dict) and metadata.get("batch_id") == progress["batch_id"]:
                captures.append((metadata.get("started_at_utc", ""), session))
        except (OSError, ValueError, AttributeError):
            continue
    for _, session in sorted(captures, key=lambda item: (item[0], str(item[1]))):
        publish_trial(progress, path, session, live_dir, recovering=True)
        path = _progress_path(_batch_folder(path))
    recover_progress(progress, live_dir)
    save_progress(path, progress)
    pending = _batch_folder(path) / ".pending"
    if pending.is_dir() and not any(pending.iterdir()):
        pending.rmdir()
    return path


def migrate_batches(live_dir, calibration_dir):
    """Use short batch names and keep progress inside an existing condition's capture."""
    guided = Path(live_dir).resolve() / "guided"
    legacy = Path(calibration_dir).resolve()
    roots = [guided] if legacy == guided else [guided, legacy]
    paths = [path for root in roots for pattern in ("*/*/capture/progress.json", "*/.progress.json",
                                                   "*/capture/progress.json", "*/progress.json")
             for path in sorted(root.glob(pattern))]
    migrated = []
    for path in paths:
        batch = _batch_folder(path)
        if linked(batch) or linked(path.parent) or batch_active(batch):
            continue
        try:
            progress = _load_progress(path)
            if _progress_path(batch) != path:
                raise ValueError("The batch progress path is ambiguous.")
            old_name = batch.name
            old_prefix = f"guided/{old_name}/"
            match = re.match(r"Trial(\d+)_", old_name)
            target = batch
            if batch.parent != guided or match:
                # Verify the old writer has released its lock before moving its folder.
                with _batch_lock(_lock_path(path)):
                    pass
                target = _create_batch_folder(progress, live_dir, int(match[1]) if match else None)
                target.rmdir()
                batch.rename(target)
                path = target / path.relative_to(batch)
            original_path = path
            path = _place_progress(path, progress)
            old_lock = original_path.with_name("progress.lock")
            if old_lock.exists() and old_lock != _lock_path(path):
                old_lock.unlink()
            if original_path.parent == target / "capture" and not any(original_path.parent.iterdir()):
                original_path.parent.rmdir()
            if target == batch and original_path == path and progress.get("batch_name") == target.name and not match:
                continue
            progress["batch_name"] = target.name
            for attempt in progress["attempts"]:
                previous = attempt.get("live_folder", "")
                if previous.startswith(old_prefix):
                    attempt["live_folder"] = f"guided/{target.name}/" + previous[len(old_prefix):]
            with _batch_lock(_lock_path(path)):
                save_progress(path, progress)
                for session in trial_folders(target, include_temporary=True):
                    summary_path = session / "capture/summary.json"
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    metadata = summary.get("calibration")
                    if isinstance(metadata, dict) and metadata.get("batch_id") == progress["batch_id"]:
                        metadata["batch_name"] = target.name
                        save_progress(summary_path, summary)
                organize_batch(progress, path, live_dir)
            migrated.append((batch, target))
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Could not update saved guided batch {batch.name}: {error}", flush=True)
    return migrated


def delete_batch(path, live_dir):
    path = Path(path).resolve()
    batch = _batch_folder(path)
    guided = (Path(live_dir) / "guided").resolve()
    if batch.parent != guided or linked(batch) or linked(path.parent):
        raise ValueError("Select one guided batch inside data/live/guided.")
    if batch_active(batch):
        raise RuntimeError("This batch is recording. Stop its guided session before deleting it.")
    with _batch_lock(_lock_path(path)):
        pass
    recycle_folder(batch)


def choose_batch(snapshot, live_dir, calibration_dir):
    migrate_single_trials(live_dir)
    migrate_batches(live_dir, calibration_dir)
    guided = Path(live_dir) / "guided"
    managed = False
    while True:
        batches = []
        for batch in sorted(guided.iterdir(), reverse=True) if guided.exists() else []:
            if not batch.is_dir() or linked(batch):
                continue
            try:
                path = _progress_path(batch)
                progress = _load_progress(path)
                recover_progress(progress, live_dir)
            except (OSError, ValueError):
                print(f"Could not read saved guided progress in {batch.name}.", flush=True)
                continue
            batches.append((path, progress))
        batches.sort(key=lambda item: item[1].get("created_at_utc", ""), reverse=True)
        compatible = [index for index, (_, progress) in enumerate(batches, 1) if progress["firmware"] == snapshot]
        if not batches:
            choice = (input("No saved batches. Enter new or done [Enter = new]? ").strip().casefold() or "new"
                      if managed else "new")
        else:
            managed = True
            for index, (path, progress) in enumerate(batches, 1):
                different = " (different antenna/settings)" if index not in compatible else ""
                print(f"{index}. {_batch_folder(path).name} — {len(progress['completed_conditions'])}/40 complete, "
                      f"rotate {progress['rotated_board']}{different}", flush=True)
            default = str(compatible[0]) if compatible else "new"
            choice = input(f"Resume batch [Enter = {default}], new, delete NUMBER, or done? ").strip().casefold() or default
        if choice == "done":
            return None
        if choice == "new":
            progress = _new_progress(snapshot, live_dir)
            batch = _create_batch_folder(progress, live_dir)
            progress["batch_name"] = batch.name
            path = batch / ".progress.json"
            save_progress(path, progress)
            return path, progress
        if choice.startswith("delete "):
            number = choice[7:].strip()
            if number.isdigit() and 1 <= int(number) <= len(batches):
                path, _ = batches[int(number) - 1]
                confirm = input(f"Move {_batch_folder(path).name} and all its trials to the Recycle Bin? [y/N] ").strip().casefold()
                if confirm in ("y", "yes"):
                    try:
                        delete_batch(path, live_dir)
                    except (OSError, RuntimeError, ValueError) as error:
                        print(f"Could not delete batch: {error}", flush=True)
                continue
        if choice.isdigit() and int(choice) in compatible:
            return batches[int(choice) - 1]
        print("Choose a compatible batch, new, delete NUMBER, or done.", flush=True)


def show_status(progress):
    completed = set(progress["completed_conditions"])
    print(f"Guided progress {len(completed)}/40 planned conditions complete. "
          f"{len(progress['attempts'])} capture attempts.", flush=True)
    for distance in DISTANCES:
        missing = [str(angle) for angle in ANGLES if f"{distance:g}m_{angle:g}deg" not in completed]
        detail = ", ".join(missing) if missing else "all complete"
        print(f"{distance:g} m — {8 - len(missing)}/8 complete. Pending yaw {detail}", flush=True)


def _prompt_trial(progress):
    pending = pending_conditions(progress)
    previous_distance = next((attempt.get("actual_distance_m") for attempt in reversed(progress["attempts"])
                              if finite(attempt.get("actual_distance_m")) and attempt["actual_distance_m"] in DISTANCES), None)
    current_distance = [point for point in pending if point[0] == previous_distance]
    distance, angle = (current_distance or pending or [(DISTANCES[0], ANGLES[0])])[0]
    answer = input(f"Actual distance in meters [Enter = {distance:g}], status, or done? ").strip().casefold()
    if answer in ("done", "status"):
        return answer
    if answer == "skip":
        distance = None
    elif answer:
        try:
            distance = float(answer)
        except ValueError:
            print("Enter a nonnegative distance in meters, skip, status, or done.", flush=True)
            return "retry"
        if not finite(distance) or distance < 0:
            print("Enter a nonnegative finite distance.", flush=True)
            return "retry"
    if distance in DISTANCES:
        angle = next((yaw for point, yaw in pending if point == distance), ANGLES[0])
    conditions = dict(progress["setup"])
    for role in PROJECTS:
        suggested = angle if role == progress["rotated_board"] else progress["fixed_yaw_deg"]
        default = f" [Enter = {suggested:g}]" if suggested is not None else ""
        yaw = _number(f"{role.capitalize()} yaw in degrees{default}, or skip? ", suggested)
        if yaw is not None:
            conditions[f"{role}_yaw_deg"] = normalize_yaw(yaw)
    if condition_key(progress, distance, conditions) is None:
        print("This condition will be saved as an extra trial outside the 40 planned points.", flush=True)
    elif condition_key(progress, distance, conditions) in progress["completed_conditions"]:
        print("Retrying a completed condition. A successful capture replaces its previous result; "
              "an interrupted capture keeps the previous result.", flush=True)
    while True:
        action = input("Press Enter to record 100 filtered estimates, or enter status/done. ").strip().casefold()
        if not action:
            return distance, conditions
        if action == "done":
            return "done"
        if action == "status":
            show_status(progress)
        else:
            print("Press Enter to record, or enter status or done.", flush=True)


def run_guided(folder, out, *, plot=True, interval=0.5):
    """Record one connected Analyzer session while guiding resumable conditions."""
    if not finite(interval) or interval <= 0:
        raise ValueError("Polling interval must be positive and finite.")
    live_dir = Path(out).resolve()
    snapshot = snapshot_firmware()
    selected = choose_batch(snapshot, live_dir, live_dir.parent / "calibration")
    if selected is None:
        return None
    path, _ = selected
    with _batch_lock(_lock_path(path)), ExitStack() as connections:
        progress = recover_progress(_load_progress(path), live_dir)
        batch = _batch_folder(path)
        progress.setdefault("batch_name", batch.name)
        path = organize_batch(progress, path, live_dir)
        save_progress(path, progress)
        mode_label = "Normal dual-antenna testing" if snapshot["mode"] == "normal" else "Isolated-antenna calibration"
        print(mode_label, flush=True)
        for role, settings in progress["firmware"]["projects"].items():
            antenna = settings["physical_antenna"]
            offsets = settings["wireless_offsets_cm"]
            details = (f"ANT1 + ANT2, saved wireless offsets {offsets[0]}, {offsets[1]} cm" if antenna is None else
                       f"ANT{antenna}, saved wireless offset {offsets[antenna - 1]} cm")
            print(f"{role.capitalize()} {details}.", flush=True)
        print("Batch uses saved project settings. Flash both boards after configuration changes.", flush=True)
        show_status(progress)
        client = None
        try:
            while True:
                trial = _prompt_trial(progress)
                if trial == "done":
                    break
                if trial == "status":
                    show_status(progress)
                    continue
                if trial == "retry":
                    continue
                if snapshot_firmware() != progress["firmware"]:
                    print("Firmware settings changed. Progress is saved. Flash the boards and start a new batch.", flush=True)
                    break
                distance, conditions = trial
                metadata = begin_attempt(progress, path, distance, conditions)
                if client is None:
                    client = connections.enter_context(analyzer_session(folder))
                if snapshot_firmware() != progress["firmware"]:
                    print("Firmware settings changed while connecting. Progress is saved. Start a new batch after flashing.", flush=True)
                    break
                capture = client.start(max_estimates=ESTIMATE_TARGET)
                session = run_live(folder=folder, out=batch / ".pending", interval=interval, plot=plot,
                                   field="distanceMain", actual_distance=distance, conditions=conditions,
                                   recording=capture, max_estimates=ESTIMATE_TARGET, calibration=metadata)
                session = publish_trial(progress, path, session, live_dir)
                path = _progress_path(batch)
                recover_progress(progress, live_dir)
                save_progress(path, progress)
                attempt = next(item for item in progress["attempts"] if item["attempt_id"] == metadata["attempt_id"])
                print(f"Saved {Path(session).name}. {attempt['detail']}", flush=True)
                show_status(progress)
                if attempt.get("user_stopped"):
                    print("Guided collection stopped. Saved progress can be resumed next time.", flush=True)
                    break
                if not client.running():
                    print("Analyzer connection ended. Relaunch Genie to resume this batch.", flush=True)
                    break
        except (KeyboardInterrupt, EOFError):
            print("Guided collection stopped. Saved progress can be resumed next time.", flush=True)
        finally:
            path = _progress_path(batch)
            recover_progress(progress, live_dir)
            save_progress(path, progress)
            pending = batch / ".pending"
            if pending.is_dir() and not any(pending.iterdir()):
                pending.rmdir()
    return path
