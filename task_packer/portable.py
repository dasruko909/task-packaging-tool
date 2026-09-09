"""Offline, isolated validation/reproduction entry point shipped with each ZIP."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from .sandbox import isolated


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    reproduce = '--reproduce' in sys.argv[2:]
    with tempfile.TemporaryDirectory(prefix='solve-portable-') as temporary:
        work = Path(temporary)
        package = work / 'package'
        for path in root.rglob('*'):
            if path.is_symlink():
                raise RuntimeError(f'Symlink in package: {path}')
        shutil.copytree(root, package, ignore=shutil.ignore_patterns('prepared_*', '__pycache__', 'verification'))
        program = r'''
import sys,json,hashlib,tempfile
from pathlib import Path
from libsolve.package import Package
root=Path(sys.argv[1]); recipe=json.loads((root/'generation-recipe.json').read_text()) if (root/'generation-recipe.json').exists() else {}
config=json.loads((root/'config.json').read_text())
if sys.argv[2]=='reproduce':
 config['test_generation']=recipe.get('test_generation',[])
 (root/'config.json').write_text(json.dumps(config))
p=Package('local',root); p.validate(); p.prepare(tmp_dir=tempfile.gettempdir())
if sys.argv[2]=='reproduce':
 p.generate_inputs(force=True,tmp_dir=tempfile.gettempdir())
 for name,wanted in recipe.get('inputs',{}).items():
  if hashlib.sha256((root/'tests/in'/name).read_bytes()).hexdigest()!=wanted:
   raise RuntimeError('Generator does not reproduce test '+name)
p.validate()
print('Validation and reproduction completed.' if sys.argv[2]=='reproduce' else 'Validation and build completed. Full judging results are available in the packer.')
'''
        command = isolated([sys.executable, '-c', program, str(package), 'reproduce' if reproduce else 'check'], work=work)
        subprocess.run(command, check=True, timeout=1200)


if __name__ == '__main__':
    main()
