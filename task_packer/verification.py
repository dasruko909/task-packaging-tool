"""Executable checks and an explicit, persistent verification report."""
from __future__ import annotations

import json
import hashlib
import re
import tempfile
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from .execution import ExecutionError, Runner
from .costs import MAX_PROJECT_COST_USD, MODEL_PRICES
from .models import ProjectConfig
from .storage import atomic_write_text, read_text_exact, write_json
from .registry import records, solution_path, safe_file, load_manifest, programs
from .paths import checked_tree, checked_path
from .freshness import fingerprints, exported_path


def verification_program(directory: Path, stem: str) -> Path:
    """Use C++ artifacts created by current versions, with legacy Python support."""
    for suffix in ('.cpp', '.py'):
        path = directory / f'{stem}{suffix}'
        if path.is_file():
            return path
    return directory / f'{stem}.cpp'


def usable_reducer(path: Path) -> bool:
    """A C++ reducer is optional but, when present, must be executable."""
    if not path.is_file():
        return False
    if path.suffix != '.cpp':
        return True
    try:
        return bool(re.search(r'\b(?:int|signed)\s+main\s*\(', path.read_text(encoding='utf-8')))
    except OSError:
        return False


def score(text: str) -> int:
    lines = text.strip().splitlines()
    if len(lines) != 2 or not lines[0].strip().isdigit() or not lines[1].strip():
        raise ExecutionError("Invalid judge-result format: " + repr(text[:200]))
    value = int(lines[0])
    if not 0 <= value <= 100:
        raise ExecutionError("Score is outside the 0–100 range.")
    return value


