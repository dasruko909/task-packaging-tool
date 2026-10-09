"""Data models saved in the project state file.

The classes are deliberately simple so JSON remains human-readable and can be
repaired with a plain text editor if needed.
"""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from types import UnionType
from typing import Any, get_args, get_origin, get_type_hints

from .paths import checked_path, project_code, language_code, relative_path


def _validate(value: Any, expected: Any, location: str) -> None:
    """Check the JSON shape before constructing the persisted dataclasses."""
    if expected is Any:
        return
    origin = get_origin(expected)
    arguments = get_args(expected)
    if origin is UnionType:
        for option in arguments:
            try:
                _validate(value, option, location)
                return
            except ValueError:
                pass
        # Report the useful nested error for optional config objects.
        if type(None) in arguments and value is not None:
            _validate(value, arguments[0], location)
        raise ValueError(f"{location}: expected {expected}")
    if is_dataclass(expected):
        if not isinstance(value, dict):
            raise ValueError(f"{location}: expected an object")
        annotations = get_type_hints(expected)
        for key in value:
            if key not in annotations:
                raise ValueError(f"{location}.{key}: unknown field")
        for item in fields(expected):
            if item.name not in value:
                if item.default is MISSING and item.default_factory is MISSING:
                    raise ValueError(f"{location}.{item.name}: missing required field")
            else:
                _validate(value[item.name], annotations[item.name], f"{location}.{item.name}")
        return
    container = origin or expected
    valid = (type(value) in (int, float) if container is float else type(value) is container)
    if not valid:
        raise ValueError(f"{location}: expected {container.__name__}")
    if origin is list:
        for index, item in enumerate(value):
            _validate(item, arguments[0], f"{location}[{index}]")
    elif origin is dict:
        for key, item in value.items():
            _validate(key, arguments[0], f"{location} key")
            _validate(item, arguments[1], f"{location}.{key}")


def validate_material_names(values: dict, location: str) -> None:
    for field_name, field_type in {
        'codename': str, 'language_code': str, 'input_package': str,
        'image_files': list[str], 'image_placements': dict[str, str],
        'solution_files': dict[str, str], 'sample_files': list[dict[str, str]],
    }.items():
        if field_name in values:
            _validate(values[field_name], field_type, f'{location}.{field_name}')
    code = values.get('codename')
    if 'codename' in values:
        project_code(code)
    root = Path('output') / code if code else Path('.')
    package = values.get('input_package')
    if package and code:
        source = checked_path(Path(package)).absolute()
        source_root = checked_path(Path('input') / code).absolute()
        if not source.is_relative_to(source_root):
            raise ValueError(f'{location}.input_package {package!r} must be inside {source_root}.')
    if 'language_code' in values:
        values['language_code'] = language_code(values['language_code'])
    for name in [*values.get('image_files', []), *values.get('image_placements', {})]:
        relative_path(root / 'description', name, flat=True)
    for name in values.get('solution_files', {}).values():
        relative_path(root / 'solutions', name)
    for sample in values.get('sample_files', []):
        for field in ('input', 'output'):
            if field in sample:
                relative_path(root / 'tests' / ('in' if field == 'input' else 'out'), sample[field])


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

    def __post_init__(self) -> None:
        self.validate_paths()

    def validate_paths(self) -> None:
        values = asdict(self)
        validate_material_names(values, 'config')
        self.language_code = values['language_code']

    @property
    def package_dir(self) -> Path:
        project_code(self.codename)
        self.language_code = language_code(self.language_code)
        return checked_path(Path("output") / self.codename)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectConfig":
        _validate(data, cls, "config")
        if not data["subtasks"]:
            raise ValueError("config.subtasks: must contain at least one subtask")
        values = dict(data)
        validate_material_names(values, "config")
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
        if not isinstance(data, dict):
            raise ValueError("state: expected an object")
        version = data.get("schema_version", 1)
        if type(version) is not int or version < 1:
            raise ValueError("state.schema_version: expected a positive integer")
        if version > 4:
            raise ValueError('State comes from a newer version of the packer.')
        _validate(data, cls, "state")
        values = dict(data)
        config = values.get("config")
        values["config"] = ProjectConfig.from_dict(config) if config is not None else None
        # State files created by the first version had no ``setup`` field.
        # Filling it in here preserves backward compatibility.
        values["setup"] = dict(values.get("setup", {}))
        validate_material_names(values["setup"], "state.setup")
        values.setdefault(
            "usage", {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        values["schema_version"] = 4
        return cls(**values)
