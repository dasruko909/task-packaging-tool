"""Security properties exercised through the real bubblewrap sandbox."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from task_packer.execution import Runner
from task_packer.sandbox import isolated
from task_packer.solve_native import runtime_mounts, runtime_python


@unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is required")
class SandboxIsolationTests(unittest.TestCase):
    def test_native_runtime_hides_unrelated_virtualenv_root_files(self) -> None:
        runtime_root = runtime_python().parent.parent
        if not (runtime_root / "pyvenv.cfg").is_file():
            self.skipTest("native Solve does not use a virtual environment")
        sentinel = runtime_root / "sandbox-root-sentinel"
        self.assertFalse(sentinel.exists())
        sentinel.write_text("synthetic secret", encoding="utf-8")
        try:
            with tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                code = (
                    "import sys; from pathlib import Path; "
                    "from libsolve.package import Package; "
                    "print('visible' if Path(sys.argv[1]).exists() else 'hidden')"
                )
                command = isolated(
                    [str(runtime_python()), "-c", code, str(sentinel)],
                    work=work, readable=runtime_mounts(),
                )
                result = subprocess.run(
                    command, cwd=work, capture_output=True, text=True, timeout=15,
                    env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "hidden")
        finally:
            sentinel.unlink(missing_ok=True)

    def test_program_cannot_reach_host_siblings_secrets_environment_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            public = root / "public"
            programs = root / "programs"
            work.mkdir(); public.mkdir(); programs.mkdir()
            outside = root / "project-secret.txt"
            outside.write_text("project secret", encoding="utf-8")
            reference = work / "reference.txt"
            reference.write_text("reference answer", encoding="utf-8")

            sibling = programs / "sibling.py"
            sibling.write_text("print('sibling')\n", encoding="utf-8")
            runner = Runner(work, public)
            runner.compile(sibling)
            sibling_stage = runner.staged_sources[sibling.resolve()]

            dependency = programs / "dependency.txt"
            dependency.write_text("unchanged", encoding="utf-8")
            probe = programs / "probe.py"
            probe.write_text(
                "import json, os, socket\n"
                "from pathlib import Path\n"
                f"targets = {repr([str(outside), str(reference), str(sibling_stage)])}\n"
                "seen = []\n"
                "for target in targets:\n"
                " try: seen.append(Path(target).read_text())\n"
                " except OSError: seen.append('blocked')\n"
                "try:\n"
                " (Path(__file__).parent / 'dependency.txt').write_text('changed')\n"
                " dependency_write = 'allowed'\n"
                "except OSError: dependency_write = 'blocked'\n"
                "try:\n"
                " sock = socket.socket(); sock.settimeout(.2); sock.connect(('192.0.2.1', 9))\n"
                " network = 'allowed'\n"
                "except OSError: network = 'blocked'\n"
                "print(json.dumps({'seen': seen, 'env': os.getenv('PACKER_TEST_SECRET'), "
                "'write': dependency_write, 'network': network}))\n",
                encoding="utf-8",
            )
            runner.compile(probe, dependencies=(dependency,), root=programs)
            old = os.environ.get("PACKER_TEST_SECRET")
            os.environ["PACKER_TEST_SECRET"] = "host token"
            try:
                result = json.loads(runner.program(probe))
            finally:
                if old is None:
                    os.environ.pop("PACKER_TEST_SECRET", None)
                else:
                    os.environ["PACKER_TEST_SECRET"] = old

            self.assertEqual(result["seen"], ["blocked", "blocked", "blocked"])
            self.assertIsNone(result["env"])
            self.assertEqual(result["write"], "blocked")
            self.assertEqual(result["network"], "blocked")
            self.assertEqual(dependency.read_text(encoding="utf-8"), "unchanged")

    def test_checker_receives_only_explicit_input_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("work", "public", "checker"):
                (root / name).mkdir()
            checker = root / "checker/checker.py"
            checker.write_text(
                "import sys\n"
                "values = [open(path).read() for path in sys.argv[1:]]\n"
                "print(100 if values == ['input', 'reference', 'candidate'] else 0)\n",
                encoding="utf-8",
            )
            runner = Runner(root / "work", root / "public")
            paths = []
            for name, value in (("input", "input"), ("reference", "reference"),
                                ("candidate", "candidate")):
                path = root / "work" / f"{name}.txt"
                path.write_text(value, encoding="utf-8")
                paths.append(path)

            output = runner.program(
                checker, args=[str(path) for path in paths], readable=paths
            )
            self.assertEqual(output.strip(), "100")

    def test_interactive_programs_keep_separate_views(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("work", "public", "programs"):
                (root / name).mkdir()
            judge = root / "programs/judge.py"
            judge.write_text(
                "import sys\n"
                "value = open(sys.argv[1]).read().strip()\n"
                "print(value, flush=True)\n"
                "reply = input().strip()\n"
                "open(sys.argv[2], 'w').write('100' if reply == value else '0')\n",
                encoding="utf-8",
            )
            contestant = root / "programs/contestant.py"
            contestant.write_text("print(input(), flush=True)\n", encoding="utf-8")
            input_path = root / "interactive-input.txt"
            input_path.write_text("42\n", encoding="utf-8")
            runner = Runner(root / "work", root / "public")

            verdict, timed_out = runner.interaction(
                judge, contestant, input_path, seconds=2, memory_kb=262144
            )

            self.assertFalse(timed_out)
            self.assertEqual(verdict, "100")


if __name__ == "__main__":
    unittest.main()