class Verification:
    def __init__(self, config: ProjectConfig, regenerate_outputs: bool = False,
                 usage: dict[str, float | int] | None = None):
        self.config = config
        self.root = checked_tree(config.package_dir).resolve()
        self.checks: list[dict[str, str]] = []
        self.regenerate_outputs = regenerate_outputs
        self.usage = dict(usage or {})
        self.input_overrides: dict[str, str] = {}
        self.verified_fingerprints: dict[str, str] | None = None

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        checked_path(path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return default

    @staticmethod
    def _markdown(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", "<br>")

    def load_existing(self) -> "Verification":
        """Load old checks while rebuilding the derived, detailed sections."""
        data = self._read_json(self.root / "verification/report.json", {})
        checks = data.get("checks", []) if isinstance(data, dict) else []
        if isinstance(checks, list):
            self.checks = [row for row in checks if isinstance(row, dict)]
        return self

    def _package_details(self) -> dict[str, Any]:
        manifest = self._read_json(self.root / "config.json", {})
        groups = manifest.get("test_groups", []) if isinstance(manifest, dict) else []
        generated_names: set[str] = set()
        if isinstance(manifest, dict):
            for row in manifest.get("test_generation", []):
                if not isinstance(row, dict):
                    continue
                names = row.get("filename", [])
                generated_names.update(
                    name for name in (names if isinstance(names, list) else [names])
                    if isinstance(name, str) and name
                )
        group_rows = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            tests = [row for row in group.get("tests", []) if isinstance(row, dict)]
            generated = sum(row.get("input") in generated_names for row in tests)
            group_rows.append({
                "name": str(group.get("name", "?")),
                "sample": bool(group.get("is_sample")),
                "points": int(group.get("score", 0) or 0),
                "tests": len(tests),
                "generated": generated,
                "fixed": len(tests) - generated,
            })
        return {
            "codename": self.config.codename,
            "title": self.config.title,
            "task_type": self.config.task_type,
            "time_limit_ms": self.config.time_limit_ms,
            "memory_limit_kb": self.config.memory_limit_kb,
            "groups": group_rows,
            "group_count": len(group_rows),
            "test_count": sum(row["tests"] for row in group_rows),
            "sample_count": sum(row["tests"] for row in group_rows if row["sample"]),
            "scored_test_count": sum(row["tests"] for row in group_rows if not row["sample"]),
            "generated_test_count": sum(row["generated"] for row in group_rows),
            "fixed_test_count": sum(row["fixed"] for row in group_rows),
            "executions_per_test": int(manifest.get("number_of_executions", 1) or 1)
            if isinstance(manifest, dict) else 1,
        }

    def _api_history(self) -> dict[str, Any]:
        history = Path(".packer-projects") / self.config.codename / "history/api-calls"
        calls = []
        for path in sorted(history.glob("*.txt")) if history.is_dir() else []:
            event = self._read_json(path, {})
            if isinstance(event, dict):
                calls.append(event)
        models = sorted({str(row["model"]) for row in calls if row.get("model")})
        completed = sum(row.get("status") == "completed" for row in calls)
        failed = sum(row.get("status") in {"failed", "empty"} for row in calls)
        return {"recorded_calls": len(calls), "completed_calls": completed,
                "failed_calls": failed, "models": models}

    def _ai_details(self) -> dict[str, Any]:
        usage = dict(self.usage)
        if not usage:
            state = self._read_json(
                Path(".packer-projects") / self.config.codename / "state.json", {})
            if isinstance(state, dict) and isinstance(state.get("usage"), dict):
                usage = dict(state["usage"])
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        cached = int(usage.get("cached_input_tokens", 0) or 0)
        history = self._api_history()
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        cost = float(usage.get("cost_usd", 0.0) or 0.0)
        result = {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "uncached_input_tokens": max(0, input_tokens - cached),
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": cost,
            "project_budget_usd": MAX_PROJECT_COST_USD,
            "budget_used_percent": round(100 * cost / MAX_PROJECT_COST_USD, 2),
            **history,
        }
        if len(history["models"]) == 1 and history["models"][0] in MODEL_PRICES:
            model = history["models"][0]
            prices = MODEL_PRICES[model]
            result["pricing"] = {
                "model": model,
                "usd_per_million_tokens": prices,
                "uncached_input_cost_usd": round(max(0, input_tokens - cached) * prices["input"] / 1_000_000, 8),
                "cached_input_cost_usd": round(cached * prices["cached_input"] / 1_000_000, 8),
                "output_cost_usd": round(output_tokens * prices["output"] / 1_000_000, 8),
            }
        else:
            result["pricing"] = None
        return result

    def _test_model_details(self) -> dict[str, Any]:
        coverage = self._read_json(self.root / "verification/coverage.json", [])
        if not isinstance(coverage, list):
            coverage = []
        rows = [row for row in coverage if isinstance(row, dict)]
        killed = sum(bool(row.get("killed_by")) for row in rows)
        survivors = [str(row.get("mutant", "?")) for row in rows if not row.get("killed_by")]
        total = len(rows)
        if not total:
            rating = "no data"
            assessment = "No mutation tests were run, so resistance to common incorrect strategies was not assessed."
        elif survivors:
            rating = "insufficient"
            assessment = (f"The suite allows {len(survivors)} of {total} declared bug models; "
                          "it should not be considered resistant to heuristic solutions.")
        elif total < 4:
            rating = "limited"
            assessment = (f"All {total} bug models were rejected, but the sample is small. "
                          "It confirms only listed mistakes, not broad resistance to heuristics.")
        elif total < 8:
            rating = "moderate"
            assessment = (f"All {total} bug models were rejected. Coverage is useful, "
                          "but is not a broad model of heuristic solutions.")
        else:
            rating = "broad"
            assessment = (f"All {total} diverse bug models were rejected. "
                          "This is a strong mutation result, still without formal proof of test completeness.")
        return {
            "rating": rating,
            "assessment": assessment,
            "mutants_total": total,
            "mutants_killed": killed,
            "kill_rate_percent": round(100 * killed / total, 1) if total else None,
            "distinct_killing_tests": len({row.get("killed_by") for row in rows if row.get("killed_by")}),
            "survivors": survivors,
            "coverage": rows,
        }

    def _solve_details(self) -> dict[str, Any]:
        native = self._read_json(self.root / "verification/solve-check.json", {})
        if not isinstance(native, dict) or not native:
            return {"available": False}
        tests = [row for row in native.get("tests", []) if isinstance(row, dict)]
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in tests:
            grouped[(str(row.get("solution", "?")), str(row.get("group", "?")))].append(row)
        groups = []
        for (solution, group), rows in sorted(grouped.items()):
            scores = [float(row.get("score", 0) or 0) for row in rows]
            groups.append({
                "solution": solution,
                "group": group,
                "runs": len(rows),
                "accepted": sum(score == 100 for score in scores),
                "min_score": min(scores, default=0),
                "max_time_ms": max((int(row.get("time_ms", 0) or 0) for row in rows), default=0),
                "max_memory_kb": max((int(row.get("memory_kb", 0) or 0) for row in rows), default=0),
            })
        detailed_tests = tests
        truncated = False
        if len(tests) > 100:
            failures = [row for row in tests if float(row.get("score", 0) or 0) < 100]
            slowest = sorted(tests, key=lambda row: int(row.get("time_ms", 0) or 0), reverse=True)[:20]
            detailed_tests = list({(row.get("solution"), row.get("input")): row
                                   for row in failures + slowest}.values())
            truncated = True
        declared = [row for row in native.get("solutions", []) if isinstance(row, dict)]
        declared_by_name = {str(row.get("name", "?")): row for row in declared}
        solution_names = sorted(set(declared_by_name) | {str(row.get("solution", "?")) for row in tests})
        solution_results = []
        for name in solution_names:
            rows = [row for row in tests if str(row.get("solution", "?")) == name]
            info = declared_by_name.get(name, {})
            types = info.get("types", info.get("type", []))
            if isinstance(types, str):
                types = [types]
            solution_results.append({
                "name": name,
                "types": types if isinstance(types, list) else [],
                "score": float(info.get("score", 0) or 0),
                "runs": len(rows),
                "accepted": sum(float(row.get("score", 0) or 0) == 100 for row in rows),
                "max_time_ms": max((int(row.get("time_ms", 0) or 0) for row in rows), default=0),
                "total_time_ms": sum(int(row.get("time_ms", 0) or 0) for row in rows),
                "max_memory_kb": max((int(row.get("memory_kb", 0) or 0) for row in rows), default=0),
            })
        return {
            "available": True,
            "ok": native.get("ok"),
            "validated": native.get("validated"),
            "built": native.get("built"),
            "skipped": native.get("skipped"),
            "versions": native.get("versions", {}),
            "execution_limits": native.get("execution_limits", {}),
            "solutions": declared,
            "solution_results": solution_results,
            "run_count": len(tests),
            "accepted_runs": sum(float(row.get("score", 0) or 0) == 100 for row in tests),
            "failed_runs": sum(float(row.get("score", 0) or 0) < 100 for row in tests),
            "max_time_ms": max((int(row.get("time_ms", 0) or 0) for row in tests), default=0),
            "total_time_ms": sum(int(row.get("time_ms", 0) or 0) for row in tests),
            "max_memory_kb": max((int(row.get("memory_kb", 0) or 0) for row in tests), default=0),
            "groups": groups,
            "tests": detailed_tests,
            "tests_truncated": truncated,
        }

    def _save_compact_markdown(self, package: dict, ai: dict, test_model: dict,
                               solve: dict, timing: dict, statuses: dict,
                               overall: str) -> None:
        observed = (
            f"max {timing['observed_max_test_ms']} ms/test, suma {timing['observed_total_all_runs_ms']} ms"
            if timing["observed_max_test_ms"] is not None else "no Solve measurement"
        )
        estimated_seconds = timing["estimated_max_per_solution_ms"] / 1000
        local_limit = timing.get("local_per_test_ms")
        local_suffix = f", local {local_limit} ms/test" if local_limit else ""
        local_estimate = timing.get("estimated_local_max_per_solution_ms")
        estimate_suffix = (
            f" (local {local_estimate / 1000:g} s)" if local_estimate else ""
        )
        models = ", ".join(ai["models"]) or "no data"
        solve_result = (
            f"{solve['accepted_runs']}/{solve['run_count']} runs with score 100"
            if solve.get("available") and solve.get("run_count") else "no completed runs"
        )
        groups = ", ".join(f"{row['name']}:{row['tests']}" for row in package["groups"]) or "none"
        mutation = (
            f"{test_model['mutants_killed']}/{test_model['mutants_total']} rejected; "
            f"rating {test_model['rating']}"
        )
        memory_observed = solve.get("max_memory_kb") if solve.get("available") else None
        lines = [f"# Report `{self._markdown(package['codename'])}`", "",
                 "| Metric | Result |", "|---|---|",
                 f"| Status | **{overall}** ({statuses['PASS']} PASS, {statuses['FAIL']} FAIL, {statuses['SKIP']} SKIP) |",
                 f"| Tests | {package['test_count']} ({package['sample_count']} samples, {package['scored_test_count']} scored); groups {self._markdown(groups)} |",
                 f"| Solve | {solve_result} |",
                 f"| Time | limit {package['time_limit_ms']} ms/test{local_suffix}; {observed}; estimated full-package maximum {estimated_seconds:g} s/solution{estimate_suffix} |",
                 f"| Memory | limit {package['memory_limit_kb']} KiB; measured maximum {str(memory_observed) + ' KiB' if memory_observed is not None else 'none'} |",
                 f"| Incorrect-solution tests | {mutation} |",
                 f"| API | {models}; input {ai['input_tokens']:,} (cached {ai['cached_input_tokens']:,}), output {ai['output_tokens']:,}; **${ai['cost_usd']:.8f}** |", ""]
        if solve.get("solution_results"):
            lines.extend(["## Solutions", "",
                          "| Solution | Points | Score-100 runs | Max time | Total time | Max memory |",
                          "|---|---:|---:|---:|---:|---:|"])
            for row in solve["solution_results"]:
                lines.append(f"| `{self._markdown(row['name'])}` | {row['score']:g}/100 | {row['accepted']}/{row['runs']} | {row['max_time_ms']} ms | {row['total_time_ms']} ms | {row['max_memory_kb']} KiB |")
            lines.append("")
        if test_model["coverage"]:
            lines.extend(["## Mutation tests", "",
                          "| Incorrect solution | Bug | Rejected by |",
                          "|---|---|---|"])
            for row in test_model["coverage"]:
                lines.append(f"| `{self._markdown(row.get('mutant', '?'))}` | {self._markdown(row.get('bug', 'no description'))} | `{self._markdown(row.get('killed_by') or 'SURVIVED')}` |")
            lines.append("")
        problems = ([row for row in self.checks
                     if row.get("status") == "FAIL"
                     and not str(row.get("name", "")).startswith("Attempt ")]
                    if overall == "FAIL" else [])
        if problems:
            lines.extend(["## Errors", ""])
            for row in problems:
                lines.append(f"- {self._markdown(row.get('name', '?'))}: {self._markdown(row.get('detail', ''))}")
            lines.append("")
        lines.extend(["Full data: `report.json`; raw measurements: `solve-check.json`.", ""])
        atomic_write_text(self.root / "verification/report.md", "\n".join(lines))

    def record(self, name: str, status: str, detail: str) -> None:
        self.checks.append({"name": name, "status": status, "detail": detail})
        self.save()

    def save(self) -> None:
        package = self._package_details()
        ai = self._ai_details()
        test_model = self._test_model_details()
        solve = self._solve_details()
        current_checks = [row for row in self.checks
                          if not str(row.get("name", "")).startswith("Proba ")]
        statuses = {name: sum(row.get("status") == name for row in current_checks)
                    for name in ("PASS", "FAIL", "SKIP")}
        final_checks = [row for row in self.checks if row.get("name") == "Final result"]
        if solve.get("available") and solve.get("ok") is False:
            overall = "FAIL"
        elif solve.get("available") and solve.get("ok") is None and solve.get("run_count", 0) == 0:
            overall = "INCOMPLETE"
        elif final_checks and final_checks[-1].get("status") in {"PASS", "FAIL", "SKIP"}:
            overall = str(final_checks[-1]["status"])
        elif solve.get("available") and solve.get("ok") is True:
            overall = "PASS"
        else:
            overall = "FAIL" if statuses["FAIL"] else "PASS" if statuses["PASS"] else "NO DATA"
        timing = {
            "configured_per_test_ms": package["time_limit_ms"],
            "executions_per_test": package["executions_per_test"],
            "estimated_max_per_solution_ms": (
                package["test_count"] * package["executions_per_test"] * package["time_limit_ms"]
            ),
            "observed_max_test_ms": solve.get("max_time_ms") if solve.get("available") else None,
            "observed_total_all_runs_ms": solve.get("total_time_ms") if solve.get("available") else None,
        }
        local_limit = solve.get("execution_limits", {}).get("local_time_ms") if solve.get("available") else None
        timing["local_per_test_ms"] = local_limit
        timing["estimated_local_max_per_solution_ms"] = (
            package["test_count"] * package["executions_per_test"] * int(local_limit)
            if local_limit else None
        )
        payload = {
            "summary": {"status": overall, **{name.lower(): value for name, value in statuses.items()}},
            "package": package,
            "ai_usage": ai,
            "test_model": test_model,
            "solve": solve,
            "timing": timing,
            "checks": self.checks,
            "sha256": fingerprints(self.root),
        }
        write_json(self.root / "verification/report.json", payload)
        self._save_compact_markdown(package, ai, test_model, solve, timing, statuses, overall)
        return

    def check_answer(self, runner: Runner, judge: Path, data: str,
                     reference: str, candidate: str) -> int:
        paths = [runner.directory / name for name in ("input.txt", "reference.txt", "candidate.txt")]
        for path, text in zip(paths, (data, reference, candidate)):
            atomic_write_text(path, text)
        try:
            return score(runner.program(
                judge, args=[str(p) for p in paths], seconds=10, readable=paths
            ))
        except ExecutionError as error:
            raise ExecutionError(f"{error}\nInput: {data[:2000]}\nReference: {reference[:2000]}\nCandidate: {candidate[:2000]}", judge) from error

    def run(self) -> None:
        recover_tests(self.root)
        before = fingerprints(self.root)
        self.verified_fingerprints = None
        with tempfile.TemporaryDirectory(prefix=f'.{self.root.name}-tests-',
                                         dir=self.root.parent) as temporary:
            tests = Path(temporary) / 'tests'
            if (self.root / 'tests').exists():
                shutil.copytree(self.root / 'tests', tests)
            else:
                tests.mkdir()
            for name, data in self.input_overrides.items():
                atomic_write_text(safe_file(tests / 'in', name), data)
            self._run(tests)
            if fingerprints(self.root) != before:
                raise ExecutionError('Materials changed during verification; rerun checks.')
            expected = {name: digest for name, digest in before.items()
                        if not name.startswith('tests/')}
            expected.update({'tests/' + str(path.relative_to(tests)):
                             hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in tests.rglob('*') if path.is_file()
                             and exported_path(Path('tests') / path.relative_to(tests))})
            publish_tests(self.root, tests, expected)
            self.verified_fingerprints = expected

    def _run(self, tests: Path) -> None:
        config = self.config
        bundle = self.root / "verification"
        with tempfile.TemporaryDirectory(prefix="packer-check-") as temporary:
            runner = Runner(Path(temporary), self.root / "public")
            manifest = load_manifest(self.root)
            registry = records(manifest)
            validator = verification_program(self.root / 'validators', 'input_validator')
            if not validator.is_file():
                raise ExecutionError('Independent input validator is missing. Prepare verification materials.')
            registered = programs(self.root, manifest)
            registered_sources = {program.source.resolve() for program in registered}
            compile_plans: dict[Path, dict[str, Any]] = {}

            def add_program(source: Path, *, dependencies=(), root=None,
                            language=None, label=None) -> None:
                source = source.resolve()
                plan = compile_plans.setdefault(source, {
                    'dependencies': [], 'root': (root or source.parent).resolve(),
                    'language': language, 'label': label or source.name,
                })
                if language and plan['language'] and language != plan['language']:
                    raise ExecutionError(
                        f"Conflicting languages registered for {source.name}: "
                        f"{plan['language']} and {language}.", source
                    )
                plan['language'] = plan['language'] or language
                for dependency in dependencies:
                    dependency = dependency.resolve()
                    if dependency not in plan['dependencies']:
                        plan['dependencies'].append(dependency)

            for program in registered:
                add_program(
                    program.source, dependencies=program.dependencies, root=program.root,
                    language=program.language,
                    label=f"registered {program.role} {program.name!r}",
                )
            add_program(validator, label='verification input validator')

            cases_path = bundle / 'cases.json'
            cases = json.loads(cases_path.read_text()) if cases_path.is_file() else []
            if config.task_type == 'interactive':
                add_program(self.root / 'public/interactor_local.cpp',
                            label='interactive local tester')
                for case in cases:
                    if isinstance(case, dict) and isinstance(case.get('client'), str):
                        add_program(safe_file(bundle, case['client']),
                                    label=f"verification client {case['client']!r}")
            else:
                for stem in ('small_generator', 'brute'):
                    source = verification_program(bundle, stem)
                    add_program(source, label=f'verification {stem.replace("_", " ")}')
                reducer = verification_program(bundle, 'reducer')
                if usable_reducer(reducer):
                    add_program(reducer, label='verification reducer')
                for source in sorted(bundle.glob('author_brute_*.*')):
                    if source.suffix in {'.cpp', '.py'}:
                        add_program(source, label=f"author brute force {source.name!r}")
                mutants_path = bundle / 'mutants.json'
                mutants = json.loads(mutants_path.read_text()) if mutants_path.is_file() else []
                for mutant in mutants:
                    if isinstance(mutant, dict) and isinstance(mutant.get('name'), str):
                        add_program(safe_file(bundle / 'mutants', mutant['name']),
                                    label=f"verification mutant {mutant['name']!r}")

            # Compile every selected entry point exactly once. Helpers and data
            # are staged as dependencies and are never treated as programs.
            for source, plan in compile_plans.items():
                runner.compile(source, **plan)
            def validate_input(data, subtask=0, source=None):
                try:
                    runner.program(validator, data, args=[str(subtask)], seconds=10)
                except ExecutionError as error:
                    raise ExecutionError(
                        f'Invalid data for subtask {subtask}: {error}', source
                    ) from error
            self.record("Compilation", "PASS", f"Checked {len(compile_plans)} programs.")
            for generation in manifest.get("test_generation", []) if not config.existing_tests else []:
                source = safe_file(self.root / "generators", generation["generator"])
                if source.resolve() not in registered_sources:
                    raise ExecutionError(
                        f"Generator {generation['generator']!r} is used by test_generation "
                        "but is not registered in manifest.generators.", source
                    )
                data = runner.program(source, args=generation["parameters"].split(), seconds=10)
                atomic_write_text(safe_file(tests / "in", generation["filename"]), data)
                again = runner.program(source, args=generation['parameters'].split(), seconds=10)
                if data != again:
                    raise ExecutionError('Generator is not deterministic for the same parameters.', source)
            self.record("Generators", "PASS" if not config.existing_tests else "SKIP",
                        "Ran small, random, and max profiles." if not config.existing_tests
                        else "Used existing tests; imported-package generators were not run.")
            judge = safe_file(self.root / "checker", manifest["checker"]["name"])
            solutions = [(item, solution_path(config, item).resolve())
                         for item in config.subtasks]
            for _, solution in solutions:
                if solution not in registered_sources:
                    raise ExecutionError(
                        f"Solution {solution.name!r} is selected for verification but is not "
                        "registered in manifest.solutions.", solution
                    )
            full = solutions[-1][1]
            inputs = [safe_file(tests / 'in', test.input) for test in registry]
            output_names = {test.input: test.output for test in registry}
            for test, path in zip(registry, inputs):
                subtask = next((item.index for item in config.subtasks if (item.group_name or f'{item.index:02d}') == test.group), 0)
                source = (
                    self.root / "generators" / test.generation["generator"]
                    if test.generation
                    else safe_file(self.root / "tests/in", test.input)
                )
                validate_input(read_text_exact(path), subtask, source)
            self.record('Input validation', 'PASS', f'Checked format and constraints of {len(inputs)} tests, including group conditions.')
            if not inputs:
                raise ExecutionError("No tests to run.")
            seconds = max(.1, config.time_limit_ms / 1000)
            if config.task_type == "interactive":
                for input_path in inputs:
                    verdict, timeout = runner.interaction(judge, full, input_path,
                                                         seconds=seconds + 2, memory_kb=config.memory_limit_kb)
                    if timeout or score(verdict) != 100:
                        raise ExecutionError(f"Interaction failed: {input_path.name}; {verdict}")
                self.record("Interactive solution", "PASS", f"Completed conversation on {len(inputs)} tests.")
                for item, solution in solutions[:-1]:
                    group = next((g for g in manifest.get("test_groups", [])
                                  if g.get("name") == (item.group_name or f"{item.index:02d}")), None)
                    names = {test["input"] for test in group.get("tests", [])} if group else set()
                    assigned = [path for path in inputs if path.name in names]
                    for path in assigned:
                        verdict, timeout = runner.interaction(judge, solution, path,
                                                             seconds=seconds + 2, memory_kb=config.memory_limit_kb)
                        if timeout or score(verdict) != 100:
                            raise ExecutionError(f"Partial solution {solution.name}: interaction failed on {path.name}.")
                    self.record(f"Subtask {item.index}", "PASS" if assigned else "SKIP",
                                f"Ran {len(assigned)} conversations in the subtask group.")
                for case in cases:
                    validate_input(case['input'])
                    test = runner.directory / "interaction-input.txt"
                    test.write_text(case["input"])
                    verdict, timeout = runner.interaction(judge, safe_file(bundle, case["client"]), test,
                                                         seconds=seconds + 2, memory_kb=config.memory_limit_kb)
                    # A silent client is stopped by the external supervisor, just as by a judge runner.
                    if case["kind"] == "silent" and timeout:
                        if verdict and score(verdict) != case["score"]:
                            raise ExecutionError("Interactor awarded an invalid score to a silent client.", judge)
                        continue
                    if timeout or score(verdict) != case["score"]:
                        raise ExecutionError(f"Interactor test {case['kind']} failed: {verdict}. Case: {json.dumps(case, ensure_ascii=False)}", judge)
                self.record("Interactor robustness", "PASS", "Checked malformed and truncated messages, query limit, and silence (external time limit).")
                self.record("Local tester", "SKIP", "Compiled interactor_local.cpp; conversations ran with the actual interactor.")
                self.record("Brute force", "SKIP", "For the interactive protocol, conversations were run instead of comparing output files.")
                return

            if config.task_type == "multiple":
                for case in cases:
                    validate_input(case['input'])
                    actual = self.check_answer(runner, judge, case["input"], case["reference"], case["candidate"])
                    if actual != case["score"]:
                        raise ExecutionError(f"Checker: {case['kind']}; expected {case['score']}, got {actual}. Case: {json.dumps(case, ensure_ascii=False)}", judge)
                self.record("Checker robustness", "PASS", f"Checked {len(cases)} cases with explicit expected scores.")
            else:
                self.record("Custom checker", "SKIP", "The original standard libsolve checker compares results.")


            pending_outputs = {}
            for input_path in inputs:
                data = read_text_exact(input_path)
                answer = runner.program(full, data, seconds=seconds, memory_kb=config.memory_limit_kb)
                expected_path = tests / "out" / output_names.get(input_path.name, input_path.name.replace(".in", ".out"))
                generated = any(test['input'] == input_path.name and not test.get('output') for group in manifest['test_groups'] for test in group['tests'])
                if expected_path.exists() and not ((self.regenerate_outputs and generated and not config.existing_tests)
                                                      or input_path.name in self.input_overrides):
                    expected = read_text_exact(expected_path)
                    valid = (self.check_answer(runner, judge, data, expected, answer) == 100)
                    if not valid:
                        raise ExecutionError(f"Result does not match test {input_path.name}.\nInput: {data[:2000]}\nExpected: {expected[:2000]}\nResult: {answer[:2000]}", full)
                else:
                    pending_outputs[expected_path] = answer
            self.record("Reference solution", "PASS", f"Ran {len(inputs)} tests and compared existing answers. Missing answers will be saved after all checks.")

            for item, solution in solutions[:-1]:
                group = next((g for g in manifest.get("test_groups", [])
                              if g.get("name") == (item.group_name or f"{item.index:02d}")), None)
                names = {test["input"] for test in group.get("tests", [])} if group else set()
                assigned = [path for path in inputs if path.name in names]
                for path in assigned:
                    data = read_text_exact(path)
                    expected_path = tests / "out" / output_names.get(path.name, path.name.replace(".in", ".out"))
                    expected = pending_outputs[expected_path] if expected_path in pending_outputs else read_text_exact(expected_path)
                    answer = runner.program(solution, data, seconds=seconds, memory_kb=config.memory_limit_kb)
                    valid = (self.check_answer(runner, judge, data, expected, answer) == 100)
                    if not valid:
                        raise ExecutionError(f"Partial solution {solution.name}: invalid result on {path.name}.", solution)
                self.record(f"Subtask {item.index}", "PASS" if assigned else "SKIP",
                            f"Ran {len(assigned)} group tests; small tests are checked separately.")

            count = 0
            for item, solution in solutions:
                for seed in range(20):
                    small_generator = verification_program(bundle, 'small_generator')
                    brute_program = verification_program(bundle, 'brute')
                    data = runner.program(small_generator, args=[str(seed), str(item.index)])
                    validate_input(data, item.index, small_generator)
                    expected = runner.program(brute_program, data, seconds=10)
                    for brute in sorted(bundle.glob('author_brute_*.*')):
                        if brute.suffix not in {'.cpp', '.py'}:
                            continue
                        candidate = runner.program(brute, data, seconds=10)
                        if self.check_answer(runner, judge, data, expected, candidate) != 100:
                            raise ExecutionError(f"Independent brute-force disagreement: {brute.name}; "
                                                 f"input: {data[:2000]}; reference: {expected[:1000]}; "
                                                 f"answer: {candidate[:1000]}")
                    for tested in dict.fromkeys((solution, full)):
                        answer = runner.program(tested, data, seconds=seconds, memory_kb=config.memory_limit_kb)
                        valid = (self.check_answer(runner, judge, data, expected, answer) == 100)
                        if not valid:
                            failure = {"seed": seed, "subtask": item.index, "input": data,
                                       "expected": expected, "actual": answer, "solution": tested.name}
                            write_json(bundle / "counterexample.json", failure)
                            from .counterexamples import minimize
                            def still_fails(candidate):
                                try:
                                    validate_input(candidate, item.index)
                                    reference = runner.program(brute_program, candidate, seconds=3)
                                    actual = runner.program(tested, candidate, seconds=seconds, memory_kb=config.memory_limit_kb)
                                    return self.check_answer(runner, judge, candidate, reference, actual) != 100
                                except ExecutionError:
                                    return False
                            def proposals(original):
                                reducer = verification_program(bundle, 'reducer')
                                if usable_reducer(reducer):
                                    for step in range(20):
                                        try:
                                            yield runner.program(reducer, original, args=[str(step)], seconds=1)
                                        except ExecutionError:
                                            return
                            failure['minimal_input'] = minimize(data, still_fails, proposals)
                            failure['diagnosis'] = 'The data is valid. Determine whether the error is in the solution, brute force, or checker; no automatic reference change was made.'
                            write_json(bundle / 'counterexample.json', failure)
                            raise ExecutionError("Brute-force disagreement: " + json.dumps(failure, ensure_ascii=False)[:6000])
                    count += 1
            self.record("Brute force", "PASS", f"Compared solutions on {count} small tests with recorded fixed seeds.")

            if not mutants_path.is_file():
                raise ExecutionError('No test plan for deliberately incorrect solutions.')
            if len(mutants) < 2:
                raise ExecutionError('At least two realistic incorrect solutions are required.')
            coverage = []
            for mutant in mutants:
                source = safe_file(bundle / 'mutants', mutant['name'])
                runner.compile(source)  # A syntax error is not a successful mutation test.
                killed_by = None
                outcome = 'survived'
                for input_path in inputs:
                    data = read_text_exact(input_path)
                    expected_path = tests / 'out' / output_names[input_path.name]
                    expected = pending_outputs.get(expected_path)
                    if expected is None:
                        expected = read_text_exact(expected_path)
                    try:
                        answer = runner.program(source, data, seconds=seconds, memory_kb=config.memory_limit_kb)
                        killed = self.check_answer(runner, judge, data, expected, answer) != 100
                        if killed:
                            outcome = 'wrong_answer'
                    except ExecutionError:
                        killed = True
                        outcome = 'runtime_error_or_limit'
                    if killed:
                        killed_by = input_path.name
                        break
                coverage.append({
                    'mutant': mutant['name'], 'bug': mutant['description'],
                    'killed_by': killed_by, 'outcome': outcome,
                })
            write_json(bundle / 'coverage.json', coverage)
            survivors = [row['mutant'] for row in coverage if not row['killed_by']]
            if survivors:
                raise ExecutionError('The test suite allows incorrect solutions: ' + ', '.join(survivors) + '. Add tests covering the described bugs; verification/coverage.json.')
            self.record('Test effectiveness', 'PASS', f'The suite rejects {len(mutants)} deliberately incorrect solutions; details in coverage.json.')

            for path, answer in pending_outputs.items():
                if path.is_file():
                    import time
                    atomic_write_text(self.root / 'verification/output-history' / f'{path.name}.{time.time_ns()}', read_text_exact(path))
                atomic_write_text(path, answer)


def recover_tests(root: Path) -> None:
    """Restore a complete test tree after an interrupted directory replacement."""
    work = checked_tree(root.parent / f'.{root.name}-test-replacement')
    destination = checked_tree(root / 'tests')
    if not work.exists():
        return
    try:
        state = json.loads((work / 'journal.json').read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError(f'Unreadable test recovery journal: {work}') from error
    if (not isinstance(state, dict) or set(state) != {'phase', 'original'}
            or not isinstance(state['phase'], str)
            or state['phase'] not in {'preparing', 'ready', 'committed'}
            or type(state['original']) is not bool
            or (work / 'original').exists() and not (work / 'original').is_dir()
            or any(p.name not in {'original', 'journal.json'}
                   and not (p.name.startswith('.journal.json.') and p.name.endswith('.tmp')
                            and p.is_file()) for p in work.iterdir())):
        raise ValueError(f'Unsafe test recovery journal: {work}')
    original = work / 'original'
    if state['phase'] == 'ready':
        if state['original'] and original.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            original.replace(destination)
        elif not state['original'] and destination.exists():
            shutil.rmtree(destination)
        # No original means either the first rename had not happened yet or
        # recovery already restored it before interruption.
    shutil.rmtree(work)


def publish_tests(root: Path, candidate: Path, expected: dict[str, str]) -> None:
    work = checked_tree(root.parent / f'.{root.name}-test-replacement')
    destination = checked_tree(root / 'tests')
    checked_tree(candidate)
    work.mkdir()
    state = {'phase': 'preparing', 'original': destination.exists()}
    try:
        write_json(work / 'journal.json', state)
        state['phase'] = 'ready'
        write_json(work / 'journal.json', state)
        if state['original']:
            destination.replace(work / 'original')
        candidate.replace(destination)
        if fingerprints(root) != expected:
            raise ExecutionError('Materials changed during test publication; rerun checks.')
        state['phase'] = 'committed'
        write_json(work / 'journal.json', state)
    except BaseException:
        if (work / 'journal.json').is_file():
            recover_tests(root)
        else:
            shutil.rmtree(work)
        raise
    shutil.rmtree(work)
