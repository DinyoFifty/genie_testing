"""Regression checks for setup paths and firmware build tool discovery."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import flash
from support import setup_genie
from support.package_project import package_project
from support.local_config import initialize_local_files


class SetupTests(unittest.TestCase):
    def test_installs_only_missing_silabs_package(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = {}
            for name, (package, executable) in setup_genie.SILABS_PACKAGES.items():
                root = Path(directory) / name
                target = root / executable
                target.parent.mkdir(parents=True)
                if name != "analyzer":
                    target.touch()
                roots[package] = root

            def run(command, **kwargs):
                if command[2] == "install":
                    self.assertEqual(command[3:-2], ["cs_analyzer/1.2.2"])
                    self.assertEqual(command[-2:], ["--no-lock", "--check-updates=false"])
                    (roots["cs_analyzer/1.2.2"] / "CS_Analyzer.exe").touch()
                    return subprocess.CompletedProcess(command, 0)
                package = command[4]
                return subprocess.CompletedProcess(command, 0, json.dumps([str(roots[package])]))

            with patch.object(setup_genie, "find_slt", return_value=Path("slt.exe")), patch.object(
                setup_genie.subprocess, "run", side_effect=run,
            ) as execute:
                locations = setup_genie.prepare_silabs_tools()
            self.assertEqual(len(locations), len(setup_genie.SILABS_PACKAGES))
            self.assertEqual(sum(call.args[0][2] == "install" for call in execute.call_args_list), 1)

    def test_check_reports_all_missing_packages_without_installing(self):
        with patch.object(setup_genie, "find_slt", return_value=Path("slt.exe")), patch.object(
            setup_genie.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "[]"),
        ) as execute:
            with self.assertRaisesRegex(RuntimeError, "simplicity-sdk/2026.6.2.*cs_analyzer/1.2.2"):
                setup_genie.prepare_silabs_tools(check_only=True)
        self.assertTrue(all("install" not in call.args[0] for call in execute.call_args_list))

    def test_rebuilds_an_unusable_local_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / ".venv/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            source = root / "normal.py"
            source.write_text("original source")
            with patch.object(setup_genie, "ROOT", root), patch.object(
                setup_genie.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
                    subprocess.CompletedProcess([], 0),
                ],
            ) as execute:
                self.assertEqual(setup_genie.prepare_environment(), python)
            self.assertEqual(execute.call_args_list[1].args[0],
                             [sys.executable, "-m", "venv", "--clear", str(root / ".venv")])
            self.assertEqual(source.read_text(), "original source")

    def test_check_does_not_install_or_write_a_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(setup_genie, "ROOT", root), patch.object(sys, "argv", ["setup", "--check"]), patch.object(
                setup_genie, "prepare_silabs_tools", return_value={},
            ) as silabs, patch.object(setup_genie, "installed_tools", return_value={}), patch.object(
                setup_genie, "verify_environment", return_value={"python": "3.11.9"},
            ), patch.object(setup_genie, "prepare_environment") as environment, patch.object(
                setup_genie, "configure_paths",
            ) as configure, patch.object(setup_genie, "install_extensions") as extensions:
                self.assertEqual(setup_genie.main(), 0)
            silabs.assert_called_once_with(check_only=True)
            environment.assert_not_called()
            configure.assert_not_called()
            extensions.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_reuses_installed_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extension = root / "support/vscode-workflow"
            extension.mkdir(parents=True)
            (extension / "package.json").write_text(json.dumps({
                "publisher": "local-genie", "name": "genie-files-manager", "version": "1.2.0",
            }))
            installed = "\n".join(item + "@1.0.0" for item in setup_genie.EXTENSIONS)
            installed += "\nlocal-genie.genie-files-manager@1.2.0"
            with patch.object(setup_genie, "ROOT", root), patch.object(
                setup_genie.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, installed),
            ) as execute:
                setup_genie.install_extensions(Path("code.cmd"))
            self.assertEqual(execute.call_count, 1)

    def test_builds_missing_sidebar_installer_from_shared_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extension = root / "support/vscode-workflow"
            extension.mkdir(parents=True)
            (extension / "package.json").write_text(json.dumps({
                "publisher": "local-genie", "name": "genie-files-manager", "version": "1.2.0",
            }))
            (extension / "extension.js").write_text("// shared sidebar source")
            installed = "\n".join(item + "@1.0.0" for item in setup_genie.EXTENSIONS)
            with patch.object(setup_genie, "ROOT", root), patch.object(
                setup_genie.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, installed),
            ) as execute:
                setup_genie.install_extensions(Path("code.cmd"))
            installer = extension / "genie-files-manager.vsix"
            self.assertTrue(zipfile.is_zipfile(installer))
            self.assertIn(str(installer), execute.call_args.args[0])

    def test_package_keeps_sdk_libraries_and_excludes_local_builds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            extension = root / "support/vscode-workflow"
            extension.mkdir(parents=True)
            (extension / "package.json").write_text('{"version":"1.2.0"}')
            (extension / "extension.js").write_text("// extension fixture")
            for name in (
                "firmware/bt_cs_ncp/simplicity_sdk_2026.6.2/bgapi/build/gcc/libbgapi.a",
                "firmware/bt_cs_ncp/cmake_gcc/build/CMakeCache.txt",
                "firmware/board_roles.json",
                "data/live/trial/capture/raw.jsonl",
                ".venv/pyvenv.cfg",
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            output = Path(directory) / "share.zip"
            package_project(root, output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read(
                    "genie_testing/firmware/bt_cs_ncp/simplicity_sdk_2026.6.2/bgapi/build/gcc/libbgapi.a",
                ), b"fixture")
                self.assertNotIn("genie_testing/firmware/bt_cs_ncp/cmake_gcc/build/CMakeCache.txt", archive.namelist())
                self.assertNotIn("genie_testing/firmware/board_roles.json", archive.namelist())
                self.assertNotIn("genie_testing/.venv/pyvenv.cfg", archive.namelist())
                self.assertEqual([name for name in archive.namelist() if "/data/" in name], ["genie_testing/data/live/"])

    def test_refresh_paths_preserves_offsets_and_is_repeatable(self):
        with tempfile.TemporaryDirectory(prefix="Genie setup ") as directory:
            root = Path(directory)
            for project_name in flash.PROJECTS.values():
                project = root / "firmware" / project_name
                (project / "cmake_gcc").mkdir(parents=True)
                (project / "cmake_gcc" / f"{project_name}.cmake").write_text(
                    'set(SDK_PATH "old")\nset(PKG_PATH "old")\n', encoding="utf-8",
                )
                (project / ".clangd").write_text(
                    'CompileFlags:\n  Add: [-Iold]\n', encoding="utf-8",
                )
                (project / "config").mkdir()
                (project / "config/offset.h").write_bytes(b"user calibration 60 67\n")
            (root / ".vscode").mkdir()
            for name, content in (
                ("genie_testing.code-workspace", {"settings": {}}),
                (".vscode/settings.json", {}),
                (".vscode/c_cpp_properties.json", {"configurations": [{}]}),
            ):
                (root / name).write_text(json.dumps(content), encoding="utf-8")
            tools = {"sdk": root / "SDK with spaces", "installs": root / "tools",
                     "ARM_GCC_DIR": root / "tools/bin/arm-none-eabi-gcc.exe",
                     "POST_BUILD_EXE": root / "tools/commander.exe",
                     "NINJA_EXE_PATH": root / "tools/ninja.exe"}
            setup_genie.configure_paths(root, tools)
            first = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            setup_genie.configure_paths(root, tools)
            self.assertEqual(first, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})
            self.assertTrue((root / "data/live").is_dir())
            for project_name in flash.PROJECTS.values():
                project = root / "firmware" / project_name
                self.assertEqual((project / "config/offset.h").read_bytes(), b"user calibration 60 67\n")
                self.assertIn(tools["sdk"].as_posix(), (project / ".clangd").read_text())
                self.assertEqual((project / "cmake_gcc" / f"{project_name}.cmake").read_text(),
                                 'set(SDK_PATH "old")\nset(PKG_PATH "old")\n')
                self.assertIn(tools["sdk"].as_posix(), (project / "cmake_gcc/genie_local_paths.cmake").read_text())

    def test_build_discovers_tools_and_preserves_explicit_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / flash.PROJECTS["initiator"]
            cmake_directory = project / "cmake_gcc"
            (cmake_directory / "build/base").mkdir(parents=True)
            (project / "app.c").touch()
            (cmake_directory / "CMakePresets.json").write_text("{}")
            image = cmake_directory / "build/base/bt_cs_ncp.hex"
            image.touch()
            compiler = root / "gcc/bin/arm-none-eabi-gcc.exe"
            with patch.object(flash, "FIRMWARE", root), patch.object(
                flash, "find_tool", return_value=compiler,
            ) as discover, patch.object(flash.subprocess, "run") as run, patch.dict(
                os.environ, {"POST_BUILD_EXE": "custom commander.exe", "NINJA_EXE_PATH": "custom ninja.exe"},
            ):
                os.environ.pop("ARM_GCC_DIR", None)
                self.assertEqual(flash.build_project(Path("cmake"), "initiator"), image.resolve())
                self.assertEqual(discover.call_count, 1)
                self.assertEqual(run.call_count, 2)
                for call in run.call_args_list:
                    environment = call.kwargs["env"]
                    self.assertEqual(environment["ARM_GCC_DIR"], compiler.parent.parent.as_posix())
                    self.assertEqual(environment["POST_BUILD_EXE"], "custom commander.exe")
                    self.assertEqual(environment["NINJA_EXE_PATH"], "custom ninja.exe")


if __name__ == "__main__":
    unittest.main()
