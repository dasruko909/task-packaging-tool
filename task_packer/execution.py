"""Bounded local execution of generated programs (Linux/POSIX)."""
from __future__ import annotations

import math
import os
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

    def run(self, command: list[str], *, data: str = "", seconds: float = 3,
            memory_kb: int = 262144, source: Path | None = None) -> str:
        with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            inp.write(data.encode()); inp.seek(0)
            try:
                process = subprocess.Popen(
                    isolated(command, work=self.directory, readable=[self.includes, source] if source else [self.includes]), stdin=inp, stdout=out, stderr=err, cwd=self.directory,
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

    def compile(self, source: Path) -> list[str]:
        source = source.resolve()
        if source in self.commands:
            return self.commands[source]
        if source.suffix == ".py":
            # Compile without writing __pycache__ alongside source files.
            self.run([sys.executable, "-c", "import ast,sys; ast.parse(open(sys.argv[1]).read())", str(source)], source=source)
            command = [sys.executable, "-I", str(source)]
        elif source.suffix in {".cpp", ".c"}:
            compiler = shutil.which("g++" if source.suffix == ".cpp" else "gcc")
            if compiler is None:
                raise ExecutionError("Compiler not found for " + source.name)
            binary = self.directory / f"program_{len(self.commands)}"
            self.run([compiler, str(source), "-O2", "-std=c++17" if source.suffix == ".cpp" else "-std=c11",
                      "-I", str(self.includes), "-o", str(binary)],
                     seconds=45, memory_kb=1048576, source=source)
            command = [str(binary)]
        else:
            raise ExecutionError(f"Local execution does not support {source.suffix}: {source.name}")
        self.commands[source] = command
        return command

    def program(self, source: Path, data: str = "", args: list[str] | None = None,
                seconds: float = 3, memory_kb: int = 262144) -> str:
        try:
            return self.run(self.compile(source) + (args or []), data=data, seconds=seconds,
                            memory_kb=memory_kb, source=source)
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
            if source.suffix == ".py":
                target = work / "program.py"
                shutil.copy2(source, target)
                return [sys.executable, "-I", str(target)]
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
