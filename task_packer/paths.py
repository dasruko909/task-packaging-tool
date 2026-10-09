"""Validation for owned paths; explicitly selected source files stay external."""
from __future__ import annotations

import os
import re
from pathlib import Path, PureWindowsPath


def project_code(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r'[a-z0-9_-]{1,20}', value) is None:
        raise ValueError(f"Invalid project code {value!r}: use 1–20 ASCII lowercase letters, digits, - or _.")
    return value


def language_code(value: str) -> str:
    if not isinstance(value, str) or value.strip().lower() not in {'pl', 'en'}:
        raise ValueError(f"Invalid statement language_code {value!r}: expected pl or en.")
    return value.strip().lower()


def checked_path(path: Path) -> Path:
    """Reject links in every existing component, including dangling links."""
    path = Path(path)
    if '..' in path.parts:
        raise ValueError(f"Parent traversal is forbidden: {path}")
    absolute = path.absolute()
    for part in (*reversed(absolute.parents), absolute):
        if part.is_symlink():
            raise ValueError(f"Symbolic link is forbidden: {part}")
    return path


def relative_path(root: Path, name: str, *, flat: bool = False) -> Path:
    """Keep manifest paths inside their designated directory, retaining subdirs."""
    if (not isinstance(name, str) or not name or '\x00' in name or '\\' in name
            or Path(name).is_absolute() or PureWindowsPath(name).drive
            or '..' in Path(name).parts or Path(name) == Path('.')
            or (flat and Path(name).name != name)):
        raise ValueError(f"Invalid relative file name in {root}: {name!r}")
    return checked_path(root / name)


def checked_tree(root: Path) -> Path:
    """Inspect only the chosen tree; never descend into a symbolic link."""
    checked_path(root)
    if root.is_dir():
        for directory, folders, files in os.walk(root, followlinks=False):
            for name in folders + files:
                path = Path(directory) / name
                if path.is_symlink():
                    raise ValueError(f"Symbolic link is forbidden: {path}")
    return root


def project_paths(codename: str) -> list[Path]:
    project_code(codename)
    return [checked_path(path) for path in (
        Path('.packer-projects') / codename, Path('input') / codename,
        Path('output') / codename, Path('output') / f'{codename}.zip',
        Path('output') / f'{codename}.zip.sha256')]
