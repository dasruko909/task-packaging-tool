"""Data models saved in the project state file.

The classes are deliberately simple so JSON remains human-readable and can be
repaired with a plain text editor if needed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Subtask:
    """One subtask and its share of the score."""

    index: int
    name: str
    points: int
    constraints: str
    group_name: str = ""


@dataclass(slots=True)
class ProjectConfig:
    """Information shared by every package-creation stage."""

    codename: str
    title: str
    origin: str
    language_code: str
    original_statement: str
    subtasks: list[Subtask]
    statement_idea: str = ""
    generator_language: str = "cpp"
    solution_language: str = "cpp"
    time_limit_ms: int = 2000
    memory_limit_kb: int = 262_144
    task_type: str = "auto"
    statement_source: str = "paste"
    statement_pdf: str = ""
    input_package: str = ""
    image_files: list[str] = field(default_factory=list)
    image_placements: dict[str, str] = field(default_factory=dict)
    test_plan: dict[str, Any] = field(default_factory=dict)
    existing_tests: bool = False
    judge_notes: str = ""
    specification: dict[str, Any] = field(default_factory=dict)
    solution_files: dict[str, str] = field(default_factory=dict)
    sample_files: list[dict[str, str]] = field(default_factory=list)

    @property
    def package_dir(self) -> Path:
        return Path("output") / self.codename

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectConfig":
        values = dict(data)
        values["subtasks"] = [Subtask(**item) for item in data["subtasks"]]
        # Complete states saved before import and special-task support existed.
        values.setdefault("task_type", "standard")
        values.setdefault("statement_source", "paste")
        values.setdefault("statement_pdf", "")
        values.setdefault("input_package", "")
        values.setdefault("statement_idea", "")
        values.setdefault("image_files", [])
        values.setdefault("image_placements", {})
        values.setdefault("test_plan", {})
        values.setdefault("existing_tests", False)
        values.setdefault("judge_notes", "")
        return cls(**values)


@dataclass(slots=True)
class WorkflowState:
    """Persistent progress log.

    ``completed`` contains small units of work, for example ``generator:2``.
    ``drafts`` stores a model response before approval, so interrupting the
    program at a review screen does not lose text.
    """

    schema_version: int = 4
    config: ProjectConfig | None = None
    # Fields from stage one are stored here immediately. Once setup is complete,
    # this dictionary is cleared and the final data moves to ``config``.
    setup: dict[str, Any] = field(default_factory=dict)
    completed: list[str] = field(default_factory=list)
    drafts: dict[str, str] = field(default_factory=dict)
    usage: dict[str, float | int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    )
    finished: bool = False
    feedback: dict[str, str] = field(default_factory=dict)
    previous_drafts: dict[str, str] = field(default_factory=dict)

    def is_done(self, key: str) -> bool:
        return key in self.completed

    def mark_done(self, key: str) -> None:
        if key not in self.completed:
            self.completed.append(key)
        self.drafts.pop(key, None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowState":
        values = dict(data)
        config = values.get("config")
        values["config"] = ProjectConfig.from_dict(config) if config else None
        # State files created by the first version had no ``setup`` field.
        # Filling it in here preserves backward compatibility.
        values.setdefault("setup", {})
        values.setdefault(
            "usage", {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        if int(values.get('schema_version', 1)) > 4:
            raise ValueError('State comes from a newer version of the packer.')
        values["schema_version"] = 4
        return cls(**values)
