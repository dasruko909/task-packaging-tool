"""Content certificates invalidate checks when any relevant artifact changes."""
import hashlib
import json
from pathlib import Path
from .storage import write_json


def fingerprints(root: Path) -> dict[str, str]:
    paths = [root / 'config.json']
    for folder in ('description', 'editorial', 'solutions', 'generators', 'checker', 'public', 'tests', 'validators'):
        paths.extend((root / folder).rglob('*'))
    verification = root / 'verification'
    paths.extend(
        p for p in verification.rglob('*')
        if p.is_file()
        and 'output-history' not in p.relative_to(verification).parts
        and p.name not in {'report.md', 'report.json', 'certificate.json',
                           'counterexample.json', 'coverage.json'}
        and not p.name.startswith('solve-')
    )
    result = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(set(paths)) if p.is_file() and not any(part.startswith(('prepared_', '__pycache__')) for part in p.parts)
            and '.before-repair-' not in p.name and p.suffix not in {'.pdf', '.html'}}
    for module in Path(__file__).parent.glob('*.py'):
        result['__packer__/' + module.name] = hashlib.sha256(module.read_bytes()).hexdigest()
    return result


def certify(config, *, native: bool, local: bool = True) -> None:
    certificate = {
        'sha256': fingerprints(config.package_dir), 'local': local,
        'native': native, 'task_type': config.task_type,
    }
    native_result = config.package_dir / 'verification/solve-check.json'
    if native and native_result.is_file():
        certificate['native_result_sha256'] = hashlib.sha256(native_result.read_bytes()).hexdigest()
    write_json(config.package_dir / 'verification/certificate.json', certificate)


def changed_files(config) -> list[str]:
    path = config.package_dir / 'verification/certificate.json'
    if not path.is_file():
        return ['no current verification certificate']
    try:
        previous = json.loads(path.read_text())
    except (ValueError, OSError):
        return ['unreadable verification certificate']
    if not previous.get('local') or previous.get('task_type') != config.task_type:
        return ['required local verification is missing']
    now, old = fingerprints(config.package_dir), previous.get('sha256', {})
    return sorted(name for name in now.keys() | old.keys() if now.get(name) != old.get(name))


def certified_native_result(config) -> dict | None:
    """Return a successful Solve result only when its certificate is still exact."""
    if changed_files(config):
        return None
    certificate_path = config.package_dir / 'verification/certificate.json'
    result_path = config.package_dir / 'verification/solve-check.json'
    try:
        certificate = json.loads(certificate_path.read_text())
        result_bytes = result_path.read_bytes()
        result = json.loads(result_bytes)
    except (OSError, ValueError, TypeError):
        return None
    expected = certificate.get('native_result_sha256')
    if not certificate.get('native') or not expected:
        return None
    if hashlib.sha256(result_bytes).hexdigest() != expected:
        return None
    return result if result.get('ok') is True else None
