"""Drop zone for PDFs, images, and existing Solve 4 packages."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .models import ProjectConfig
from .storage import atomic_write_text
from .paths import checked_path, checked_tree, project_code, language_code as normalize_language
from .registry import safe_file, load_manifest


INPUT_ROOT = Path("input")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def input_dir(codename: str) -> Path:
    return checked_path(INPUT_ROOT / project_code(codename))


def package_drop_dir(codename: str) -> Path:
    return checked_tree(input_dir(codename) / "package")


def images_drop_dir(codename: str) -> Path:
    return checked_tree(input_dir(codename) / "images")


def pdf_drop_path(codename: str) -> Path:
    return checked_path(input_dir(codename) / "statement.pdf")


def prepare_drop_zones(codename: str) -> None:
    """Create one obvious location for all user-supplied materials."""

    package_drop_dir(codename).mkdir(parents=True, exist_ok=True)
    images_drop_dir(codename).mkdir(parents=True, exist_ok=True)
    guide = f"""MATERIALS FOR PROJECT: {codename}

1. Existing package or tests only
   Copy the package CONTENTS to:
   input/{codename}/package/

   Correct example:
   input/{codename}/package/config.json
   input/{codename}/package/tests/in/1a.in
   input/{codename}/package/tests/out/1a.out

   If you have only tests, create exactly the tests/in and tests/out directories.

2. Statement available only as a PDF
   Name the file statement.pdf and put it here:
   input/{codename}/statement.pdf

3. Images used in the statement
   Copy PNG, JPG, JPEG, or WEBP files to:
   input/{codename}/images/
   The program will later ask where each image belongs in the statement.

Do not add the whole parent directory as another nesting level. The package
directory must contain config.json or the tests directory directly.
"""
    atomic_write_text(input_dir(codename) / "INSTRUCTIONS.txt", guide)


def package_defaults(codename: str) -> dict[str, Any]:
    """Read safe defaults from an existing config.json."""

    path = safe_file(package_drop_dir(codename), "config.json")
    if not path.is_file():
        return {}
    try:
        data = load_manifest(package_drop_dir(codename))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def find_package_statement(codename: str, language_code: str = "pl") -> Path | None:
    language_code = normalize_language(language_code)
    preferred = package_drop_dir(codename) / "description" / f"{language_code}.md"
    if preferred.is_file():
        return preferred
    description = package_drop_dir(codename) / "description"
    return next(iter(sorted(description.glob("*.md"))), None)


def list_images(codename: str) -> list[Path]:
    """Return only regular, supported files from the flat image directory."""

    directory = images_drop_dir(codename)
    return [
        path
        for path in sorted(directory.iterdir())
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in IMAGE_SUFFIXES
    ]


def package_has_tests(codename: str) -> bool:
    tests = package_drop_dir(codename) / "tests" / "in"
    return tests.is_dir() and any(path.is_file() for path in tests.iterdir())


def validate_package_drop(codename: str) -> None:
    root = package_drop_dir(codename)
    useful = (root / "config.json").is_file() or (root / "tests" / "in").is_dir()
    if not useful:
        raise RuntimeError(
            f"No package was found in {root}. Copy its contents as described in "
            f"{input_dir(codename) / 'INSTRUCTIONS.txt'}."
        )


def validate_pdf(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"PDF not found: {path}")
    if path.suffix.lower() != ".pdf" or path.stat().st_size == 0:
        raise RuntimeError(f"The file is not a valid PDF: {path}")


def _copy_tree_without_symlinks(source: Path, destination: Path) -> None:
    """Copy a package without executing or following symbolic links."""

    checked_tree(source)
    checked_tree(destination)
    load_manifest(source)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"Unsafe symbolic link skipped in package: {path}")
        target = safe_file(destination, path.relative_to(source).as_posix())
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def import_existing_materials(config: ProjectConfig) -> None:
    """Copy the package and images to the output directory once."""

    config.validate_paths()
    if config.input_package:
        _copy_tree_without_symlinks(Path(config.input_package), config.package_dir)

    destination = checked_tree(config.package_dir / "description")
    destination.mkdir(parents=True, exist_ok=True)
    for image_name in config.image_files:
        source = safe_file(images_drop_dir(config.codename), image_name)
        if not source.is_file():
            if safe_file(destination, image_name).is_file():
                continue
            raise RuntimeError(f"Saved image is missing: {source}")
        shutil.copy2(source, safe_file(destination, image_name))
    from .registry import bind_subtasks
    manifest = load_manifest(config.package_dir)
    if manifest.get('test_groups'):
        bind_subtasks(config, manifest['test_groups'])
    for name in sorted(destination.iterdir()):
        if name.is_file() and not name.is_symlink() and name.suffix.lower() in IMAGE_SUFFIXES and name.name not in config.image_files:
            config.image_files.append(name.name)
    for solution in manifest.get('solutions', []):
        types = solution.get('type', [])
        if types == 'model' or isinstance(types, list) and 'model' in types:
            path = safe_file(config.package_dir / 'solutions', solution['name'])
            if path.is_file():
                config.solution_files.setdefault(str(config.subtasks[-1].index), solution['name'])
                break
    summary = {
        'descriptions': sorted(p.name for p in destination.iterdir() if p.is_file()),
        'images': config.image_files, 'checker': manifest.get('checker'),
        'solutions': manifest.get('solutions', []), 'reused_solutions': config.solution_files,
        'generators': manifest.get('generators', []), 'groups': manifest.get('test_groups', []),
    }
    from .storage import write_json
    write_json(config.package_dir / 'import-summary.json', summary)
    print('Import: ' + str(len(summary['descriptions'])) + ' statement files, '
          + str(len(config.image_files)) + ' images; reusable solutions: '
          + str(config.solution_files or 'none') + '.')


def statement_attachments(config: ProjectConfig) -> list[Path]:
    """Visual materials sent to the model for task-type detection and statement drafting."""

    config.validate_paths()
    attachments: list[Path] = []
    if config.statement_pdf:
        attachments.append(Path(config.statement_pdf))
    for name in config.image_files:
        candidates = [safe_file(config.package_dir / 'description', name), safe_file(images_drop_dir(config.codename), name)]
        if config.input_package:
            candidates.append(safe_file(Path(config.input_package) / 'description', name))
        attachments.append(next((path for path in candidates if path.is_file()), safe_file(images_drop_dir(config.codename), name)))
    if config.input_package:
        for path in sorted(checked_tree(Path(config.input_package) / 'description').glob('*')):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in IMAGE_SUFFIXES and path.name not in {p.name for p in attachments}:
                attachments.append(path)
    return attachments
