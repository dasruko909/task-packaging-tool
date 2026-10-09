"""Shared manifest-backed registry for grading, examples and reproducibility."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass

from .paths import relative_path, checked_tree


def safe_file(root: Path, name: str) -> Path:
    return relative_path(root, name)


@dataclass(frozen=True)
class ProgramRecord:
    """One executable entry point selected by the package manifest."""

    role: str
    name: str
    source: Path
    root: Path
    dependencies: tuple[Path, ...]
    language: str | None


def programs(root: Path, manifest: dict) -> list[ProgramRecord]:
    """Return only manifest-registered checker, solution and generator programs."""
    result = []
    for key, folder, singular in (
        ('checker', 'checker', True),
        ('solutions', 'solutions', False),
        ('generators', 'generators', False),
    ):
        raw = manifest.get(key)
        entries = [raw] if singular and raw else (raw or [])
        for entry in entries:
            name = entry['name']
            base = root / folder
            result.append(ProgramRecord(
                role=key[:-1] if key.endswith('s') else key,
                name=name,
                source=safe_file(base, name),
                root=base,
                dependencies=tuple(
                    safe_file(base, dependency)
                    for dependency in entry.get('additional_files_names', [])
                ),
                language=entry.get('prog_lang'),
            ))
    return result


def validate_manifest(root: Path, data: dict) -> None:
    """Validate all file references before any consumer reads or writes them."""
    if not isinstance(data, dict):
        raise ValueError(f'{root / "config.json"}: manifest must be a JSON object.')
    try:
        for key, folder in (('checker', 'checker'), ('solutions', 'solutions'),
                            ('generators', 'generators')):
            programs = [data[key]] if key == 'checker' and data.get(key) else data.get(key, [])
            for program in programs:
                safe_file(root / folder, program['name'])
                # libsolve prefixes the full name when preparing programs.
                safe_file(root / folder, 'prepared_' + program['name'])
                for name in program.get('additional_files_names', []):
                    safe_file(root / folder, name)
        for group in data.get('test_groups', []):
            for test in group.get('tests', []):
                safe_file(root / 'tests/in', test['input'])
                safe_file(root / 'tests/out', output_name(test))
        for key in ('test_generation', 'test_generations'):
            for generation in data.get(key, []):
                safe_file(root / 'generators', generation['generator'])
                names = generation['filename']
                for name in names if isinstance(names, list) else [names]:
                    safe_file(root / 'tests/in', name)
        for key, folder in (('descriptions', 'description'), ('editorials', 'editorial')):
            for language, name in data.get(key, {}).items():
                relative_path(root / folder, language, flat=True)
                safe_file(root / folder, name)
        # libsolve also constructs statement paths from title/default_language.
        for language in data.get('title', {}):
            relative_path(root / 'description', language, flat=True)
        if data.get('default_language'):
            relative_path(root / 'description', data['default_language'], flat=True)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError(f'{root / "config.json"}: {error}') from error


def output_name(test: dict) -> str:
    return test.get('output') or test['input'].replace('.in', '.out')


@dataclass(frozen=True)
class TestRecord:
    input: str
    output: str
    group: str
    sample: bool
    generation: dict | None


def records(manifest: dict) -> list[TestRecord]:
    generations = {}
    for generation in manifest.get('test_generation', manifest.get('test_generations', [])):
        names = generation['filename']
        for name in names if isinstance(names, list) else [names]:
            generations[name] = generation
    result = []
    seen = set()
    for index, group in enumerate(manifest.get('test_groups', [])):
        for test in group.get('tests', []):
            if test['input'] in seen:
                raise ValueError(f"Test assigned more than once: {test['input']}")
            seen.add(test['input'])
            result.append(TestRecord(test['input'], output_name(test), str(group.get('name', index)),
                                     bool(group.get('is_sample', group.get('sample', False))), generations.get(test['input'])))
    return result


def load_manifest(root: Path) -> dict:
    checked_tree(root)
    path = safe_file(root, 'config.json')
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    validate_manifest(root, data)
    return data


def bind_subtasks(config, groups: list[dict]) -> None:
    """One explicit group per subtask; imported weights use Solve rounding."""
    graded = [g for g in groups if not g.get('is_sample', g.get('sample', False))]
    if not graded:
        return
    if len(graded) != len(config.subtasks):
        raise ValueError('The number of subtasks differs from the number of scored groups. Align subtasks with the manifest before generating the statement.')
    weights = [g.get('score', 1) for g in groups]
    if any(type(w) is not int or w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError('Groups must have non-negative weights and a positive total score.')
    points = [w * 100 // sum(weights) for w in weights]
    remaining = 100 - sum(points)
    for index in reversed(range(len(points))):
        if weights[index] > 0 and remaining:
            points[index] += 1
            remaining -= 1
    for index, group in enumerate(groups):
        group.setdefault('name', str(index))
    for subtask, group in zip(config.subtasks, graded):
        if subtask.group_name and subtask.group_name != str(group['name']):
            raise ValueError(f'The group assignment for subtask {subtask.index} changed.')
        subtask.group_name = str(group['name'])
        subtask.points = points[groups.index(group)]


def solution_path(config, subtask) -> Path:
    from .solve4 import source_extension
    name = config.solution_files.get(str(subtask.index), f'solution_{subtask.index:02d}.{source_extension(config.solution_language)}')
    return safe_file(config.package_dir / 'solutions', name)
