"""Check that experiments stay private in a fresh checkout and teammate ZIP."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from calibration import configure_mode, replace_defines
from support.genie_calibration import snapshot_firmware
from support.local_config import initialize_local_files, local_paths
from support.package_project import package_project
from support.setup_genie import configure_paths


ROOT = Path(__file__).resolve().parents[2]


class LocalConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="Genie local ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        shutil.copytree(ROOT / "support/local_defaults", self.root / "support/local_defaults")
        shutil.copyfile(ROOT / ".gitignore", self.root / ".gitignore")
        for project in ("bt_cs_ncp", "bt_cs_soc_reflector_studio"):
            relative = Path("firmware") / project / "cmake_gcc" / f"{project}.cmake"
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)

    def tools(self):
        return {"sdk": self.root / "SDK", "installs": self.root / "tools",
                "ARM_GCC_DIR": self.root / "tools/bin/arm-none-eabi-gcc.exe",
                "POST_BUILD_EXE": self.root / "tools/commander.exe",
                "NINJA_EXE_PATH": self.root / "tools/ninja.exe"}

    def test_defaults_are_normal_zero_offsets_and_existing_files_are_preserved(self):
        defaults = self.root / "support/local_defaults"
        baseline = snapshot_firmware(defaults / "firmware")
        self.assertEqual(baseline["mode"], "normal")
        for project in baseline["projects"].values():
            self.assertEqual(project["wireless_offsets_cm"], [0, 0, 0, 0])
        created = initialize_local_files(self.root)
        self.assertEqual(len(created), len(local_paths(self.root)))
        before = {path: path.read_bytes() for path in created}
        header = self.root / "firmware/bt_cs_ncp/config/sl_rail_util_cs_antenna_offset_config.h"
        header.write_bytes(b"existing personal calibration, never replace\n")
        before[header] = header.read_bytes()
        self.assertEqual(initialize_local_files(self.root), [])
        self.assertEqual(before, {path: path.read_bytes() for path in created})

    @unittest.skipUnless(shutil.which("git"), "Git is needed to verify a clean working tree")
    def test_mode_offsets_data_and_setup_paths_do_not_change_git(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.root), *args],
                                  check=True, capture_output=True, text=True).stdout

        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=Genie test", "-c", "user.email=genie@example.invalid",
            "commit", "-qm", "shared defaults")
        configure_paths(self.root, self.tools())
        firmware = self.root / "firmware"
        configure_mode("calibration", 2, firmware)
        for index, project in enumerate(("bt_cs_ncp", "bt_cs_soc_reflector_studio")):
            header = firmware / project / "config/sl_rail_util_cs_antenna_offset_config.h"
            source = replace_defines(header.read_text(), {
                "GENIE_ANT1_WIRELESS_OFFSET_CM": 61 + index,
                "GENIE_ANT2_WIRELESS_OFFSET_CM": -7 - index,
            }, header)
            header.write_text(source, encoding="utf-8")
        before = snapshot_firmware(firmware)
        configure_mode("normal", firmware=firmware)
        configure_mode("calibration", 2, firmware)
        configure_paths(self.root, self.tools())
        self.assertEqual(snapshot_firmware(firmware), before)
        for relative in ("data/live/trial/capture/raw.jsonl", "data/calibration/batch/progress.json",
                         "data/setup/setup.log", "firmware/board_roles.json", ".venv/pyvenv.cfg",
                         "firmware/bt_cs_ncp/autogen/.slc_state/.crc_config.crc"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("personal results")
        self.assertEqual(git("status", "--porcelain"), "")
        self.assertNotEqual(git("ls-files", "support/local_defaults"), "")
        self.assertEqual(git("ls-files", "data", "genie_testing.code-workspace", "firmware/board_roles.json"), "")

    def test_zip_excludes_every_local_setting_and_capture_but_keeps_defaults(self):
        configure_paths(self.root, self.tools())
        extension = self.root / "support/vscode-workflow"
        extension.mkdir(parents=True)
        (extension / "package.json").write_text(json.dumps({"version": "1.2.0"}))
        (extension / "extension.js").write_text("// shared sidebar source")
        for relative in ("data/live/trial/capture/raw.jsonl", "data/calibration/batch/progress.json",
                         "data/setup/setup.log", "firmware/board_roles.json", ".venv/pyvenv.cfg",
                         "firmware/bt_cs_ncp/autogen/.slc_state/.crc_config.crc"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("personal results")
        archive = package_project(self.root, self.root.parent / "share.zip")
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            for relative in local_paths(self.root):
                self.assertNotIn("genie_testing/" + relative.as_posix(), names)
                template = "genie_testing/support/local_defaults/" + relative.as_posix()
                self.assertIn(template, names)
                self.assertEqual(bundle.read(template),
                                 (self.root / "support/local_defaults" / relative).read_bytes())
            self.assertEqual([name for name in names if name.startswith("genie_testing/data/")],
                             ["genie_testing/data/live/"])
            self.assertNotIn("genie_testing/firmware/board_roles.json", names)
            self.assertNotIn("genie_testing/.venv/pyvenv.cfg", names)
            self.assertNotIn("genie_testing/firmware/bt_cs_ncp/cmake_gcc/genie_local_paths.cmake", names)
            self.assertFalse(any("/.slc_state/" in name for name in names))
            for project in ("bt_cs_ncp", "bt_cs_soc_reflector_studio"):
                self.assertIn(f"genie_testing/firmware/{project}/cmake_gcc/{project}.cmake", names)
            self.assertIsNone(bundle.testzip())


if __name__ == "__main__":
    unittest.main()
