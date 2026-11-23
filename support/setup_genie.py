"""Prepare the Python environment, local build paths, and VS Code sidebar."""

import argparse
from datetime import datetime, timezone
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
from support.local_config import initialize_local_files
from support.package_project import build_extension
from support.trial_storage import migrate_single_trials

SDK_VERSION = "2026.6.2"
SILABS_PACKAGES = {
    "sdk": (f"simplicity-sdk/{SDK_VERSION}", "simplicity_sdk.slcs"),
    "ARM_GCC_DIR": ("gcc-arm-none-eabi/14.2.rel1", "bin/arm-none-eabi-gcc.exe"),
    "cmake": ("cmake", "bin/cmake.exe"),
    "NINJA_EXE_PATH": ("ninja", "ninja.exe"),
    "POST_BUILD_EXE": ("commander", "commander.exe"),
    "analyzer": ("cs_analyzer/1.2.2", "CS_Analyzer.exe"),
}
EXTENSIONS = ("ms-python.python", "ms-python.vscode-pylance", "ms-python.debugpy", "ms-vscode.cpptools")


def find_slt():
    candidates = [shutil.which("slt")]
    for variable in ("LOCALAPPDATA", "ProgramFiles"):
        base = Path(os.environ.get(variable, "C:/Program Files"))
        candidates.append(base / "Programs/SimplicityInstaller/resources/slt.exe"
                          if variable == "LOCALAPPDATA" else base / "SimplicityInstaller/resources/slt.exe")
    return next((Path(item) for item in candidates if item and Path(item).is_file()), None)


def prepare_silabs_tools(check_only=False):
    """Use the installed Silicon Labs package manager without changing its global context."""
    slt = find_slt()
    if not slt:
        return {}

    def locate():
        locations = {}
        for name, (package, executable) in SILABS_PACKAGES.items():
            result = subprocess.run(
                [str(slt), "--non-interactive", "--json", "where", package, "--ignore-slconf"],
                check=True, capture_output=True, text=True, timeout=60,
            )
            roots = json.loads(result.stdout)
            if isinstance(roots, list):
                match = next((Path(root) for root in roots if isinstance(root, str)
                              and (Path(root) / executable).is_file()), None)
                if match:
                    locations[name] = match
        return locations

    locations = locate()
    missing = [package for name, (package, _) in SILABS_PACKAGES.items() if name not in locations]
    if missing and not check_only:
        print("Installing missing Silicon Labs packages: " + ", ".join(missing), flush=True)
        print("Large SDK downloads can take several minutes.", flush=True)
        try:
            subprocess.run([
                str(slt), "--non-interactive", "install", *missing,
                "--no-lock", "--check-updates=false",
            ], check=True)
        except subprocess.CalledProcessError as error:
            raise RuntimeError("Silicon Labs installation failed. Open Simplicity Installer, sign in "
                               "if requested, and install: " + ", ".join(missing) + ". Then rerun setup.") from error
        locations = locate()
        missing = [package for name, (package, _) in SILABS_PACKAGES.items() if name not in locations]
    if missing:
        raise RuntimeError("Missing Silicon Labs packages: " + ", ".join(missing) + ". Run setup.cmd to install them.")
    return locations


def installed_tools(locations=None):
    locations = locations or {}
    installs = Path.home() / ".silabs" / "slt" / "installs"
    manifests = installs.glob("conan/p/simpl*/p/simplicity_sdk.slcs")
    sdks = [path.parent for path in manifests if re.search(
        rf"^sdk_version:\s*{re.escape(SDK_VERSION)}\s*$",
        path.read_text(encoding="utf-8"), re.MULTILINE,
    )]
    if not sdks:
        raise RuntimeError(f"Install Simplicity SDK {SDK_VERSION} through the Silicon Labs tools.")
    tools = {"sdk": locations.get("sdk", max(sdks, key=lambda path: path.stat().st_mtime)), "installs": installs}
    for variable, (name, patterns) in BUILD_TOOLS.items():
        if os.environ.get(variable):
            value = Path(os.environ[variable])
            tools[variable] = value / "bin" / "arm-none-eabi-gcc.exe" if variable == "ARM_GCC_DIR" else value
        elif variable in locations:
            tools[variable] = locations[variable] / SILABS_PACKAGES[variable][1]
        else:
            tools[variable] = find_tool(name, patterns)
        if not tools[variable].is_file():
            raise RuntimeError(f"Cannot find {name} at {tools[variable]}.")
    tools["cmake"] = (locations["cmake"] / "bin/cmake.exe" if "cmake" in locations
                      else find_tool("cmake", ["conan/p/cmake*/p/bin/cmake.exe"]))
    tools["analyzer"] = (locations["analyzer"] / "CS_Analyzer.exe" if "analyzer" in locations
                         else installs / "archive" / "cs_analyzer-v1.2.2" / "CS_Analyzer.exe")
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


def verify_environment(python):
    subprocess.run([str(python), "-B", "-m", "pip", "check"], check=True)
    result = subprocess.run([
        str(python), "-B", "-c",
        "import sys, json, tkinter, matplotlib, openpyxl; "
        "assert sys.version_info[:2] == (3, 11); "
        "print(json.dumps(dict(python=sys.version.split()[0], "
        "matplotlib=matplotlib.__version__, openpyxl=openpyxl.__version__)))",
    ], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def prepare_environment():
    environment = ROOT / ".venv"
    python = environment / "Scripts/python.exe"
    usable = False
    if python.is_file():
        try:
            usable = subprocess.run([
                str(python), "-B", "-c", "import sys; sys.exit(sys.version_info[:2] != (3, 11))",
            ], capture_output=True).returncode == 0
        except OSError:
            pass
    if not usable:
        if environment.resolve().parent != ROOT.resolve():
            raise RuntimeError("The .venv folder points outside this project. Use a local project environment.")
        print("Creating the project's Python 3.11 environment...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", "--clear", str(environment)], check=True)
    subprocess.run([
        str(python), "-m", "pip", "install", "-r", str(ROOT / "support/requirements.txt"),
    ], check=True)
    return python


