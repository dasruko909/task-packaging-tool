"""Isolated process using the actual libsolve package shipped with Solve CLI."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import tempfile
import hashlib
import shlex
from contextvars import ContextVar

from .sandbox import isolated

from .storage import write_json
from .paths import project_code
from .registry import load_manifest
from .solve_native import LOCAL_WALL_TIME_FACTOR


def local_wall_time_limit(time_limit: int | None) -> int | None:
    """Allow scheduler stalls locally without relaxing the measured CPU limit."""
    return time_limit * LOCAL_WALL_TIME_FACTOR if time_limit is not None else None


def install_local_wall_time_margin(checker_root: Path | None = None) -> None:
    """Separate libsolve's no-jail wall watchdog from its CPU-time grading limit."""
    from libsolve.execution.language_utils import LanguageUtils
    from libsolve.package.program import Program

    if getattr(LanguageUtils, '_packer_wall_margin_installed', False):
        return
    original = LanguageUtils._run_command_no_jail.__func__
    permissions = ContextVar('native_program_permissions', default=None)

    def scoped(method, compiling=False):
        def invoke(program, *args, **kwargs):
            readable = [program.source_path.resolve(), *program.additional_files]
            build = program.preparation_path
            writable = []
            if build is not None:
                (writable if compiling else readable).append(build.resolve())
            if not compiling and checker_root is not None and program.path.resolve() == checker_root.resolve():
                params = kwargs.get('params', [])
                params = shlex.split(params) if isinstance(params, str) else params
                readable.extend(Path(value).resolve() for value in params)
            token = permissions.set((readable, writable))
            try:
                return method(program, *args, **kwargs)
            finally:
                permissions.reset(token)
        return invoke

    def run_with_wall_margin(cls, cmd, time_limit=None):
        allowed = permissions.get()
        if allowed is None:
            raise RuntimeError('Native execution has no program isolation scope')
        readable, writable = allowed
        executable = Path(str(cmd.params[0]))
        if executable.is_file():
            readable = [*readable, executable.resolve()]
        with tempfile.TemporaryDirectory(prefix='native-invocation-') as temporary:
            # Redirections stay with the trusted supervisor. The program receives
            # only the resulting descriptors, never the surrounding output tree.
            cmd.params = isolated(
                [str(value) for value in cmd.params], work=Path(temporary),
                readable=readable, writable=writable,
            )
            return original(cls, cmd, time_limit=local_wall_time_limit(time_limit))

    Program.prepare = scoped(Program.prepare, compiling=True)
    Program.run = scoped(Program.run)
    LanguageUtils._run_command_no_jail = classmethod(run_with_wall_margin)
    LanguageUtils._packer_wall_margin_installed = True


def execute(action: str, code: str, path: Path, assignments: dict) -> dict:
    project_code(code)
    load_manifest(path)
    install_local_wall_time_margin(path / 'checker')
    from libsolve.package import Package
    from libsolve.package.test_runner import TestRunner
    from libsolve.execution import ExecutionStatus
    from libsolve.package.validate import validator

    package = Package(code, path)
    # The upstream error formatter joins integer JSON paths as strings incorrectly.
    # Use its actual validator first to retain meaningful schema diagnostics.
    errors = list(validator.iter_errors(package.config))
    if errors:
        raise RuntimeError("\n".join(f"{'.'.join(map(str, error.path))}: {error.message}" for error in errors))
    def safe_name(name):
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts or any(ch.isspace() for ch in name):
            raise RuntimeError(f"Unsupported path in manifest: {name!r}")
    for program in [package.config["checker"], *package.config["solutions"], *package.config["generators"]]:
        safe_name(program["name"])
        for name in program.get("additional_files_names", []):
            safe_name(name)
    for group in package.config["test_groups"]:
        for test in group["tests"]:
            safe_name(test["input"])
            if "output" in test:
                safe_name(test["output"])
    for generation in package.config["test_generation"]:
        names = generation["filename"]
        for name in names if isinstance(names, list) else [names]:
            safe_name(name)
    package.validate()
    result = {"validated": True, "built": False, "interactive": package.is_interactive,
              "tests": [], "solutions": [], "ok": True}
    if action == "validate":
        return result
    original_inputs = {test.input.name: hashlib.sha256(test.input.read_bytes()).hexdigest()
                       for test in package.tests if test.input.is_file()}
    package.prepare(tmp_dir=tempfile.gettempdir(), force=False)
    package.generate_inputs(force=True, tmp_dir=tempfile.gettempdir())
    for name, digest in original_inputs.items():
        if hashlib.sha256((package.tests_in_path / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Generator does not reproduce saved input {name}.')
    result["built"] = True
    result['reproducible_inputs'] = True
    if package.is_interactive:
        result["skipped"] = "libsolve 1.0.11 does not run interactions without a jail server; the packer local runner checks conversations."
        return result
    with tempfile.TemporaryDirectory(prefix="solve-results-") as temporary:
        for program, types in package.solutions:
            runner = TestRunner(temporary, package, program)
            expected = assignments.get(program.name)
            must_pass_all = "model" in types or "ac" in types
            if expected and not must_pass_all and not set(expected) <= {group.name for group in package.test_groups}:
                raise RuntimeError(f"No subtask group for {program.name}: {expected}")
            total = 0.0
            for gi, group in enumerate(package.test_groups):
                scores = []
                for ti, test in enumerate(group.tests):
                    checked = runner.run_test(gi, ti)
                    program_ok = checked.program_result is not None and checked.program_result.status == ExecutionStatus.OK
                    checker_ok = checked.checker_result is not None and checked.checker_result.status == ExecutionStatus.OK
                    output = checked.checker_output
                    valid = program_ok and checker_ok and output is not None and not output.is_wrong
                    value = output.score if valid else 0
                    required = must_pass_all or group.name in (expected or [])
                    row = {"solution": program.name, "group": group.name, "input": test._input,
                           "score": value, "required": required,
                           "status": checked.program_result.status.name if checked.program_result else "SYSTEM_ERROR",
                           "checker_status": checked.checker_result.status.name if checked.checker_result else "NOT_RUN",
                           "time_ms": checked.program_result.time if checked.program_result else None,
                           "memory_kb": checked.program_result.memory if checked.program_result else None,
                           "message": output.message if output else "Checker produced no result"}
                    result["tests"].append(row)
                    scores.append(value)
                    if (required and value != 100) or (program_ok and not checker_ok) or (output and output.is_wrong):
                        result["ok"] = False
                total += min(scores, default=0) * package.get_group_score(gi) / 100
            result["solutions"].append({"name": program.name, "types": types, "score": total})
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["validate", "check"])
    parser.add_argument("code")
    parser.add_argument("path", type=Path)
    parser.add_argument("result", type=Path)
    parser.add_argument("assignments", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        result = execute(args.action, args.code, args.path, json.loads(args.assignments.read_text()))
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    write_json(args.result, result)
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
