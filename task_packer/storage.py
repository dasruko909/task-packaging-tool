"""Safe file writes and discovery of unfinished projects."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import fcntl
import shutil
import time
from pathlib import Path
from typing import IO

from .models import WorkflowState


STATE_ROOT = Path(".packer-projects")
TRASH_ROOT = Path(".packer-trash")


class StateReadError(ValueError):
    """A state needs manual repair; includes the file and the concrete cause."""


def _read_state(path: Path) -> WorkflowState:
    try:
        return WorkflowState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as error:
        raise StateReadError(f"{path}: {error}") from error


class ProjectLock:
    def __init__(self, handle: IO[str]):
        self._handle = handle

    def close(self) -> None:
        self._handle.close()

    def __del__(self) -> None:
        self.close()


def acquire_project_lock(codename: str) -> ProjectLock:
    """Prevent two packer processes from changing one project at once."""

    directory = STATE_ROOT / codename
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".lock"
    handle = path.open("a+", encoding="utf-8")
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
        self.codename = codename
        self.path = STATE_ROOT / codename / "state.json"
        self._lock: ProjectLock | None = None

    def acquire_lock(self) -> ProjectLock:
        """Acquire and retain this project's lock for the store's lifetime."""
        if self._lock is None:
            self._lock = acquire_project_lock(self.codename)
        return self._lock

    def save(self, state: WorkflowState) -> None:
        write_json(self.path, state.to_dict())

    def load(self) -> WorkflowState:
        self._recover_restore()
        return _read_state(self.path)

    def _recover_restore(self) -> None:
        """Finish or roll back a restore interrupted between filesystem swaps."""
        journal = self.path.parent / '.restore-transaction.json'
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
        if not STATE_ROOT.exists():
            return projects
        for path in sorted(STATE_ROOT.glob("*/state.json")):
            try:
                state = _read_state(path)
            except StateReadError as error:
                state = error
            projects.append((path.parent.name, state))
        return projects


def delete_project(codename: str) -> Path:
    """Usuwa projekt z listy, przenoszac wszystkie jego dane do odzyskiwalnego kosza."""

    if (
        not codename
        or len(codename) > 20
        or not codename.isascii()
        or any(not (character.islower() or character.isdigit() or character in "-_")
               for character in codename)
    ):
        raise ValueError("Invalid project code.")
    targets = [
        (STATE_ROOT / codename, "state"),
        (Path("input") / codename, "input"),
        (Path("output") / codename, "output"),
        (Path("output") / f"{codename}.zip", f"{codename}.zip"),
        (Path("output") / f"{codename}.zip.sha256", f"{codename}.zip.sha256"),
    ]
    existing = [(source, name) for source, name in targets if source.exists()]
    if not existing:
        raise RuntimeError(f"No project data exists for {codename!r} to remove.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = TRASH_ROOT / f"{stamp}-{codename}"
    suffix = 2
    while archive.exists():
        archive = TRASH_ROOT / f"{stamp}-{codename}-{suffix}"
        suffix += 1
    archive.mkdir(parents=True)
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
