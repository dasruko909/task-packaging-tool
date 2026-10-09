"""Run the CLI in separate processes against disposable synthetic states."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

entry = Path(__file__).resolve().parents[1] / 'packer.py'
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    for code, data in [('good', {'config': None}),
                       ('bad', {'config': {'codename': 'bad'}})]:
        path = root / '.packer-projects' / code / 'state.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(data), encoding='utf-8')
    before = {path: path.read_bytes() for path in root.glob('.packer-projects/*/state.json')}
    cases = [(['--help'], 0), (['--status'], 0),
             (['--project', 'good', '--history'], 0),
             (['--project', 'bad', '--history'], 1)]
    for args, expected in cases:
        result = subprocess.run([sys.executable, str(entry), *args], cwd=root,
                                capture_output=True, text=True)
        assert result.returncode == expected, (args, result.stderr)
        assert 'Traceback' not in result.stderr, result.stderr
        if args == ['--status']:
            assert 'requires repair' in result.stdout
            assert 'configuration in progress' in result.stdout
        if expected == 1:
            assert '.packer-projects/bad/state.json' in result.stderr
        print('PASS:', ' '.join(args), 'exit=', result.returncode)
    for path, content in before.items():
        assert path.read_bytes() == content
    assert not (root / 'input').exists()
    assert not (root / 'output').exists()
