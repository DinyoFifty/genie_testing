"""Names, discovery, and recycling for local trial folders."""

from datetime import datetime
import json
import math
import os
from pathlib import Path
import stat
import subprocess


def local_timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    value = value.astimezone()
    hour = value.hour % 12 or 12
    period = "AM" if value.hour < 12 else "PM"
    return f"{value:%Y-%m-%d}_{hour}-{value:%M}{period}"


def condition_name(distance, conditions):
    def number(value, *, angle=False):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return "Unknown"
        return f"{value % 360 if angle else value:g}"

    return (f"{number(distance)}m_Ini{number(conditions.get('initiator_yaw_deg'), angle=True)}"
            f"_Ref{number(conditions.get('reflector_yaw_deg'), angle=True)}")


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
    marker = Path(folder) / "progress.active.json"
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
