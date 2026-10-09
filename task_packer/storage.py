"""Safe file writes and discovery of unfinished projects."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import fcntl
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import IO

from .models import WorkflowState, validate_material_names
from .paths import checked_path, checked_tree, project_code, project_paths


STATE_ROOT = Path(".packer-projects")
TRASH_ROOT = Path(".packer-trash")


class StateReadError(ValueError):
    """A state needs manual repair; includes the file and the concrete cause."""


def _read_state(path: Path) -> WorkflowState:
    try:
        checked_path(path)
        state = WorkflowState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        validate_state_identity(state, path.parent.name)
        for owned in project_paths(path.parent.name)[:3]:
            checked_tree(owned)
        return state
    except (OSError, ValueError, TypeError) as error:
        raise StateReadError(f"{path}: {error}") from error


def validate_state_identity(state: WorkflowState, codename: str) -> None:
    project_code(codename)
    for location, code in [('config', state.config.codename if state.config else None),
                           ('setup', state.setup.get('codename'))]:
        if code is not None:
            project_code(code)
            if code != codename:
                raise ValueError(f"{location}.codename {code!r} does not match project {codename!r}.")
    for values in (state.setup, asdict(state.config) if state.config else {}):
        validate_material_names({**values, 'codename': codename}, 'state')


def ensure_new_project(store: 'StateStore') -> None:
    """Call while holding the project lock, before saving any materials."""
    paths = project_paths(store.codename)
    for path in [checked_path(store.path), *paths[2:]]:
        if path.exists():
            raise RuntimeError(f"Project {store.codename!r} already exists: {path}. Use --project or a different code.")


class ProjectLock:
    def __init__(self, handle: IO[str]):
        self._handle = handle

    def close(self) -> None:
        self._handle.close()

    def __del__(self) -> None:
        self.close()


def acquire_project_lock(codename: str) -> ProjectLock:
    """Prevent two packer processes from changing one project at once."""

    project_paths(codename)
    directory = checked_path(STATE_ROOT / project_code(codename))
    directory.mkdir(parents=True, exist_ok=True)
    path = checked_path(directory / ".lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise RuntimeError(
            f"Projekt {codename!r} jest juz otwarty w innym procesie. "
            "Finish work in that window or choose another task."
        ) from error
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return ProjectLock(handle)


def atomic_write_text(path: Path, content: str) -> None:
    """Write text through a temporary file and an atomic replacement."""

    checked_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class StateStore:
    """Repository for a single state file."""

    def __init__(self, codename: str):
        self.codename = project_code(codename)
        self.path = checked_path(STATE_ROOT / codename / "state.json")
        self._lock: ProjectLock | None = None

    def acquire_lock(self) -> ProjectLock:
        """Acquire and retain this project's lock for the store's lifetime."""
        if self._lock is None:
            self._lock = acquire_project_lock(self.codename)
        return self._lock

    def save(self, state: WorkflowState) -> None:
        validate_state_identity(state, self.codename)
        # Validate programmatically edited fields as well as loaded JSON.
        validated = WorkflowState.from_dict(state.to_dict())
        if 'language_code' in validated.setup:
            state.setup['language_code'] = validated.setup['language_code']
        if state.config:
            state.config.language_code = validated.config.language_code
        project_paths(self.codename)
        write_json(self.path, state.to_dict())

    def load(self) -> WorkflowState:
        # A malformed or foreign state must not trigger recovery mutations.
        state = _read_state(self.path)
        project_paths(self.codename)
        self._recover_restore()
        return state

    def _recover_restore(self) -> None:
        """Finish or roll back a restore interrupted between filesystem swaps."""
        journal = checked_path(self.path.parent / '.restore-transaction.json')
        if not journal.is_file():
            return
        try:
            data = json.loads(journal.read_text(encoding='utf-8'))
            expected = data['expected_state_sha256']
            assets = data['assets']
            transaction = Path(data['transaction'])
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise RuntimeError(
                f'Unreadable project recovery journal: {journal}'
            ) from error
        # The journal is untrusted persisted input, not authority to move paths.
        expected_transaction = checked_path(self.path.parent / '.restore-work').absolute()
        destinations = {'package': project_paths(self.codename)[2],
                        **{name: self.path.parent / name for name in
                           ('notes', 'specification.json', 'statement-audit.json')}}
        if transaction != expected_transaction or not isinstance(assets, list):
            raise ValueError(f'Unsafe recovery journal: {journal}')
        allowed = [{
            'destination': str(checked_path(destination).absolute()),
            'original': str(expected_transaction / 'originals' / name),
            'staged': str(expected_transaction / 'staged' / name),
        } for name, destination in destinations.items()]
        if assets != allowed:
            raise ValueError(f'Unsafe recovery paths: {journal}')
        checked_tree(transaction)
        for destination in destinations.values():
            checked_tree(destination)
        current_digest = (
            hashlib.sha256(self.path.read_bytes()).hexdigest()
            if self.path.is_file() else ''
        )
        if current_digest != expected:
            for asset in reversed(assets):
                destination = Path(asset['destination'])
                original = Path(asset['original'])
                replacement = Path(asset['staged'])
                swapped = original.exists() or original.is_symlink() or not replacement.exists()
                if not swapped:
                    continue
                if destination.is_symlink() or destination.is_file():
                    destination.unlink()
                elif destination.is_dir():
                    shutil.rmtree(destination)
                if original.exists() or original.is_symlink():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(original), str(destination))
        shutil.rmtree(transaction, ignore_errors=True)
        journal.unlink(missing_ok=True)

    @staticmethod
    def available() -> list[tuple[str, WorkflowState | StateReadError]]:
        projects: list[tuple[str, WorkflowState | StateReadError]] = []
        checked_path(STATE_ROOT)
        if not STATE_ROOT.exists():
            return projects
        for directory in sorted(STATE_ROOT.iterdir()):
            path = directory / 'state.json'
            try:
                checked_path(directory)
                if not path.exists() and not path.is_symlink():
                    continue
                project_code(directory.name)
                state = _read_state(path)
            except ValueError as error:
                state = StateReadError(f"{path}: {error}")
            projects.append((directory.name, state))
        return projects


def delete_project(codename: str) -> Path:
    """Usuwa projekt z listy, przenoszac wszystkie jego dane do odzyskiwalnego kosza."""

    project_paths(codename)
    targets = [
        (STATE_ROOT / codename, "state"),
        (Path("input") / codename, "input"),
        (Path("output") / codename, "output"),
        (Path("output") / f"{codename}.zip", f"{codename}.zip"),
        (Path("output") / f"{codename}.zip.sha256", f"{codename}.zip.sha256"),
    ]
    for source, _ in targets:
        checked_tree(source)
    checked_path(TRASH_ROOT)
    existing = [(source, name) for source, name in targets if source.exists()]
    if not existing:
        raise RuntimeError(f"No project data exists for {codename!r} to remove.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = TRASH_ROOT / f"{stamp}-{codename}"
    suffix = 2
    while archive.exists():
        archive = TRASH_ROOT / f"{stamp}-{codename}-{suffix}"
        suffix += 1
    checked_path(archive).mkdir(parents=True)
    try:
        for source, name in existing:
            shutil.move(str(source), archive / name)
    except BaseException:
        # Best-effort recovery after a partial move.
        for source, name in reversed(existing):
            saved = archive / name
            if saved.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(saved), source)
        archive.rmdir()
        raise
    return archive.resolve()
