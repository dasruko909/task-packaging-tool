"""Declared Python helpers through import, native grading and exported reproduction."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

from task_packer.execution import Runner
from task_packer.freshness import certify, changed_files, fingerprints
from task_packer.input_sources import import_existing_materials
from task_packer.models import ProjectConfig, Subtask
from task_packer.registry import programs
from task_packer.solve4 import create_package_skeleton, write_package_metadata
from task_packer.solve_native import archive_package, native_action, runtime_python


class NativePythonDependencyTests(unittest.TestCase):
    def test_adapter_rejects_escaping_linked_and_conflicting_dependencies(self):
        code = r'''
import tempfile
from pathlib import Path
from task_packer.native_runtime import install_local_wall_time_margin
from libsolve.package.program import Program
install_local_wall_time_margin()
with tempfile.TemporaryDirectory() as temporary:
 root=Path(temporary); source=root/'solutions'; source.mkdir()
 (source/'main.py').write_text('print(42)\n')
 secret=root/'secret.py'; secret.write_text('secret')
 (source/'linked.py').symlink_to(secret)
 for dependency in ('../secret.py', str(secret), 'linked.py', 'main.pyc'):
  try: Program(source,'main.py','python3',[dependency]).prepare(tmp_dir=root)
  except ValueError: pass
  else: raise AssertionError(dependency)
 assert not (source/'prepared_main.py').exists()
 assert secret.read_text()=='secret'
'''
        result = subprocess.run([str(runtime_python()), '-c', code],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_imported_nested_helpers_grade_and_reproduce_with_private_views(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            os.chdir(temporary)
            try:
                config = ProjectConfig(codename='helpers', title='Echo', origin='Regression',
                    language_code='en', original_statement='Echo n.',
                    subtasks=[Subtask(1, 'Full', 100, 'All')], task_type='standard',
                    input_package='input/helpers/package', existing_tests=True)
                source = Path(config.input_package)
                def put(name, value):
                    p = source / name; p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(value.encode() if isinstance(value, str) else value)
                payload = b'7 \t\r\n\r\n'
                answer = b'7 \t\r\n'
                hidden = [str((source / 'solutions/sibling.py').absolute()),
                          str((config.package_dir / 'solutions/sibling.py').absolute()),
                          str((config.package_dir / 'tests/out/fixed.out').absolute()),
                          str(runtime_python().parent.parent / 'pyvenv.cfg')]
                probe = ('import os,socket\nfrom pathlib import Path\n'
                         f'assert all(not Path(p).exists() for p in {hidden!r})\n'
                         'assert not (Path(__file__).parent.parent / "sibling.py").exists()\n'
                         'assert os.getenv("PACKER_TEST_SECRET") is None\n'
                         'assert all(not (p/"tests/out/fixed.out").exists() for p in Path(__file__).parents)\n'
                         'try:\n import libsolve\nexcept ImportError: pass\nelse: raise AssertionError("virtualenv")\n'
                         'try:\n socket.create_connection(("192.0.2.1",9),timeout=.1)\n'
                         'except OSError: pass\nelse: raise AssertionError("network")\n')
                put('solutions/entries/main.py', probe +
                    'from lib.helper import echo\nprint(echo(input()),end=" \\t\\r\\n")\n')
                put('solutions/lib/helper.py', 'from pathlib import Path\ndef echo(value): return int(value)+int(Path("assets/bonus.txt").read_text())\n')
                put('solutions/assets/bonus.txt', '0\n')
                put('solutions/sibling.py', 'raise AssertionError("undeclared sibling")\n')
                put('generators/entries/gen.py', 'import sys\nfrom lib.helper import payload\n'
                    'assert sys.argv[1:] == ["seed"]\nprint(payload,end="")\n')
                put('generators/lib/helper.py', 'from pathlib import Path\npayload=Path("assets/payload.txt").read_bytes().decode()\n')
                put('generators/assets/payload.txt', payload)
                put('tests/in/fixed.in', payload); put('tests/out/fixed.out', answer)
                put('description/en.md', 'Echo n.\n'); put('editorial/en.md', 'Print n.\n')
                manifest = {'type':'solve', 'checker':{'name':'printing_check.cpp','standard':True,'prog_lang':'cpp17'},
                    'solutions':[{'name':'entries/main.py','type':'model','prog_lang':'python3',
                                  'additional_files_names':['lib/helper.py', 'assets/bonus.txt']}],
                    'generators':[{'name':'entries/gen.py','prog_lang':'python3',
                                   'additional_files_names':['lib/helper.py', 'assets/payload.txt']}],
                    'test_groups':[{'name':'full','score':100,'tests':[{'input':'fixed.in','output':'fixed.out'}]}],
                    'test_generation':[{'generator':'entries/gen.py','parameters':'seed','filename':'fixed.in'}]}
                put('config.json', json.dumps(manifest))
                create_package_skeleton(config); import_existing_materials(config); write_package_metadata(config)
                root = config.package_dir
                selected = programs(root, json.loads((root/'config.json').read_text()))
                with tempfile.TemporaryDirectory() as work:
                    runner = Runner(Path(work), root)
                    solution = next(p for p in selected if p.role == 'solution')
                    runner.compile(solution.source, dependencies=solution.dependencies,
                                   root=solution.root, language=solution.language)
                    self.assertEqual(runner.program(solution.source, payload.decode()), answer.decode())
                before = fingerprints(root)
                result = native_action(config)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['solutions'][0]['score'], 100)
                self.assertEqual(result['tests'][0]['status'], 'OK')
                self.assertEqual((root/'tests/in/fixed.in').read_bytes(), payload)
                self.assertEqual((root/'tests/out/fixed.out').read_bytes(), answer)
                certify(config, native=True, expected=before)
                self.assertEqual(changed_files(config), [])
                archive = archive_package(config, expected=before)
                self.assertEqual(archive.with_suffix('.zip.sha256').read_text().split()[0],
                                 hashlib.sha256(archive.read_bytes()).hexdigest())
                with zipfile.ZipFile(archive) as z:
                    self.assertEqual(z.read('solutions/lib/helper.py'), (root/'solutions/lib/helper.py').read_bytes())
                    self.assertEqual(json.loads(z.read('config.json'))['solutions'][0]['additional_files_names'], ['lib/helper.py', 'assets/bonus.txt'])
                    self.assertEqual(z.read('generators/assets/payload.txt'), payload)
                    self.assertEqual(z.read('tests/in/fixed.in'), payload)
                    export = Path('export'); z.extractall(export)
                result = subprocess.run(['bash', str(export/'check_with_solve.sh'), '--reproduce'],
                    capture_output=True, text=True, timeout=60,
                    env={'PATH': str(runtime_python().parent)+os.pathsep+os.defpath,
                         'LANG':'C.UTF-8', 'PACKER_TEST_SECRET':'synthetic'})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Validation and reproduction completed.', result.stdout)
                (root/'solutions/lib/helper.py').write_text('def echo(value): return 0\n')
                self.assertIn('solutions/lib/helper.py', changed_files(config))
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
