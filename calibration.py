"""Configure both firmware projects for isolated-antenna calibration."""

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile

from flash import FIRMWARE, PROJECTS


@contextmanager
def configuration_lock(firmware):
    """Serialize mode changes from either launcher."""
    if os.name != "nt":
        import fcntl

        with Path(__file__).open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        return

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    identity = hashlib.sha256(os.path.normcase(str(firmware.resolve())).encode()).hexdigest()
    handle = kernel.CreateMutexW(None, True, "Local\\GenieFirmwareMode_" + identity)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle(handle)
        raise RuntimeError("Another firmware mode switch is running. Try again when it finishes.")
    try:
        yield
    finally:
        kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def replace_defines(source, values, path):
    for name, value in values.items():
        pattern = rf"(?m)^([ \t]*#define[ \t]+{re.escape(name)}[ \t]+)(?:[^\r\n]*\\\r?\n)*[^\r\n]*"
        source, count = re.subn(pattern, lambda match: match[1] + str(value), source)
        if count != 1:
            raise ValueError(f"Expected one definition of {name} in {path}, found {count}.")
    return source


def write_header(path, contents):
    """Replace a complete header without exposing a partially written file."""
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def configure_mode(mode, antenna=1, firmware=FIRMWARE):
    if mode not in ("calibration", "normal") or antenna not in (1, 2):
        raise ValueError("Choose calibration or normal mode and antenna 1 or 2.")
    firmware = Path(firmware)
    isolated = mode == "calibration"
    sync = "CS_SYNC_ANTENNA_1" if isolated else "CS_SYNC_SWITCHING"
    offsets = {"SL_RAIL_UTIL_CS_ANTENNA_COUNT": 1 if isolated else 2}
    if isolated:
        offsets["GENIE_TEST_ANTENNA"] = antenna
    for connection in ("WIRELESS", "WIRED"):
        elements = (f"GENIE_SELECTED_{connection}_OFFSET_CM, 0" if isolated else
                    f"GENIE_ANT1_{connection}_OFFSET_CM, GENIE_ANT2_{connection}_OFFSET_CM")
        offsets[f"SL_RAIL_UTIL_CS_ANTENNA_OFFSET_{connection}_CM"] = "{ " + elements + ", 0, 0 }"

    with configuration_lock(firmware):
        changes = []
        for project in PROJECTS.values():
            config = firmware / project / "config"
            files = {
                "sl_rail_util_cs_antenna_offset_config.h": offsets,
                "sl_rail_util_aox_config.h": {
                    "SL_RAIL_UTIL_AOX_ANTENNA_PIN_COUNT": 0 if isolated else 1,
                    "SL_RAIL_UTIL_AOX_ANTENNA_PIN0_PORT": "SL_GPIO_PORT_A",
                    "SL_RAIL_UTIL_AOX_ANTENNA_PIN0_PIN": 0,
                },
                "cs_reflector_config.h": {"CS_REFLECTOR_CS_SYNC_ANTENNA": sync},
            }
            if project == PROJECTS["initiator"]:
                files["cs_initiator_config.h"] = {
                    "CS_INITIATOR_DEFAULT_CS_TONE_ANTENNA_CONFIG_IDX_REQ": (
                        "CS_ANTENNA_CONFIG_INDEX_SINGLE_ONLY" if isolated else "CS_ANTENNA_CONFIG_INDEX_DUAL_ONLY"),
                    "CS_INITIATOR_DEFAULT_CS_SYNC_ANTENNA_REQ": sync,
                    "CS_INITIATOR_DEFAULT_PREFERRED_PEER_ANTENNA": 1 if isolated else 3,
                }
            for filename, values in files.items():
                path = config / filename
                original = path.read_bytes()
                updated = replace_defines(original.decode("utf-8"), values, path).encode("utf-8")
                changes.append((path, original, updated))

        written = []
        try:
            for path, original, updated in changes:
                if path.read_bytes() != original:
                    raise RuntimeError(f"{path} changed during the switch. Save your edits and run again.")
                if updated != original:
                    write_header(path, updated)
                    written.append((path, original, updated))
        except BaseException:
            for path, original, updated in reversed(written):
                if path.read_bytes() == updated:
                    write_header(path, original)
            raise

    description = f"Calibration mode, physical ANT{antenna}, 1 available antenna, 0 AoX control pins" if isolated else (
        "Normal mode, ANT1 and ANT2, 2 available antennas, 1 AoX control pin")
    print(description + ".", flush=True)
    print("Both firmware projects configured. Next run flash.py to rebuild and program both boards.", flush=True)
    return [path for path, _, _ in written]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--antenna", type=int, choices=(1, 2), help="Physical antenna to isolate on both boards.")
    args = parser.parse_args()
    antenna = args.antenna
    while antenna is None:
        answer = input("Calibration antenna [1 = ANT1, 2 = ANT2, Enter = ANT1]? ").strip()
        if answer in ("", "1", "2"):
            antenna = int(answer or "1")
        else:
            print("Enter 1 or 2.")
    configure_mode("calibration", antenna)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, EOFError) as error:
        print(f"Mode switch stopped. {error}", file=sys.stderr)
        sys.exit(1)
