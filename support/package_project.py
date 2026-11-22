"""Create a teammate ZIP with full firmware sources and fresh local setup."""

import argparse
import json
import os
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build_extension(root):
    extension = root / "support/vscode-workflow"
    manifest = json.loads((extension / "package.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    xml = f'''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="genie-files-manager" Version="{version}" Publisher="local-genie" />
    <DisplayName>Genie Files</DisplayName>
    <Description xml:space="preserve">Quick access to the Genie workflow and antenna offset headers.</Description>
    <Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="^1.74.0" /></Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code" Version="[1.74.0,)" /></Installation>
  <Dependencies />
  <Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" /></Assets>
</PackageManifest>'''
    content_types = '''<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json" />
  <Default Extension="js" ContentType="application/javascript" />
  <Default Extension="vsixmanifest" ContentType="text/xml" />
</Types>'''
    with zipfile.ZipFile(extension / "genie-files-manager.vsix", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("extension.vsixmanifest", xml)
        archive.writestr("[Content_Types].xml", content_types)
        for name in ("package.json", "extension.js"):
            archive.write(extension / name, "extension/" + name)


def package_project(root, destination):
    """Walk into firmware junctions and store each file's actual contents."""
    build_extension(root)
    temporary = destination.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for directory, folders, files in os.walk(root, followlinks=True):
                relative = Path(directory).relative_to(root)
                excluded = {".venv", ".codex", ".git", "__pycache__", ".uceditor"}
                if relative == Path("."):
                    excluded.update({"data", "test-output"})
                if relative.parts[:1] == ("firmware",):
                    excluded.add(".vscode")
                    if len(relative.parts) == 3 and relative.parts[2] == "cmake_gcc":
                        excluded.add("build")
                folders[:] = sorted(name for name in folders if name not in excluded)
                for name in sorted(files):
                    path = Path(directory) / name
                    if name.endswith((".pyc", ".pyo", ".crswap")) or name in {
                        "board_roles.json", ".projectlinkstore", ".silabsExtContext", "vscode.conf",
                    } or path.resolve() in {destination.resolve(), temporary.resolve()}:
                        continue
                    target = "genie_testing/" + path.relative_to(root).as_posix()
                    content = path.read_bytes()
                    # Setup supplies the recipient's SDK and compiler installation paths.
                    if relative.parts[:1] == ("firmware",) and path.suffix == ".cmake":
                        text = content.decode("utf-8")
                        text = re.sub(r'(?m)^set\((SDK_PATH|PKG_PATH) "[^"]*"\)',
                                      r'set(\1 "__RUN_SETUP__")', text)
                        content = text.encode("utf-8")
                    elif name == ".clangd":
                        text = content.decode("utf-8")
                        content = re.sub(r"(?m)^  Add:.*$", '  Add: ["-I__RUN_SETUP__/cmsis/Core/Include"]', text).encode("utf-8")
                    elif (relative == Path(".vscode") and name in {"settings.json", "c_cpp_properties.json"}) or name.endswith(".code-workspace"):
                        text = content.decode("utf-8")
                        text = re.sub(r'C:/Users/[^"\n]+/bin/arm-none-eabi-gcc.exe',
                                      "arm-none-eabi-gcc", text)
                        content = text.encode("utf-8")
                    archive.writestr(target, content)
            archive.writestr("genie_testing/data/live/", b"")
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT.parent / "genie_testing.zip")
    args = parser.parse_args()
    archive = package_project(ROOT, args.output.resolve())
    print(f"Created {archive} ({archive.stat().st_size / 1024**2:.1f} MB).")
