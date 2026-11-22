"""Prepare the Python environment, local build paths, and VS Code sidebar."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flash import BUILD_TOOLS, PROJECTS, find_tool

SDK_VERSION = "2026.6.2"


def installed_tools():
    installs = Path.home() / ".silabs" / "slt" / "installs"
    manifests = installs.glob("conan/p/simpl*/p/simplicity_sdk.slcs")
    sdks = [path.parent for path in manifests if re.search(
        rf"^sdk_version:\s*{re.escape(SDK_VERSION)}\s*$",
        path.read_text(encoding="utf-8"), re.MULTILINE,
    )]
    if not sdks:
        raise RuntimeError(f"Install Simplicity SDK {SDK_VERSION} through the Silicon Labs tools.")
    tools = {"sdk": max(sdks, key=lambda path: path.stat().st_mtime), "installs": installs}
    for variable, (name, patterns) in BUILD_TOOLS.items():
        if os.environ.get(variable):
            value = Path(os.environ[variable])
            tools[variable] = value / "bin" / "arm-none-eabi-gcc.exe" if variable == "ARM_GCC_DIR" else value
        else:
            tools[variable] = find_tool(name, patterns)
        if not tools[variable].is_file():
            raise RuntimeError(f"Cannot find {name} at {tools[variable]}.")
    tools["cmake"] = find_tool("cmake", ["conan/p/cmake*/p/bin/cmake.exe"])
    tools["analyzer"] = installs / "archive" / "cs_analyzer-v1.2.2" / "CS_Analyzer.exe"
    if not tools["analyzer"].is_file():
        raise RuntimeError("Install Channel Sounding Analyzer 1.2.2 through the Silicon Labs tools.")
    code = shutil.which("code")
    if not code:
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Microsoft VS Code/bin/code.cmd",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft VS Code/bin/code.cmd",
        ]
        code = next((str(path) for path in candidates if path.is_file()), None)
    if not code:
        raise RuntimeError("Install Visual Studio Code, then run setup again.")
    tools["code"] = Path(code)
    return tools


def configure_paths(root, tools):
    """Refresh generated paths while preserving the firmware configuration."""
    for project_name in PROJECTS.values():
        project = root / "firmware" / project_name
        cmake_file = project / "cmake_gcc" / f"{project_name}.cmake"
        content = cmake_file.read_text(encoding="utf-8")
        for variable, path in (("SDK_PATH", tools["sdk"]), ("PKG_PATH", tools["installs"])):
            content, count = re.subn(
                rf'^set\({variable} "[^"]*"\)$',
                lambda match: f'set({variable} "{path.as_posix()}")',
                content, count=1, flags=re.MULTILINE,
            )
            if count != 1:
                raise RuntimeError(f"Cannot locate {variable} in {cmake_file}.")
        cmake_file.write_text(content, encoding="utf-8")
        clangd = project / ".clangd"
        content = clangd.read_text(encoding="utf-8")
        content = re.sub(
            r"(?m)^  Add:.*$",
            lambda match: "  Add: " + json.dumps(["-I" + (tools["sdk"] / "cmsis/Core/Include").as_posix()]),
            content,
        )
        clangd.write_text(content, encoding="utf-8")

    compiler = tools["ARM_GCC_DIR"].as_posix()
    workspace_path = root / "genie_testing.code-workspace"
    workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
    workspace["settings"].update({
        "clangd.arguments": ["--query-driver=" + compiler],
        "C_Cpp.default.compilerPath": compiler,
        "python.defaultInterpreterPath": "${workspaceFolder}/.venv/Scripts/python.exe",
    })
    workspace_path.write_text(json.dumps(workspace, indent=2) + "\n", encoding="utf-8")
    settings_path = root / ".vscode" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["clangd.arguments"] = ["--query-driver=" + compiler]
    settings["python.defaultInterpreterPath"] = "${workspaceFolder}/.venv/Scripts/python.exe"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    properties_path = root / ".vscode" / "c_cpp_properties.json"
    properties = json.loads(properties_path.read_text(encoding="utf-8"))
    for configuration in properties["configurations"]:
        configuration["compilerPath"] = compiler
    properties_path.write_text(json.dumps(properties, indent=2) + "\n", encoding="utf-8")
    (root / "data" / "live").mkdir(parents=True, exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-open", action="store_true", help="Set up without opening VS Code.")
    parser.add_argument("--check", action="store_true", help="Check required tools without changing files.")
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info[:2] != (3, 11):
        raise RuntimeError("Run setup on Windows with Python 3.11.")
    tools = installed_tools()
    for name, path in tools.items():
        print(f"Found {name} at {path}", flush=True)
    if args.check:
        return 0
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    subprocess.run([
        str(python), "-m", "pip", "install", "-r", str(ROOT / "support/requirements.txt"),
    ], check=True)
    configure_paths(ROOT, tools)
    code = str(tools["code"])
    installed = subprocess.run(
        [code, "--list-extensions", "--show-versions"],
        check=True, capture_output=True, text=True,
    ).stdout.lower().splitlines()
    if not any(item.startswith("ms-python.python@") for item in installed):
        subprocess.run([code, "--install-extension", "ms-python.python"], check=True)
    extension = ROOT / "support/vscode-workflow"
    manifest = json.loads((extension / "package.json").read_text(encoding="utf-8"))
    identity = f'{manifest["publisher"]}.{manifest["name"]}@{manifest["version"]}'.lower()
    if identity not in installed:
        subprocess.run([code, "--install-extension", str(extension / "genie-files-manager.vsix"), "--force"], check=True)
    print("\nSetup complete. Use Genie Files in the VS Code Explorer.", flush=True)
    if not args.no_open:
        subprocess.run([code, str(ROOT / "genie_testing.code-workspace")], check=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"\nSetup stopped. {error}", file=sys.stderr)
        sys.exit(1)
