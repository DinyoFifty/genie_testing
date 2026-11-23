"""Print saved setup before recording without claiming device readback."""

import json
from pathlib import Path

from .master_results import PROFILES, ROOT, number, radio_profile, test_context


def setup_lines(snapshot, actual_distance, conditions, *, profile="baseline", board_pair=None, roles=None):
    conditions = conditions or {}

    def value(item, unit=""):
        numeric = number(item)
        return f"{numeric:g}{unit}" if numeric is not None else "unknown"

    mode = "Normal validation (ANT1 + ANT2)" if snapshot.get("mode") == "normal" else "Isolated-antenna calibration" if snapshot.get("mode") == "isolated" else "Unknown"
    lines = ["", "Next capture", f"Mode: {mode}"]
    for role in ("initiator", "reflector"):
        settings = snapshot.get("projects", {}).get(role, {})
        antenna = settings.get("physical_antenna")
        offsets = settings.get("wireless_offsets_cm", [])
        used = [antenna] if antenna in (1, 2) else [1, 2]
        saved = ", ".join(f"ANT{index} {value(offsets[index - 1], ' cm')}" for index in used if len(offsets) >= index)
        lines.append(f"{role.capitalize()} saved offsets: {saved or 'unknown'}")
    lines.append(f"Requested Analyzer profile: {PROFILES.get(profile, {}).get('label', 'Unknown')}")
    if board_pair:
        lines.append("Last capture board IDs: " + ", ".join(board_pair))
    elif roles:
        lines.append("Saved J-Link assignments: " + ", ".join(f"{role} {serial}" for serial, role in roles.items()))
    else:
        lines.append("Board IDs: unknown before connection; saved with the received capture.")
    lines.append(f"Actual distance: {value(actual_distance, ' m')} | Initiator angle: {value(conditions.get('initiator_yaw_deg'), ' deg')} | Reflector angle: {value(conditions.get('reflector_yaw_deg'), ' deg')}")
    lines.append(f"Antenna heights: initiator {value(conditions.get('initiator_height_m'), ' m')}, reflector {value(conditions.get('reflector_height_m'), ' m')}")
    for key, label in (("environment", "Location"), ("obstruction", "Object in the way"),
                       ("polarization", "Board tilt"), ("motion", "Movement"), ("power_source", "Power source")):
        lines.append(f"{label}: {conditions.get(key) or 'unknown'}")
    missing = [label for key, label in (("environment", "location"), ("obstruction", "obstruction"),
               ("polarization", "tilt"), ("motion", "movement"), ("power_source", "power source")) if not conditions.get(key)]
    if missing:
        lines.append("Missing setup details: " + ", ".join(missing) + ". Set these in Results > Next test setup.")
    lines.append("Saved settings are not flashed-firmware or TX-power readback. Flash both boards after changing firmware settings.")
    return lines


def print_setup_summary(snapshot, actual_distance, conditions, *, data=ROOT / "data", profile=None, board_pair=None, use_defaults=True):
    data = Path(data)
    roles = {}
    path = data.parent / "firmware/board_roles.json"
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(saved, dict):
                roles = {serial: role for serial, role in saved.items() if role in ("initiator", "reflector")}
        except (OSError, ValueError):
            pass
    context = {**(test_context(data) if use_defaults else {}), **(conditions or {})}
    lines = setup_lines(snapshot, actual_distance, context,
                        profile=profile or radio_profile(data), board_pair=board_pair, roles=roles)
    print("\n".join(lines), flush=True)
