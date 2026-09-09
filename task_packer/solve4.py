"""Build and locally validate a package compatible with the Solve 4 layout."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import shlex
from pathlib import Path

from .models import ProjectConfig
from .test_plan import counts_for, total_tests
from .statement_format import (
    MAX_TESTS_PER_SUBTASK,
    EXAMPLE_FILE_MAX_BYTES,
    example_references,
    test_filename,
)
from .storage import atomic_write_text, write_json
from .registry import load_manifest, records, bind_subtasks, solution_path


LANGUAGE_EXTENSIONS = {
    "cpp": "cpp",
    "c++": "cpp",
    "python": "py",
    "python3": "py",
    "cpp17": "cpp",
    "cpp20": "cpp",
    "py": "py",
    "java": "java",
    "c": "c",
}


def solve_language(language: str) -> str:
    return {"cpp": "cpp17", "c++": "cpp17", "python": "python3", "py": "python3"}.get(language.lower(), language.lower())


def source_extension(language: str) -> str:
    return LANGUAGE_EXTENSIONS.get(language.strip().lower(), "txt")


def _test_plan_for(config: ProjectConfig) -> dict[int, dict[str, int]]:
    plan = config.test_plan if isinstance(config.test_plan, dict) else {}
    raw = plan.get("subtasks")
    if not isinstance(raw, list):
        return {}
    result: dict[int, dict[str, int]] = {}
    for item in raw:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            continue
        counts = counts_for({"subtasks": [item]}, item["index"])
        if counts is None:
            continue
        result[item["index"]] = {
            "generator_runs": counts[0],
            "corner_tests": counts[1],
        }
    return result


def _planned_total_tests(config: ProjectConfig) -> int | None:
    plan = config.test_plan if isinstance(config.test_plan, dict) else {}
    return total_tests(plan)


def statement_validation_errors(
    markdown: str, task_type: str, language_code: str | None = None
) -> list[str]:
    """Validate required Solve 4 Markdown sections and their order."""

    if task_type == "interactive":
        polish = [
            "## Interakcja", "## Ograniczenia", "## Podzadania",
            "## Przykładowa interakcja", "## Narzędzia do testowania lokalnego",
        ]
        legacy = [
            "## Interakcja", "## Ograniczenia", "## Podzadania",
            "## Przykladowa interakcja", "## Narzedzia do testowania lokalnego",
        ]
        english = [
            "## Interaction", "## Constraints", "## Subtasks",
            "## Sample interaction", "## Local testing tools",
        ]
        section_sets = [polish] if language_code and language_code.lower() == "pl" else (
            [english] if language_code else [legacy, english]
        )
    else:
        polish = ["## Wejście", "## Wyjście", "## Ograniczenia", "## Podzadania", "## Przykład"]
        legacy = ["## Wejscie", "## Wyjscie", "## Ograniczenia", "## Podzadania", "## Przyklad"]
        english = ["## Input", "## Output", "## Constraints", "## Subtasks", "## Example"]
        section_sets = [polish] if language_code and language_code.lower() == "pl" else (
            [english] if language_code else [legacy, english]
        )
    # Headings inside examples and code blocks are not statement sections.
    outside_code = re.sub(r'^(`{3,}|~{3,}).*?^\1[ \t]*$', '', markdown, flags=re.M | re.S)
    headings = re.findall(r'^##[ \t]+([^\n]+?)\s*$', outside_code, flags=re.M)
    sections = next((candidate for candidate in section_sets if all(section[3:] in headings for section in candidate)), section_sets[0])
    expected = [section[3:] for section in sections]
    positions = [headings.index(section) if section in headings else -1 for section in expected]
    missing = [section for section, position in zip(sections, positions) if position < 0]
    errors = [f"Missing section {section}." for section in missing]
    present_positions = [position for position in positions if position >= 0]
    if present_positions != sorted(present_positions):
        errors.append("Statement sections are in the wrong order.")
    if any(heading not in expected for heading in headings) or len(headings) != len(set(headings)):
        errors.append("Use only the required sections, each exactly once; place examples and their data in the Example section.")
    if task_type == "interactive" and "adapt" not in markdown.lower():
        errors.append("The statement does not specify whether the interactor is adaptive.")
    return errors


def create_package_skeleton(config: ProjectConfig) -> None:
    """Create all standard directories without removing existing files."""

    root = config.package_dir
    for relative in (
        "checker",
        "description",
        "editorial",
        "generators",
        "public",
        "solutions",
        "tests/in",
        "tests/out",
        "tests/valid",
        "validators",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)


def _test_names(
    config: ProjectConfig, subtask_index: int, generator_runs: int | None = None
) -> list[str]:
    group = str(subtask_index)
    corner_dir = config.package_dir / "tests" / "in"
    if config.existing_tests:
        return sorted(
            path.name
            for path in corner_dir.iterdir()
            if path.is_file()
            and re.search(rf"(?:^|\.)0*{re.escape(group)}(?:[A-Za-z]|$)", path.name)
        )
    count = 3 if generator_runs is None else max(0, generator_runs)
    if count > MAX_TESTS_PER_SUBTASK:
        raise ValueError(f"Subtask {subtask_index} has more than 26 tests.")
    generated = [test_filename(subtask_index, offset, "in") for offset in range(count)]
    generated_set = set(generated)
    manual = sorted(
        path.name
        for path in corner_dir.glob(f"{subtask_index}[a-z].in")
        if path.name not in generated_set
    )
    if len(generated) + len(manual) > MAX_TESTS_PER_SUBTASK:
        raise ValueError(f"Subtask {subtask_index} has more than 26 tests.")
    return generated + manual


def _migrate_generated_test_names(config: ProjectConfig, manifest: dict) -> None:
    """Migrate legacy packer tests to the 0a.in/1a.in scheme without changing imports."""

    groups = manifest.get("test_groups")
    if config.existing_tests or not isinstance(groups, list) or not groups:
        return
    moves: list[tuple[Path, Path]] = []
    sample_renames: dict[tuple[str, str], tuple[str, str]] = {}
    graded_index = 0
    for group in groups:
        tests = group.get("tests", []) if isinstance(group, dict) else []
        if not isinstance(tests, list):
            continue
        if len(tests) > MAX_TESTS_PER_SUBTASK:
            raise ValueError("A group has more than 26 tests and cannot fit in a--z names.")
        sample = bool(group.get("is_sample", group.get("sample", False)))
        if sample:
            subtask_index = 0
        else:
            if graded_index >= len(config.subtasks):
                return
            subtask_index = config.subtasks[graded_index].index
            graded_index += 1
        for offset, test in enumerate(tests):
            if not isinstance(test, dict) or not isinstance(test.get("input"), str):
                continue
            old_input = test["input"]
            old_output = test.get("output") or old_input.replace(".in", ".out")
            new_input = test_filename(subtask_index, offset, "in")
            new_output = test_filename(subtask_index, offset, "out")
            if old_input != new_input:
                moves.append((config.package_dir / "tests/in" / old_input,
                              config.package_dir / "tests/in" / new_input))
            if old_output != new_output:
                moves.append((config.package_dir / "tests/out" / old_output,
                              config.package_dir / "tests/out" / new_output))
            if sample:
                sample_renames[(old_input, old_output)] = (new_input, new_output)
            test["input"] = new_input
            if sample or "output" in test:
                test["output"] = new_output

    unique_moves: dict[Path, Path] = {}
    for source, target in moves:
        previous = unique_moves.setdefault(source, target)
        if previous != target:
            raise RuntimeError(f"File {source.name} is assigned to multiple target names.")
    actual_moves = [(source, target) for source, target in unique_moves.items() if source.is_file()]
    sources = {source.resolve() for source, _ in actual_moves}
    for source, target in actual_moves:
        if target.exists() and target.resolve() not in sources:
            raise RuntimeError(
                f"Cannot rename {source.name} to {target.name}: the target file already exists."
            )
    staged: list[tuple[Path, Path, Path]] = []
    try:
        for index, (source, target) in enumerate(actual_moves):
            temporary = source.with_name(f".__packer-rename-{index}-{source.name}")
            source.replace(temporary)
            staged.append((source, temporary, target))
        for _, temporary, target in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.replace(target)
    except BaseException:
        # First restore target names to temporary files, then restore source files.
        for _, temporary, target in reversed(staged):
            if target.exists() and not temporary.exists():
                target.replace(temporary)
        for source, temporary, _ in reversed(staged):
            if temporary.exists() and not source.exists():
                temporary.replace(source)
        raise

    statement = config.package_dir / "description" / f"{config.language_code}.md"
    if statement.is_file() and sample_renames:
        markdown = statement.read_text(encoding="utf-8")
        for (old_input, old_output), (new_input, new_output) in sample_renames.items():
            markdown = markdown.replace(f'input_file="{old_input}"', f'input_file="{new_input}"')
            markdown = markdown.replace(f'output_file="{old_output}"', f'output_file="{new_output}"')
        atomic_write_text(statement, markdown)
    if sample_renames:
        config.sample_files = [
            {"input": new_input, "output": new_output}
            for new_input, new_output in sample_renames.values()
        ]


def build_config(config: ProjectConfig) -> dict[str, object]:
    """Build the manifest while preserving unknown fields from an imported package."""

    extension = source_extension(config.solution_language)
    generator_extension = source_extension(config.generator_language)
    manifest_path = config.package_dir / "config.json"
    manifest = load_manifest(config.package_dir)
    _migrate_generated_test_names(config, manifest)

    sample_inputs = sorted(
        path.name
        for path in (config.package_dir / "tests" / "in").glob("*")
        if path.is_file() and (
            re.fullmatch(r"0[a-z]\.in", path.name)
            or path.name.startswith(f"{config.codename}.00")
            or re.fullmatch(re.escape(config.codename) + r"0[a-z]+\.in", path.name)
        )
    )

    if config.existing_tests and isinstance(manifest.get("test_groups"), list):
        test_groups = manifest["test_groups"]
        bind_subtasks(config, test_groups)
    else:
        test_groups: list[dict[str, object]] = []
        if sample_inputs:
            test_groups.append(
                {
                    "name": "00",
                    "is_sample": True,
                    "score": 0,
                    "tests": [{"input": name, "output": name[:-3] + ".out" if name.endswith(".in") else name} for name in sample_inputs],
                }
            )
        plan = _test_plan_for(config)
        for subtask in config.subtasks:
            names = _test_names(config, subtask.index, plan.get(subtask.index, {}).get("generator_runs"))
            test_groups.append(
                {
                    "name": f"{subtask.index:02d}",
                    "score": subtask.points,
                    "tests": [{"input": name} for name in names],
                }
            )

        if config.existing_tests:
            assigned = {test['input'] for group in test_groups for test in group['tests']}
            extra = sorted(p.name for p in (config.package_dir / 'tests/in').iterdir()
                           if p.is_file() and p.name not in assigned)
            # Unlabelled raw files are assigned to the full subtask, never dropped.
            for name in extra:
                output = name.replace('.in', '.out')
                test = {'input': name}
                if (config.package_dir / 'tests/out' / output).is_file():
                    test['output'] = output
                test_groups[-1]['tests'].append(test)
        for subtask in config.subtasks:
            subtask.group_name = f'{subtask.index:02d}'

    test_generations: list[dict[str, str]] = []
    profiles = ("small", "random", "max")
    for subtask in config.subtasks:
        for offset, profile in enumerate(profiles):
            test_generations.append(
                {
                    "generator": f"generator_{subtask.index:02d}.{generator_extension}",
                    "parameters": f"{10_000 + subtask.index * 10 + offset} {profile}",
                    "filename": test_filename(subtask.index, offset, "in"),
                }
            )

    generated_solutions: list[dict[str, str]] = []
    for subtask in config.subtasks:
        if str(subtask.index) in config.solution_files:
            continue
        generated_solutions.append(
            {
                "name": f"solution_{subtask.index:02d}.{extension}",
                "type": "model" if subtask.index == config.subtasks[-1].index else "wa",
                "prog_lang": solve_language(config.solution_language),
            }
        )
    existing_solutions = manifest.get("solutions")
    solutions: list[object] = (
        list(existing_solutions)
        if config.input_package and isinstance(existing_solutions, list)
        else []
    )
    generated_names = {item["name"] for item in generated_solutions}
    solutions = [item for item in solutions if not isinstance(item, dict) or item.get("name") not in generated_names]
    for item in solutions:
        if isinstance(item, dict):
            if item.get("type") == "model":
                item["type"] = "ac"
            elif isinstance(item.get("type"), list) and "model" in item["type"]:
                item["type"] = ["ac" if value == "model" else value for value in item["type"]]
    solutions.extend(generated_solutions)

    manifest.update(
        {
            "title": {**manifest.get('title', {}), config.language_code: config.title},
            "origin": config.origin,
            "type": "solve-interactive" if config.task_type == "interactive" else "solve",
            "default_language": config.language_code,
            "number_of_executions": manifest.get("number_of_executions", 1),
            "limits": {"time": config.time_limit_ms, "memory": config.memory_limit_kb},
            "descriptions": {**manifest.get('descriptions', {}), config.language_code: f"{config.language_code}.md"},
            # English is the standard editorial language for generated packages.
            "editorials": {**manifest.get('editorials', {}), "en": "en.md"},
            "solutions": solutions,
            "test_groups": test_groups,
        }
    )

    checker_sources = sorted(
        path.name
        for path in (config.package_dir / "checker").glob("*.cpp")
        if path.name != "printing_check.cpp"
    )
    if config.input_package and manifest.get('checker') and (config.package_dir / 'checker' / manifest['checker']['name']).is_file():
        pass  # The manifest, not alphabetical order, selects the imported judge.
    elif config.task_type == "standard":
        previous = manifest.get("checker", {})
        name = previous.get("name", "printing_check.cpp") if previous.get("standard") else "printing_check.cpp"
        manifest["checker"] = {"name": name, "standard": True, "prog_lang": "cpp17",
                               "limits": {"time": 10000, "memory": 524288}}
    else:
        default_name = (
            "interactor.cpp" if config.task_type == "interactive" else "checker.cpp"
        )
        manifest["checker"] = {
            "additional_files_names": manifest.get("checker", {}).get("additional_files_names", []),
            "prog_lang": "cpp17",
            "name": default_name if default_name in checker_sources else (checker_sources[0] if checker_sources else default_name),
            "standard": False,
            "limits": {"time": 10_000, "memory": 262_144},
        }

    # Older packer versions declared a header they never provided.
    extra = manifest["checker"].get("additional_files_names", [])
    if extra == ["solve.hpp"] and not (config.package_dir / "checker/solve.hpp").exists():
        source = config.package_dir / "checker" / manifest["checker"]["name"]
        if not source.exists() or "solve.hpp" not in source.read_text():
            manifest["checker"]["additional_files_names"] = []

    if not config.existing_tests:
        plan = _test_plan_for(config)
        manifest["generators"] = [
            {"name": f"generator_{item.index:02d}.{generator_extension}",
             "prog_lang": solve_language(config.generator_language),
             "limits": {"time": 10000, "memory": 524288}}
            for item in config.subtasks
        ]
        if plan:
            test_generations = []
            for subtask in config.subtasks:
                count = plan.get(subtask.index, {}).get("generator_runs", 3)
                for offset in range(count):
                    profile = ("small", "random", "max")[offset % 3]
                    test_generations.append(
                        {
                            "generator": f"generator_{subtask.index:02d}.{generator_extension}",
                            "parameters": f"{10_000 + subtask.index * 100 + offset} {profile}",
                            "filename": test_filename(subtask.index, offset, "in"),
                        }
                    )
        manifest["test_generation"] = test_generations
    if "test_generation" not in manifest:
        manifest["test_generation"] = manifest.get("test_generations", [])
    manifest.pop("test_generations", None)
    manifest.setdefault("generators", [])
    for group in manifest["test_groups"]:
        if "sample" in group:
            group.setdefault("is_sample", group.pop("sample"))
    for item in generated_solutions:
        source = config.package_dir / "solutions" / item["name"]
        if source.is_file() and '"solve_dlazaw.hpp"' in source.read_text():
            item["additional_files_names"] = ["solve_dlazaw.hpp"]
    # Restore a reused model after normalizing the other imported solutions.
    model_name = config.solution_files.get(str(config.subtasks[-1].index))
    if model_name:
        for item in manifest['solutions']:
            if item.get('name') == model_name:
                item['type'] = 'model'
    records(manifest)  # Reject duplicate assignments before writing the manifest.
    return manifest


def write_package_metadata(
    config: ProjectConfig, usage: dict[str, float | int] | None = None
) -> None:
    """Refresh the manifest, package documentation, and helper CLI commands."""

    root = config.package_dir
    manifest = build_config(config)
    from .solve_native import install_standard_checker
    if manifest["checker"]["standard"]:
        install_standard_checker(manifest["checker"]["name"], root / "checker")
    header = root / "public/solve_dlazaw.hpp"
    if header.is_file():
        shutil.copy2(header, root / "solutions/solve_dlazaw.hpp")
    write_json(root / "config.json", manifest)

    subtask_rows = "\n".join(
        f"| {item.index} | {item.name} | {item.points} | {item.constraints} |"
        for item in config.subtasks
    )
    cost = float((usage or {}).get("cost_usd", 0.0))
    planned_total = _planned_total_tests(config)
    plan_line = (
        f"Test plan: {planned_total} tests, including {len(config.test_plan.get('tests', [])) if isinstance(config.test_plan, dict) else 0} public samples."
        if planned_total is not None
        else ""
    )
    type_label = {
        "standard": "standard",
        "multiple": "multiple correct answers",
        "interactive": "interactive",
    }.get(config.task_type, config.task_type)
    package_readme = f"""# {config.title}

