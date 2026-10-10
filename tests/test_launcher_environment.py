"""Launcher regressions using an isolated checkout and a fake environment."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WHEELS = (
    "libsolve-1.0.11-py3-none-any.whl",
    "solve_cli-1.0.14-py3-none-any.whl",
)


FAKE_PYTHON = r'''#!/usr/bin/env python3
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
state = root / "fake-state"
log = root / "fake-calls"
mode = state.read_text().splitlines() if state.exists() else []
args = sys.argv[1:]
with log.open("a") as stream:
    stream.write(" ".join(args) + "\n")

if args[:1] == ["-c"]:
    code = args[1]
    if "import solve_cli" in code:
        sys.exit(0 if "solve" in mode else 1)
    sys.exit(0 if "base" in mode else 1)
if args[:2] == ["-m", "pip"] and args[2:4] == ["install", "-r"]:
    if "fail-install" in mode:
        print("synthetic pip failure", file=sys.stderr)
        sys.exit(23)
    state.write_text("base\n" + ("solve\n" if "solve" in mode else ""))
    sys.exit(0)
if args[:3] == ["-m", "pip", "install"]:
    if "fail-solve" in mode:
        print("synthetic wheel failure", file=sys.stderr)
        sys.exit(24)
    state.write_text("\n".join(sorted(set(mode + ["base", "solve"]))) + "\n")
    sys.exit(0)
if args[:1] == ["packer.py"]:
    print("PACKER LAUNCHED")
    sys.exit(0)
print("unexpected fake python arguments", args, file=sys.stderr)
sys.exit(25)
'''


class LauncherEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "checkout"
        self.root.mkdir()
        shutil.copy2(ROOT / "run.sh", self.root / "run.sh")
        (self.root / "requirements.txt").write_text("# synthetic\n", encoding="utf-8")
        (self.root / "packer.py").write_text("# synthetic\n", encoding="utf-8")
        python = self.root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(FAKE_PYTHON, encoding="utf-8")
        python.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def set_mode(self, *values: str) -> None:
        (self.root / "fake-state").write_text("\n".join(values), encoding="utf-8")

    def add_wheels(self) -> None:
        directory = self.root / "vendor/wheels"
        directory.mkdir(parents=True, exist_ok=True)
        for name in WHEELS:
            (directory / name).write_bytes(b"synthetic wheel placeholder")

    def launch(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "run.sh", "--synthetic"], cwd=self.root, text=True,
            capture_output=True, check=False, env=dict(os.environ),
        )

    def calls(self) -> list[str]:
        path = self.root / "fake-calls"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def test_healthy_base_without_wheels_launches_without_pip(self) -> None:
        self.set_mode("base")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PACKER LAUNCHED", result.stdout)
        self.assertFalse(any("pip" in call for call in self.calls()))
        self.assertIn("Basic workflows are available", result.stderr)

    def test_unhealthy_base_installs_requirements_then_launches(self) -> None:
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Setting up the application environment", result.stdout)
        self.assertTrue(any("pip install -r requirements.txt" in call for call in self.calls()))
        self.assertIn("PACKER LAUNCHED", result.stdout)

    def test_newly_supplied_wheels_install_on_later_launch(self) -> None:
        self.set_mode("base")
        first = self.launch()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.add_wheels()
        second = self.launch()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(any("libsolve-1.0.11" in call and "solve_cli-1.0.14" in call
                            for call in self.calls()))
        self.assertIn("Installing the supplied Solve 4 wheels", second.stdout)

    def test_healthy_complete_environment_does_not_install_again(self) -> None:
        self.set_mode("base", "solve")
        self.add_wheels()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("pip" in call for call in self.calls()))
        self.assertIn("PACKER LAUNCHED", result.stdout)

    def test_failed_base_install_reports_clear_error(self) -> None:
        self.set_mode("fail-install")
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("synthetic pip failure", result.stderr)
        self.assertIn("Could not install application requirements", result.stderr)
        self.assertNotIn("PACKER LAUNCHED", result.stdout)


if __name__ == "__main__":
    unittest.main()