def install_extensions(code):
    installed = subprocess.run(
        [str(code), "--list-extensions", "--show-versions"],
        check=True, capture_output=True, text=True,
    ).stdout.lower().splitlines()
    for extension in EXTENSIONS:
        if not any(item.startswith(extension + "@") for item in installed):
            subprocess.run([str(code), "--install-extension", extension], check=True)
    extension = ROOT / "support/vscode-workflow"
    manifest = json.loads((extension / "package.json").read_text(encoding="utf-8"))
    identity = f'{manifest["publisher"]}.{manifest["name"]}@{manifest["version"]}'.lower()
    if identity not in installed:
        build_extension(ROOT)
        subprocess.run([str(code), "--install-extension", str(extension / "genie-files-manager.vsix"), "--force"], check=True)


def configure_paths(root, tools):
    """Refresh generated paths while preserving the firmware configuration."""
    initialize_local_files(root)
    for project_name in PROJECTS.values():
        project = root / "firmware" / project_name
        cmake_file = project / "cmake_gcc/genie_local_paths.cmake"
        cmake_file.write_text(
            "# Local SDK paths, generated by setup.\n"
            f'set(SDK_PATH "{tools["sdk"].as_posix()}")\n'
            f'set(PKG_PATH "{tools["installs"].as_posix()}")\n', encoding="utf-8",
        )
        clangd = project / ".clangd"
        content = clangd.read_text(encoding="utf-8")
        content = re.sub(
            r"(?m)^  Add:.*$",
            lambda match: "  Add: " + json.dumps(["-I" + (tools["sdk"] / "cmsis/Core/Include").as_posix()]),
            content,
        )
        clangd.write_text(content, encoding="utf-8")
        toolchain = project / "cmake_gcc/autogen_toolchain.cmake"
        toolchain.write_text(
            "# Local tool paths, generated by setup.\n"
            f'set(DEFAULT_TOOLCHAIN_DIR "{tools["ARM_GCC_DIR"].parent.as_posix()}/")\n'
            f'set(DEFAULT_POST_BUILD_EXE "{tools["POST_BUILD_EXE"].as_posix()}")\n'
            f'set(DEFAULT_NINJA_RUNTIME_PATH "{tools["NINJA_EXE_PATH"].as_posix()}")\n',
            encoding="utf-8",
        )
        bluetooth = project / "config/btconf/slc_args.json"
        if bluetooth.is_file():
            configuration = json.loads(bluetooth.read_text(encoding="utf-8"))
            configuration["sdkRoot"] = tools["sdk"].as_posix() + "/"
            configuration["apackRoot"] = (tools["sdk"] / "bluetooth_bgbuild").as_posix()
            bluetooth.write_text(json.dumps(configuration, indent=2) + "\n", encoding="utf-8")

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
    shared_settings = json.loads((root / "support/local_defaults/.vscode/settings.json").read_text(encoding="utf-8"))
    settings.setdefault("files.exclude", {}).update(shared_settings.get("files.exclude", {}))
    settings["clangd.arguments"] = ["--query-driver=" + compiler]
    settings["python.defaultInterpreterPath"] = "${workspaceFolder}/.venv/Scripts/python.exe"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    properties_path = root / ".vscode" / "c_cpp_properties.json"
    properties = json.loads(properties_path.read_text(encoding="utf-8"))
    for configuration in properties["configurations"]:
        configuration["compilerPath"] = compiler
    properties_path.write_text(json.dumps(properties, indent=2) + "\n", encoding="utf-8")
    (root / "data" / "live").mkdir(parents=True, exist_ok=True)
    migrate_single_trials(root / "data/live")
    from support.genie_calibration import migrate_batches
    migrate_batches(root / "data/live", root / "data/calibration")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-open", action="store_true", help="Set up without opening VS Code.")
    parser.add_argument("--check", action="store_true", help="Check required tools without changing files.")
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info[:2] != (3, 11):
        raise RuntimeError("Run setup on Windows with Python 3.11.")
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "running", "stage": "Silicon Labs tools"}
    try:
        tools = installed_tools(prepare_silabs_tools(check_only=args.check))
        report["tools"] = {name: str(path) for name, path in tools.items()}
        for name, path in tools.items():
            print(f"Found {name} at {path}", flush=True)
        report["stage"] = "Python environment"
        python = ROOT / ".venv/Scripts/python.exe" if args.check else prepare_environment()
        report["packages"] = verify_environment(python)
        if not args.check:
            report["stage"] = "Project paths"
            configure_paths(ROOT, tools)
            report["stage"] = "VS Code extensions"
            install_extensions(tools["code"])
            report["stage"] = "Master results workbook"
            subprocess.run([str(python), "-B", str(ROOT / "results.py"), "--refresh"], check=True)
        if not args.check and not args.no_open:
            report["stage"] = "Opening VS Code"
            subprocess.run([str(tools["code"]), str(ROOT / "genie_testing.code-workspace")], check=True)
        report.update(status="passed", stage="complete")
        print("\nSetup checks passed." if args.check else "\nSetup complete. Use Genie Files in the VS Code Explorer.", flush=True)
        return 0
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        if not args.check:
            report_path = ROOT / "data/setup/setup-report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"Setup report: {report_path}", flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"\nSetup stopped. {error}", file=sys.stderr)
        sys.exit(1)
