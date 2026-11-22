"""Create private experiment and laptop settings without replacing saved files."""

from pathlib import Path


def local_paths(root):
    defaults = Path(root) / "support/local_defaults"
    return {path.relative_to(defaults) for path in defaults.rglob("*") if path.is_file()}


def initialize_local_files(root):
    root = Path(root)
    defaults = root / "support/local_defaults"
    created = []
    for relative in sorted(local_paths(root)):
        target = root / relative
        if target.exists():
            continue
        contents = (defaults / relative).read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as stream:
                stream.write(contents)
        except FileExistsError:
            continue
        created.append(target)
    return created
