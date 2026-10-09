"""Integration with the supplied Solve CLI 1.0.14 and libsolve 1.0.11."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

from .execution import kill
from .models import ProjectConfig
from .storage import atomic_write_text, write_json
from .sandbox import isolated
from .paths import checked_path, checked_tree
from .registry import safe_file, load_manifest

ROOT = Path(__file__).resolve().parent.parent
WHEELS = ROOT / "vendor/wheels"
VERSIONS = {"libsolve": "1.0.11", "solve-cli": "1.0.14"}
WHEEL_NAMES = (
    "libsolve-1.0.11-py3-none-any.whl",
    "solve_cli-1.0.14-py3-none-any.whl",
)
WHEEL_MESSAGE = (
    "Private Solve 4 wheels are missing. Wrocław students should place the "
    "authorised wheel files in vendor/wheels/; see README.md."
)
LOCAL_TIME_LIMIT_NUMERATOR = 5
LOCAL_TIME_LIMIT_DENOMINATOR = 4
LOCAL_WALL_TIME_FACTOR = 4


def install_standard_checker(name: str, directory: Path) -> None:
    safe_file(directory, name)
    wheel = WHEELS / "libsolve-1.0.11-py3-none-any.whl"
    if wheel.is_file():
        with zipfile.ZipFile(wheel) as archive:
            known = json.loads(archive.read("libsolve/checker/files/list.json"))
            if name not in {item["name"] for item in known}:
                raise RuntimeError(f"Unknown standard checker: {name}")
            content = archive.read("libsolve/checker/files/" + name).decode()
    else:
        try:
            from importlib.resources import files
            resource = files("libsolve").joinpath("checker/files")
            known = json.loads(resource.joinpath("list.json").read_text())
            if name not in {item["name"] for item in known}:
                raise RuntimeError(f"Unknown standard checker: {name}")
            content = resource.joinpath(name).read_text()
        except (ModuleNotFoundError, OSError, TypeError, ValueError) as error:
            raise RuntimeError(WHEEL_MESSAGE) from error
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / name, content)


def runtime_python() -> Path:
    local = ROOT / ".venv/bin/python"
    return local if local.is_file() else Path(sys.executable)


def environment() -> dict[str, str]:
    env = {"LANG": "C.UTF-8", "PATH": os.environ.get("PATH", os.defpath)}
    env["PATH"] = str(runtime_python().parent) + os.pathsep + env.get("PATH", os.defpath)
    return env


def doctor() -> dict:
    code = "import json; from importlib.metadata import version; from libsolve.package import Package; from solve_cli import main; print(json.dumps({n:version(n) for n in ['libsolve','solve-cli']}))"
    try:
        with tempfile.TemporaryDirectory(prefix="solve-doctor-") as temporary:
            env = dict(environment(), XDG_CONFIG_HOME=temporary)
            result = subprocess.run([str(runtime_python()), "-c", code], capture_output=True, text=True, timeout=15, env=env)
        versions = json.loads(result.stdout) if result.returncode == 0 else {}
        error = result.stderr.strip()[-2000:] if result.returncode else ""
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        versions, error = {}, str(exc)
    sandbox = shutil.which('bwrap')
    sandbox_ready = False
    sandbox_error = "bubblewrap (bwrap) is missing."
    if sandbox:
        try:
            with tempfile.TemporaryDirectory(prefix="solve-sandbox-doctor-") as temporary:
                work = Path(temporary)
                probe = isolated(
                    [str(runtime_python()), "-c", "print('sandbox-ok')"],
                    work=work,
                )
                result = subprocess.run(
                    probe, cwd=work, capture_output=True, text=True, timeout=15,
                    env=environment(),
                )
            sandbox_ready = result.returncode == 0 and result.stdout.strip() == "sandbox-ok"
            if not sandbox_ready:
                sandbox_error = (result.stderr.strip() or result.stdout.strip() or
                                 f"bubblewrap exited with code {result.returncode}")[-2000:]
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            sandbox_error = str(exc)
    wheel_files = {name: (WHEELS / name).is_file() for name in WHEEL_NAMES}
    wheel_error = "" if all(wheel_files.values()) else WHEEL_MESSAGE
    return {"python": str(runtime_python()), "versions": versions,
            "ready": versions == VERSIONS, "error": error,
            "wheel_files": wheel_files, "wheel_error": wheel_error,
            'sandbox': sandbox, 'sandbox_ready': sandbox_ready,
            'sandbox_error': sandbox_error,
            "compilers": {name: shutil.which(name) for name in ("g++", "gcc", "java", "javac", "python3")},
            "interactive_native": "requires a jail server; local runner available",
            "install": "./run.sh --doctor"}


def copy_package(source: Path, destination: Path) -> None:
    checked_tree(source)
    checked_tree(destination)
    load_manifest(source)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part.startswith("prepared_") or part in {"__pycache__", "verification"} for part in relative.parts):
            continue
        if ".before-repair-" in path.name:
            continue
        if path.is_symlink():
            raise RuntimeError(f"Package contains a symbolic link: {relative}")
        target = safe_file(destination, relative.as_posix())
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def add_local_time_margin(package: Path, action: str) -> dict | None:
    """Relax only the disposable local copy to account for a slower runner."""
    if action != "check":
        return None
    path = package / "config.json"
    manifest = load_manifest(package)
    configured = int(manifest["limits"]["time"])
    local = (
        configured * LOCAL_TIME_LIMIT_NUMERATOR + LOCAL_TIME_LIMIT_DENOMINATOR - 1
    ) // LOCAL_TIME_LIMIT_DENOMINATOR
    manifest["limits"]["time"] = local
    write_json(path, manifest)
    return {
        "configured_time_ms": configured,
        "local_time_ms": local,
        "time_factor": LOCAL_TIME_LIMIT_NUMERATOR / LOCAL_TIME_LIMIT_DENOMINATOR,
        "wall_timeout_ms": local * LOCAL_WALL_TIME_FACTOR,
        "wall_time_factor": LOCAL_WALL_TIME_FACTOR,
    }


def native_action(config: ProjectConfig, action: str = "check") -> dict:
    checked_tree(config.package_dir)
    folder = config.package_dir / "verification"
    folder.mkdir(parents=True, exist_ok=True)
    result_path = folder / f"solve-{action}.json"
    health = doctor()
    if not health["ready"]:
        result = {"ok": None, "error": "No compatible Solve environment. Run ./run.sh --doctor.", "environment": health}
        write_json(result_path, result)
        return result
    if not health['sandbox_ready']:
        result = {'ok': False, 'error': 'bubblewrap isolation is not working: ' + health['sandbox_error']}
        write_json(result_path, result)
        return result
    from .solve4 import source_extension
    manifest = load_manifest(config.package_dir)
    groups = [group for group in manifest.get("test_groups", []) if not group.get("is_sample")]
    assignments = {}
    for item in config.subtasks:
        name = item.group_name or f"{item.index:02d}"
        if not any(group.get("name") == name for group in groups) and item.index <= len(groups):
            name = groups[item.index - 1].get("name", str(item.index - 1))
        from .registry import solution_path
        assignments[solution_path(config, item).name] = [name]
    write_json(result_path, {"ok": None, "state": "running"})
    log = folder / f"solve-{action}.log"
    with tempfile.TemporaryDirectory(prefix="packer-solve-") as temporary:
        work = Path(temporary)
        package = work / config.codename
        copy_package(config.package_dir, package)
        execution_limits = add_local_time_margin(package, action)
        output = work / "result.json"
        inputs = work / "assignments.json"
        write_json(inputs, assignments)
        command = [str(runtime_python()), "-m", "task_packer.solve_worker", action,
                   config.codename, str(package), str(output), str(inputs)]
        with log.open("w") as stream:
            env = environment()
            env['PYTHONPATH'] = str(ROOT)
            command = isolated(command, work=work, readable=[ROOT / 'task_packer', WHEELS, runtime_python().parent.parent], env=env)
            process = subprocess.Popen(command, cwd=ROOT, env=environment(), stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.wait(timeout=60 if action == "validate" else 1200)
            except subprocess.TimeoutExpired:
                result = {"ok": False, "error": "Solve operation timed out.", "log": str(log)}
                write_json(result_path, result)
                return result
            finally:
                kill(process)
        result = json.loads(output.read_text()) if output.exists() else {"ok": False, "error": "Solve did not write a result; see the log."}
    if execution_limits:
        result["execution_limits"] = execution_limits
    result["versions"] = health["versions"]
    result["log"] = str(log)
    write_json(folder / f"solve-{action}.json", result)
    return result


def native_validate(config: ProjectConfig) -> tuple[bool | None, str]:
    result = native_action(config, "validate")
    return result["ok"], result.get("error", "Manifest and files passed validation by the original libsolve 1.0.11.")


def archive_package(config: ProjectConfig) -> Path:
    """Export a complete package; keep fixed outputs so CLI upload cannot remove them."""
    root = config.package_dir
    destination = checked_path(root.parent / (config.codename + ".zip"))
    checked_path(destination.with_suffix(".zip.sha256"))
    with tempfile.TemporaryDirectory(prefix="packer-export-") as temporary:
        work = Path(temporary) / config.codename
        copy_package(root, work)
        manifest = load_manifest(work)
        if manifest["type"] != "solve-interactive":
            for group in manifest["test_groups"]:
                for test in group["tests"]:
                    output = test.get("output", test["input"].replace(".in", ".out"))
                    if not (work / "tests/out" / output).is_file():
                        raise RuntimeError(f"Cannot export: output {output} is missing.")
                    test["output"] = output
        readme = work / "readme.md"
        if readme.is_file():
            atomic_write_text(readme, readme.read_text().replace(
                "See `verification/report.md` and `verification/report.json`. PASS means",
                "The `verification/report.md` and `verification/report.json` reports remain in the packer project. PASS means"))
        recipe = {'test_generation': manifest.get('test_generation', []),
                  'inputs': {str(p.relative_to(work / 'tests/in')): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in (work / 'tests/in').rglob('*') if p.is_file()}}
        write_json(work / 'generation-recipe.json', recipe)
        # Fixed test files are portable; the full generation recipe remains in the ZIP.
        manifest["test_generation"] = []
        write_json(work / "config.json", manifest)
        temporary_zip = Path(temporary) / "package.zip"
        with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(work.rglob("*")):
                if path.is_file():
                    archive.write(path, str(path.relative_to(work)))
        shutil.copy2(temporary_zip, destination)
    atomic_write_text(destination.with_suffix(".zip.sha256"), hashlib.sha256(destination.read_bytes()).hexdigest() + "  " + destination.name + "\n")
    return destination
