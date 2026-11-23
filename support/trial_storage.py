"""Names, discovery, and recycling for local trial folders."""

from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess


def local_timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    value = value.astimezone()
    hour = value.hour % 12 or 12
    period = "AM" if value.hour < 12 else "PM"
    return f"{value:%Y-%m-%d}_{hour}-{value:%M}{period}"


def normalize_yaw(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value % 360


def normalize_conditions(conditions):
    conditions = dict(conditions or {})
    for key in ("initiator_yaw_deg", "reflector_yaw_deg"):
        angle = normalize_yaw(conditions.get(key))
        if angle is not None:
            conditions[key] = angle
    return conditions


def condition_name(distance, conditions):
    def number(value, *, angle=False):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return "Unknown"
        return f"{value % 360 if angle else value:g}"

    return (f"{number(distance)}m_Ini{number(conditions.get('initiator_yaw_deg'), angle=True)}"
            f"_Ref{number(conditions.get('reflector_yaw_deg'), angle=True)}")


def create_condition_folder(root, distance, conditions):
    """Keep repeated single trials separate, using the shared condition format."""
    root = Path(root)
    name = condition_name(distance, conditions)
    number = 1
    while True:
        folder = root / (name if number == 1 else f"{name}_{number:02d}")
        try:
            folder.mkdir(parents=True)
        except FileExistsError:
            number += 1
        else:
            return folder


def migrate_single_trials(root):
    """Rename old single captures without changing their readings or metadata."""
    root = Path(root)
    if not root.is_dir() or linked(root):
        return []
    root = root.resolve()
    renamed = []
    for folder in sorted(root.iterdir()):
        if (not folder.is_dir() or linked(folder) or folder.name == "guided"
                or folder.name.startswith((".", "_"))):
            continue
        try:
            summary = json.loads((folder / "capture/summary.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(summary, dict) or summary.get("calibration"):
            continue
        # A modern capture still recording must keep its current path.
        if "reports_saved" in summary and not summary["reports_saved"] and summary.get("stop_reason") is None:
            continue
        conditions = summary.get("conditions")
        conditions = conditions if isinstance(conditions, dict) else {}
        name = condition_name(summary.get("actual_distance_m"), conditions)
        if re.fullmatch(re.escape(name) + r"(?:_\d{2,})?", folder.name):
            continue
        target = create_condition_folder(root, summary.get("actual_distance_m"), conditions)
        target.rmdir()  # Windows rename requires an unused destination.
        if folder.resolve().parent != root or target.resolve().parent != root:
            raise ValueError("Single-trial folders must remain inside their live folder.")
        try:
            folder.rename(target)
        except OSError as error:
            print(f"Kept {folder.name}; could not rename this capture: {error}", flush=True)
            continue
        renamed.append((folder, target))
    return renamed


def linked(path):
    try:
        attributes = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(getattr(attributes, "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def trial_folders(root, *, include_temporary=False):
    """Find flat single trials and grouped guided trials, skipping container folders."""
    for directory, folders, files in os.walk(root, followlinks=False):
        directory = Path(directory)
        folders[:] = sorted(name for name in folders if not name.startswith(".replaced-")
                            and not linked(directory / name)
                            and (include_temporary or not name.startswith((".", "_"))))
        if (directory / "capture/summary.json").is_file() or "summary.json" in files:
            yield directory
            folders.clear()


def batch_active(folder):
    folder = Path(folder)
    markers = [folder / name for name in (".progress.active.json", "progress.active.json", "capture/progress.active.json")]
    markers.extend(folder.glob("*/capture/progress.active.json"))
    return any(_marker_active(marker) for marker in markers)


def _marker_active(marker):
    if not marker.exists():
        return False
    try:
        process = json.loads(marker.read_text(encoding="utf-8"))["pid"]
        if type(process) is not int or process <= 0:
            return True
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, process)
            if not handle:
                return ctypes.get_last_error() != 87
            try:
                status = wintypes.DWORD()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value == 259
            finally:
                kernel.CloseHandle(handle)
        os.kill(process, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return True
    return True


def recycle_folder(path):
    """Send one validated local folder to the Windows Recycle Bin."""
    path = Path(path).resolve()
    if os.name != "nt":
        raise OSError("Folder recycling requires Windows.")
    command = """$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName Microsoft.VisualBasic
[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
    $env:GENIE_TRIAL_RECYCLE_PATH,
    [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
    [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "GENIE_TRIAL_RECYCLE_PATH": str(path)}, capture_output=True,
        text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode or path.exists():
        raise OSError(result.stderr.strip() or "Windows did not recycle the folder.")
