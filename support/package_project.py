"""Create a teammate ZIP with full firmware sources and fresh local setup."""

import argparse
import json
import os
from pathlib import Path
import zipfile

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support.local_config import local_paths

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
    private = local_paths(root)
    temporary = destination.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for directory, folders, files in os.walk(root, followlinks=True):
                relative = Path(directory).relative_to(root)
                excluded = {".venv", ".codex", ".git", "__pycache__", ".uceditor", ".slc_state"}
                if relative == Path("."):
                    excluded.update({"data", "test-output", ".vscode"})
                if relative.parts[:1] == ("firmware",):
                    excluded.add(".vscode")
                    if len(relative.parts) == 3 and relative.parts[2] == "cmake_gcc":
                        excluded.add("build")
                folders[:] = sorted(name for name in folders if name not in excluded)
                for name in sorted(files):
                    path = Path(directory) / name
                    if path.relative_to(root) in private or (
                        relative == Path(".") and name.endswith(".code-workspace")
                    ) or name.endswith((".pyc", ".pyo", ".crswap")) or name in {
                        "board_roles.json", "genie_local_paths.cmake", ".projectlinkstore", ".silabsExtContext", "vscode.conf",
                        ".DS_Store", "Thumbs.db", "Desktop.ini",
                    } or path.resolve() in {destination.resolve(), temporary.resolve()}:
                        continue
                    target = "genie_testing/" + path.relative_to(root).as_posix()
                    archive.writestr(target, path.read_bytes())
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
