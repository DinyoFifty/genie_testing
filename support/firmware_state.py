"""Saved antenna modes and offset baselines shared by all capture workflows."""

import hashlib
import json
from pathlib import Path
import re

from flash import FIRMWARE, PROJECTS


def _definition(source, name, path):
    matches = re.findall(rf"^[ \t]*#define[ \t]+{re.escape(name)}[ \t]+([^\r\n]+)",
                         source, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"Expected one definition of {name} in {path}.")
    return matches[0].split("//", 1)[0].strip()


def snapshot_firmware(firmware=FIRMWARE):
    """Snapshot saved project settings; this does not read back flashed firmware."""
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
                             "Run calibration.py or normal.py, then flash.py before collecting measurements.")
        antenna = int(_definition(source, "GENIE_TEST_ANTENNA", header), 0) if count == 1 else None
        if count == 1 and antenna not in (1, 2):
            raise ValueError("Select physical ANT1 or ANT2 for isolated calibration.")
        kind = _definition(source, "SL_RAIL_UTIL_CS_ANTENNA_CONFIG_TYPE", header)
        kinds = {f"SL_RAIL_UTIL_CS_ANTENNA_OFFSET_CONFIG_TYPE_{name.upper()}": name
                 for name in ("wireless", "wired")}
        if kind not in kinds:
            raise ValueError(f"Unknown antenna offset configuration in {header}.")
        if kinds[kind] != "wireless":
            raise ValueError("Distance capture requires wireless antenna offsets on both boards.")
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
        raise ValueError("Select the same physical antenna on both boards for isolated calibration.")
    snapshot = {"mode": mode, "projects": projects}
    snapshot["fingerprint"] = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return snapshot


