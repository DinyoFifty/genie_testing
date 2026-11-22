"""Regression checks for setup paths and firmware build tool discovery."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import flash
from support import setup_genie
from support.package_project import package_project


class SetupTests(unittest.TestCase):
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
                     "ARM_GCC_DIR": root / "tools/bin/arm-none-eabi-gcc.exe"}
            setup_genie.configure_paths(root, tools)
            first = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            setup_genie.configure_paths(root, tools)
            self.assertEqual(first, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})
            self.assertTrue((root / "data/live").is_dir())
            for project_name in flash.PROJECTS.values():
                project = root / "firmware" / project_name
                self.assertEqual((project / "config/offset.h").read_bytes(), b"user calibration 60 67\n")
                self.assertIn(tools["sdk"].as_posix(), (project / ".clangd").read_text())

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
