"""Build and flash the project matching each connected board's role."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
FIRMWARE = ROOT / "firmware"
BOARD_ROLES = FIRMWARE / "board_roles.json"
PROJECTS = {
    "initiator": "bt_cs_ncp",
    "reflector": "bt_cs_soc_reflector_studio",
}
DEVICE = "EFR32MG24B210F1536IM48"

BUILD_TOOLS = {
    "ARM_GCC_DIR": ("arm-none-eabi-gcc", ["conan/p/gcc-*/p/bin/arm-none-eabi-gcc.exe"]),
    "POST_BUILD_EXE": ("commander", [
        "archive/Simplicity Commander/commander.exe",
        "conan/p/commander*/p/commander.exe",
    ]),
    "NINJA_EXE_PATH": ("ninja", ["conan/p/ninja*/p/ninja.exe"]),
}


def find_tool(name, patterns):
    installed = shutil.which(name)
    if installed:
        return Path(installed)
    installs = Path.home() / ".silabs" / "slt" / "installs"
    candidates = []
    for pattern in patterns:
        candidates.extend(installs.glob(pattern))
    candidates = [path for path in candidates if path.is_file()]
    if not candidates:
        raise RuntimeError(f"Cannot find {name}. Install it through Simplicity Studio.")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def commander_json(commander, arguments):
    result = subprocess.run(
        [str(commander), *arguments, "--json"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        details = (result.stderr + result.stdout).strip()
        raise RuntimeError(f"Commander returned an unreadable response.\n{details}") from error
    if result.returncode != 0 or response.get("success") is not True:
        details = (result.stderr + result.stdout).strip()
        raise RuntimeError(f"Commander failed.\n{details}")
    return response.get("result", {})


def connected_boards(commander):
    result = commander_json(commander, ["adapter", "list", "--noconnect"])
    boards = result.get("devices", [])
    if not isinstance(boards, list):
        raise RuntimeError("Commander returned an invalid board list.")
    for board in boards:
        serial = str(board.get("serial_number", ""))
        if not serial.isdecimal() or int(serial) <= 0:
            raise RuntimeError("A connected adapter has no usable J-Link serial number.")
    return boards


def load_roles():
    if not BOARD_ROLES.exists():
        return {}
    roles = json.loads(BOARD_ROLES.read_text(encoding="utf-8"))
    if not isinstance(roles, dict) or any(role not in PROJECTS for role in roles.values()):
        raise RuntimeError(f"Invalid board assignments in {BOARD_ROLES}.")
    return roles


def board_role(board, roles):
    serial = str(board["serial_number"])
    nickname = str(board.get("adapter_nickname", "")).strip().lower()
    if nickname in PROJECTS:
        return nickname
    if serial in roles:
        return roles[serial]
    print(f"\nNew board detected, J-Link {serial}.")
    print("1. Initiator\n2. Reflector")
    while True:
        answer = input("Choose 1 or 2, or enter skip to leave this board alone: ").strip().lower()
        if answer == "skip":
            return None
        role = {
            "1": "initiator", "initiator": "initiator",
            "2": "reflector", "reflector": "reflector",
        }.get(answer)
        if role:
            roles[serial] = role
            temporary = BOARD_ROLES.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(roles, indent=2) + "\n", encoding="utf-8")
            temporary.replace(BOARD_ROLES)
            return role
        print("Enter 1, 2, or skip.")


def build_project(cmake, role):
    project_name = PROJECTS[role]
    project = (FIRMWARE / project_name).resolve()
    source = project / "app.c"
    cmake_directory = project / "cmake_gcc"
    if not source.is_file() or not (cmake_directory / "CMakePresets.json").is_file():
        raise RuntimeError(f"Firmware project is incomplete at {project}.")
    environment = os.environ.copy()
    for variable, (name, patterns) in BUILD_TOOLS.items():
        if not environment.get(variable):
            tool = find_tool(name, patterns)
            value = tool.parent.parent if variable == "ARM_GCC_DIR" else tool
            environment[variable] = value.as_posix()
    print(f"\nBuilding {project_name} from your saved source files...", flush=True)
    subprocess.run(
        [str(cmake), "--preset", "project"],
        cwd=cmake_directory, env=environment, check=True,
    )
    subprocess.run(
        [str(cmake), "--build", "--preset", "default_config", "--parallel", "3"],
        cwd=cmake_directory,
        env=environment,
        check=True,
    )
    image = cmake_directory / "build" / "base" / f"{project_name}.hex"
    if not image.is_file():
        raise RuntimeError(f"The build did not produce {image}.")
    return image


def flash_board(commander, board, role, image):
    serial = str(board["serial_number"])
    if serial not in {str(item["serial_number"]) for item in connected_boards(commander)}:
        raise RuntimeError(f"The {role} board, J-Link {serial}, disconnected before flashing.")
    print(f"Flashing {role}, J-Link {serial}...", flush=True)
    commander_json(commander, [
        "flash", str(image), "--device", DEVICE, "--serialno", serial,
    ])
    print(f"Success. {role.capitalize()} firmware programmed and verified.\n", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="Detect boards and build their projects without writing to a board.",
    )
    args = parser.parse_args()
    commander = find_tool("commander", [
        "archive/Simplicity Commander/commander.exe",
        "conan/p/commander*/p/commander.exe",
    ])
    cmake = find_tool("cmake", ["conan/p/cmake*/p/bin/cmake.exe"])
    roles = load_roles()
    print("Save your firmware source and configuration edits before running this launcher.", flush=True)
    boards = connected_boards(commander)
    if not boards:
        print("Connect a board by USB. Waiting... Press Ctrl+C to cancel.", flush=True)
        while not boards:
            time.sleep(2)
            boards = connected_boards(commander)
    completed = 0
    for board in boards:
        role = board_role(board, roles)
        if role is None:
            continue
        print(f"Detected {role}, J-Link {board['serial_number']} -> {PROJECTS[role]}", flush=True)
        image = build_project(cmake, role)
        if args.check:
            print(f"Build check passed for {role}. Image ready at {image}", flush=True)
        else:
            flash_board(commander, board, role, image)
        completed += 1
    if not completed:
        print("No boards selected.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, EOFError, subprocess.SubprocessError) as error:
        print(f"\nFlash launcher stopped. {error}", file=sys.stderr)
        sys.exit(1)