Task code: `{config.codename}`
Source: {config.origin}
Type: {type_label}
API cost recorded during packaging: `${cost:.4f}`
{plan_line}

## Contents

- `description/{config.language_code}.md` -- statement in Solve 4 format,
- `editorial/` -- editorial,
- `solutions/` -- partial and reference solutions,
- `generators/` -- deterministic test generators,
- `tests/in/` and `tests/out/` -- imported or generated tests,
- `public/` -- additional files for contestants,
- `config.json` -- package manifest.

## Subtasks

| No. | Name | Points | Constraints |
|---:|---|---:|---|
{subtask_rows}

## Automated verification results

See `verification/report.md` and `verification/report.json`. PASS means
executed checks, SKIP means omitted scope, and FAIL means a detected error.

## Validation and upload

```bash
solve task -c {shlex.quote(config.codename)} -p "$(pwd)" validate
solve task -c {shlex.quote(config.codename)} -p "$(pwd)" build
solve task -c {shlex.quote(config.codename)} -p "$(pwd)" upload --stop-work
```

Before publication, manually review the statement, limits, source, partial-solution
results, and any skipped checks. Test a checker or interactor against valid, invalid,
and maliciously formatted answers. No checksums are added for new generations.
"""
    if config.input_package and (root / 'readme.md').is_file():
        atomic_write_text(root / 'packer-readme.md', package_readme)
    else:
        atomic_write_text(root / "readme.md", package_readme)

    commands = f"""#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")"
