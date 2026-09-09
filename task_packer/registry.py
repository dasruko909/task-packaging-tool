"""Shared manifest-backed registry for grading, examples and reproducibility."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass


def safe_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).name != name or name in {'.', '..'}:
        raise ValueError(f"Invalid file name: {name!r}")
    path = root / name
    if path.is_symlink():
        raise ValueError(f"Symlink found where a file is required: {path}")
    return path


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
    path = root / 'config.json'
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Manifest musi byc obiektem JSON.')
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
