"""Exercise the exported checker in a disposable virtualenv without private Solve."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv


@unittest.skipUnless(shutil.which('bwrap'), 'bubblewrap is required')
class PortableRuntimeTests(unittest.TestCase):
    def test_virtualenv_solve_is_available_but_unrelated_files_stay_hidden(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / 'runtime'
            venv.EnvBuilder(with_pip=False).create(runtime)
            python = runtime / 'bin/python'
            site = next((runtime / 'lib').glob('python*/site-packages'))
            module = site / 'libsolve'
            module.mkdir()
            (module / '__init__.py').write_text('')
            sentinel = runtime / 'private-root-file'
            sentinel.write_text('synthetic secret')
            package = root / 'package'
            package.mkdir()
            (package / 'config.json').write_text('{}')
            # A tiny stand-in proves the runtime boundary; it does not simulate grading.
            (module / 'package.py').write_text(
                'from pathlib import Path\nimport shutil,sys\nfrom types import SimpleNamespace\n'
                'sys.modules["task_packer.native_runtime"] = SimpleNamespace(install_local_wall_time_margin=lambda root: None)\n'
                'class Package:\n'
                ' def __init__(self, code, root):\n'
                '  self.root=root\n'
                ' def validate(self):\n'
                '  assert (self.root/"config.json").is_file()\n'
                ' def prepare(self, **kwargs):\n'
                f'  assert not Path({str(sentinel)!r}).exists()\n'
                f'  assert not Path({str(package)!r}).exists()\n'
                f'  assert sys.prefix == {str(runtime)!r}\n'
                f'  assert shutil.which("python3") == {str(runtime / "bin/python3")!r}\n'
                '  print("virtualenv runtime available; private files hidden")\n'
            )
            repo = Path(__file__).resolve().parents[1]
            launcher = (
                f'import sys; sys.path.insert(0, {str(repo)!r}); '
                'from task_packer.portable import main; main()'
            )
            result = subprocess.run(
                [str(python), '-c', launcher, str(package)],
                capture_output=True, text=True, timeout=30,
                env={'PATH': os.defpath, 'LANG': 'C.UTF-8'},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('virtualenv runtime available; private files hidden', result.stdout)
            self.assertIn('Validation and build completed.', result.stdout)
            self.assertEqual(sentinel.read_text(), 'synthetic secret')
            self.assertEqual((package / 'config.json').read_text(), '{}')


if __name__ == '__main__':
    unittest.main()