PYTHONPATH="$PWD/packer_tools" python3 -m task_packer.portable "$PWD" "$@"
"""
    portable_dir = root / 'packer_tools/task_packer'
    portable_dir.mkdir(parents=True, exist_ok=True)
    for name in ('__init__.py', 'sandbox.py', 'portable.py'):
        shutil.copy2(Path(__file__).parent / name, portable_dir / name)
    atomic_write_text(root / "check_with_solve.sh", commands)
    (root / "check_with_solve.sh").chmod(0o755)


def local_validation(config: ProjectConfig) -> list[str]:
    """Return problems detectable without a Solve CLI installation."""

    root = config.package_dir
    required = [
        root / "config.json",
        root / "description" / f"{config.language_code}.md",
        root / "editorial" / "en.md",
        root / "solutions",
        root / "tests" / "in",
        root / "checker",
    ]
    errors = [f"Missing: {path}" for path in required if not path.exists()]
    if sum(item.points for item in config.subtasks) != 100:
        errors.append("Subtask scores do not add up to 100.")
    statement_path = root / "description" / f"{config.language_code}.md"
    if statement_path.is_file():
        markdown = statement_path.read_text(encoding="utf-8")
        errors.extend(
            statement_validation_errors(
                markdown, config.task_type, config.language_code
            )
        )
        if config.task_type != "interactive":
            for input_name, output_name, _ in example_references(markdown):
                for folder, name in (("in", input_name), ("out", output_name)):
                    path = root / "tests" / folder / name
                    if not name or Path(name).name != name or not path.is_file():
                        errors.append(f"Missing example file tests/{folder}/{name}.")
                    elif path.stat().st_size > EXAMPLE_FILE_MAX_BYTES:
                        errors.append(f"Example tests/{folder}/{name} exceeds 2048 bytes; the CLI will omit it when compiling the statement.")
    if not any((root / "tests" / "in").glob("*")):
        errors.append("No input files in tests/in.")
    checker_sources = [
        path
        for path in (root / "checker").glob("*.cpp")
        if path.name != "printing_check.cpp"
    ]
    if config.task_type != "standard" and not checker_sources:
        errors.append("A custom checker or interactor is missing from checker/.")
    if config.task_type == "interactive":
        for required_public in ("solve_dlazaw.hpp", "interactor_local.cpp"):
            if not (root / "public" / required_public).is_file():
                errors.append(
                    f"Missing public/{required_public} for the interactive task."
                )
        if checker_sources:
            code = checker_sources[0].read_text(encoding="utf-8", errors="replace")
            if "flush" not in code:
                errors.append("The interactor does not explicitly flush responses.")
    try:
        manifest = json.loads((root / "config.json").read_text(encoding="utf-8"))
        registry = records(manifest)
        if statement_path.is_file() and config.task_type != 'interactive':
            registered = {(test.input, test.output) for test in registry}
            for inp, out, _ in example_references(markdown):
                if (inp, out) not in registered:
                    errors.append(f'Example {inp}/{out} is not registered in the manifest.')
        assigned = {test.input for test in registry}
        for path in (root / 'tests/in').glob('*'):
            if path.is_file() and path.name not in assigned:
                errors.append(f'Test is not assigned to a group: {path.name}.')
        if not manifest.get("test_groups"):
            errors.append("config.json does not contain test groups.")
        if not config.existing_tests and config.test_plan:
            total_tests = _planned_total_tests(config)
            if total_tests is not None and not 20 <= total_tests <= 100:
                errors.append("The planned number of tests is not within 20--100.")
    except (OSError, json.JSONDecodeError):
        errors.append("config.json is not valid JSON.")
    return errors


def solve_validation(config: ProjectConfig) -> tuple[bool | None, str]:
    """Run native validation when the ``solve`` command is available."""

    from .solve_native import native_validate
    return native_validate(config)
