"""Bounded local execution of generated programs (Linux/POSIX)."""
from __future__ import annotations

import math
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from .sandbox import isolated


class ExecutionError(RuntimeError):
    def __init__(self, message: str, source: Path | None = None):
        super().__init__(message)
        self.source = source


def limits(seconds: float, memory_kb: int):
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(seconds) + 1,) * 2)
        resource.setrlimit(resource.RLIMIT_AS, (memory_kb * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


class Runner:
    def __init__(self, directory: Path, includes: Path):
        self.directory = directory.resolve()
        self.includes = includes.resolve()
        self.commands: dict[Path, list[str]] = {}
        self.stages: dict[Path, Path] = {}
        self.staged_sources: dict[Path, Path] = {}

    def run(self, command: list[str], *, data: str = "", seconds: float = 3,
            memory_kb: int = 262144, source: Path | None = None,
            cwd: Path | None = None) -> str:
        with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            inp.write(data.encode()); inp.seek(0)
            try:
                process = subprocess.Popen(
                    isolated(command, work=self.directory, readable=[self.includes], cwd=cwd),
                    stdin=inp, stdout=out, stderr=err, cwd=cwd or self.directory,
                    start_new_session=True, preexec_fn=limits(seconds, memory_kb),
                    env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                )
            except OSError as error:
                raise ExecutionError(str(error), source) from error
            timed_out = False
            try:
                process.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                kill(process)
            err.seek(0)
            diagnostic = err.read(8000).decode(errors="replace")
            if timed_out or process.returncode:
                raise ExecutionError(
                    f"{'Time limit exceeded' if timed_out else f'Exit code {process.returncode}'}: {diagnostic}", source)
            out.seek(0)
            return out.read().decode(errors="replace")

    @staticmethod
    def _declared_language(source: Path, language: object | None, label: str) -> str:
        inferred = {'.cpp': 'cpp17', '.c': 'c', '.py': 'python3'}.get(source.suffix.lower())
        aliases = {'cpp': 'cpp17', 'c++': 'cpp17', 'python': 'python3', 'py': 'python3'}
        if language is not None and not isinstance(language, str):
            raise ExecutionError(f"Unsupported language {language!r} for {label}.", source)
        selected = aliases.get((language or '').lower(), (language or '').lower()) or inferred
        if selected not in {'cpp17', 'c', 'python3'}:
            shown = language or source.suffix or 'unknown'
            raise ExecutionError(f"Unsupported language {shown!r} for {label}.", source)
        expected = {'cpp17': '.cpp', 'c': '.c', 'python3': '.py'}[selected]
        if source.suffix.lower() != expected:
            raise ExecutionError(
                f"Unsupported source extension {source.suffix or '<none>'!r} for {label} ({selected}).",
                source,
            )
        return selected

    @staticmethod
    def _quoted_includes(path: Path) -> list[str]:
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            return []
        return re.findall(r'^\s*#\s*include\s*"([^"\n]+)"', text, flags=re.MULTILINE)

    @staticmethod
    def _python_command(source: Path, root: Path) -> list[str]:
        launcher = (
            'import runpy,sys; sys.path[:0]=sys.argv[1:3]; '
            'sys.argv=sys.argv[3:]; runpy.run_path(sys.argv[0],run_name="__main__")'
        )
        return [
            sys.executable, '-I', '-c', launcher,
            str(root), str(source.parent), str(source),
        ]

    def _local_files(self, source: Path, root: Path,
                     dependencies: tuple[Path, ...], label: str) -> dict[Path, Path]:
        try:
            source_relative = source.relative_to(root)
        except ValueError as error:
            raise ExecutionError(f"Program {label} is outside {root}: {source}", source) from error
        files: dict[Path, Path] = {source: source_relative}
        queue = [source]
        for dependency in dependencies:
            dependency = dependency.resolve()
            if not dependency.is_file():
                raise ExecutionError(f"Missing dependency for {label}: {dependency}", source)
            try:
                relative = dependency.relative_to(root)
            except ValueError as error:
                raise ExecutionError(f"Dependency for {label} is outside {root}: {dependency}", source) from error
            files[dependency] = relative
            queue.append(dependency)
        visited = set()
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            for name in self._quoted_includes(current):
                candidates = (current.parent / name, root / name, self.includes / name)
                dependency = next((path.resolve() for path in candidates if path.is_file()), None)
                if dependency is None or dependency.is_relative_to(self.includes):
                    continue
                if not dependency.is_relative_to(root):
                    continue
                if dependency not in files:
                    files[dependency] = dependency.relative_to(root)
                    queue.append(dependency)
        return files

    def compile(self, source: Path, *, dependencies=(), root: Path | None = None,
                language: object | None = None, label: str | None = None) -> list[str]:
        source = source.resolve()
        if source in self.commands:
            return self.commands[source]
        label = label or source.name
        selected = self._declared_language(source, language, label)
        if not source.is_file():
            raise ExecutionError(f"Missing program {label}: {source}", source)
        root = (root or source.parent).resolve()
        dependencies = tuple(Path(path).resolve() for path in dependencies)
        files = self._local_files(source, root, dependencies, label)
        stage = self.directory / f"program_{len(self.commands)}_files"
        stage.mkdir()
        for original, relative in files.items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
        staged_source = stage / files[source]
        if selected == "python3":
            # Compile without writing __pycache__ alongside source files.
            self.run([sys.executable, "-c", "import ast,sys; ast.parse(open(sys.argv[1]).read())", str(staged_source)], source=source, cwd=stage)
            # Isolated mode intentionally drops the script directory from
            # sys.path. Add only the staged dependency tree back for imports.
            command = self._python_command(staged_source, stage)
        else:
            compiler = shutil.which("g++" if selected == "cpp17" else "gcc")
            if compiler is None:
                raise ExecutionError("Compiler not found for " + label, source)
            binary = stage / "program"
            self.run([compiler, str(staged_source), "-O2", "-std=c++17" if selected == "cpp17" else "-std=c11",
                      "-I", str(stage), "-I", str(self.includes), "-o", str(binary)],
                     seconds=45, memory_kb=1048576, source=source, cwd=stage)
            command = [str(binary)]
        self.commands[source] = command
        self.stages[source] = stage
        self.staged_sources[source] = staged_source
        return command

    def program(self, source: Path, data: str = "", args: list[str] | None = None,
                seconds: float = 3, memory_kb: int = 262144) -> str:
        try:
            source = source.resolve()
            command = self.compile(source)
            return self.run(command + (args or []), data=data, seconds=seconds,
                            memory_kb=memory_kb, source=source, cwd=self.stages[source])
        except ExecutionError as error:
            raise ExecutionError(f"{error}\nArguments: {args or []}\nInput: {data[:2000]}", error.source) from error

    def interaction(self, judge: Path, contestant: Path, input_path: Path,
                    *, seconds: float, memory_kb: int) -> tuple[str, bool]:
        judge_command = self.compile(judge)
        contestant_command = self.compile(contestant)
        judge_work = Path(tempfile.mkdtemp(prefix="interaction-judge-", dir=self.directory))
        contestant_work = Path(tempfile.mkdtemp(prefix="interaction-contestant-", dir=self.directory))

        def private_command(command: list[str], source: Path, work: Path) -> list[str]:
            """Copy the executable into one side's private writable sandbox."""
            shutil.copytree(self.stages[source.resolve()], work, dirs_exist_ok=True)
            if source.suffix == ".py":
                target = work / self.staged_sources[source.resolve()].relative_to(
                    self.stages[source.resolve()]
                )
                return self._python_command(target, work)
            target = work / "program"
            shutil.copy2(Path(command[0]), target)
            target.chmod(0o755)
            return [str(target), *command[1:]]

        judge_command = private_command(judge_command, judge.resolve(), judge_work)
        contestant_command = private_command(
            contestant_command, contestant.resolve(), contestant_work
        )
        private_input = judge_work / "input.txt"
        shutil.copy2(input_path, private_input)
        result = judge_work / "verdict.txt"
        read_a, write_a = os.pipe()
        read_b, write_b = os.pipe()
        processes = []
        try:
            with tempfile.TemporaryFile() as err:
                for command, work, stdin, stdout, memory in (
                    (judge_command + [str(private_input), str(result)], judge_work,
                     read_a, write_b, 262144),
                    (contestant_command, contestant_work, read_b, write_a, memory_kb),
                ):
                    processes.append(subprocess.Popen(
                        isolated(command, work=work), stdin=stdin, stdout=stdout,
                        stderr=err, cwd=work,
                        start_new_session=True, preexec_fn=limits(seconds, memory),
                        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                    ))
                for fd in (read_a, write_a, read_b, write_b):
                    os.close(fd)
                read_a = write_a = read_b = write_b = -1
                deadline = time.monotonic() + seconds
                while any(p.poll() is None for p in processes) and time.monotonic() < deadline:
                    time.sleep(.01)
                timeout = any(p.poll() is None for p in processes)
                for process in processes:
                    kill(process)
                if not timeout and any(p.returncode != 0 for p in processes):
                    err.seek(0)
                    raise ExecutionError("Interactive process error: " + err.read(8000).decode(errors="replace"))
                return (result.read_text() if result.exists() else ""), timeout
        finally:
            for fd in (read_a, write_a, read_b, write_b):
                if fd >= 0:
                    os.close(fd)
            for process in processes:
                kill(process)
            shutil.rmtree(judge_work, ignore_errors=True)
            shutil.rmtree(contestant_work, ignore_errors=True)
