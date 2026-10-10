"""Content certificates invalidate checks when any relevant artifact changes."""
import hashlib
import json
from pathlib import Path
from .paths import checked_tree, checked_path
from .storage import write_json


CERTIFICATE_VERSION = 2


def exported_path(relative: Path) -> bool:
    """The same content selection used by copy_package."""
    return (not any(part.startswith('prepared_') or part in {'__pycache__', 'verification'}
                    for part in relative.parts)
            and '.before-repair-' not in relative.name)


def fingerprints(root: Path) -> dict[str, str]:
    checked_tree(root)
    reports = {'report.md', 'report.json', 'certificate.json', 'counterexample.json',
               'coverage.json', 'solve-check.json', 'solve-check.log',
               'solve-validate.json', 'solve-validate.log'}
    paths = []
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        verification_material = (
            relative.parts[0] == 'verification'
            and relative.parts[1:2] != ('output-history',)
            and not (len(relative.parts) == 2 and path.name in reports)
            and '__pycache__' not in relative.parts
            and '.before-repair-' not in path.name
        )
        if path.is_file() and (exported_path(relative) or verification_material):
            paths.append(path)
    result = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(paths)}
    for module in Path(__file__).parent.glob('*.py'):
        result['__packer__/' + module.name] = hashlib.sha256(module.read_bytes()).hexdigest()
    return result


def certify(config, *, native: bool, local: bool = True, expected: dict | None = None) -> None:
    current = fingerprints(config.package_dir)
    if native:
        result_path = checked_path(config.package_dir / 'verification/solve-check.json')
        try:
            native_bytes = result_path.read_bytes()
            result = json.loads(native_bytes)
        except (OSError, ValueError) as error:
            raise RuntimeError('No current successful native verification result.') from error
        if (not isinstance(result, dict) or result.get('ok') is not True
                or result.get('source_sha256') != current):
            raise RuntimeError('Native verification is stale; rerun checks before certification.')
    if expected is not None and current != expected:
        raise RuntimeError('Materials changed during verification; rerun checks before certification.')
    certificate = {
        'version': CERTIFICATE_VERSION, 'sha256': current, 'local': local,
        'native': native, 'task_type': config.task_type,
    }
    native_result = checked_path(config.package_dir / 'verification/solve-check.json')
    if native and native_result.is_file():
        certificate['native_result_sha256'] = hashlib.sha256(native_bytes).hexdigest()
    write_json(config.package_dir / 'verification/certificate.json', certificate)


def changed_files(config) -> list[str]:
    path = checked_path(config.package_dir / 'verification/certificate.json')
    if not path.is_file():
        return ['no current verification certificate']
    try:
        previous = json.loads(path.read_text())
    except (ValueError, OSError):
        return ['unreadable verification certificate']
    if (not isinstance(previous, dict) or type(previous.get('version')) is not int
            or previous.get('version') != CERTIFICATE_VERSION
            or not isinstance(previous.get('sha256'), dict)
            or not previous['sha256']
            or any(not isinstance(name, str) or not isinstance(digest, str)
                   or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
                   for name, digest in previous['sha256'].items())
            or type(previous.get('local')) is not bool
            or type(previous.get('native')) is not bool
            or previous.get('native') and (
                not isinstance(previous.get('native_result_sha256'), str)
                or len(previous['native_result_sha256']) != 64
                or any(c not in '0123456789abcdef' for c in previous['native_result_sha256']))):
        return ['invalid or outdated verification certificate; rerun checks']
    if not previous.get('local') or previous.get('task_type') != config.task_type:
        return ['required local verification is missing']
    now, old = fingerprints(config.package_dir), previous.get('sha256', {})
    return sorted(name for name in now.keys() | old.keys() if now.get(name) != old.get(name))


def certified_native_result(config) -> dict | None:
    """Return a successful Solve result only when its certificate is still exact."""
    if changed_files(config):
        return None
    certificate_path = checked_path(config.package_dir / 'verification/certificate.json')
    result_path = checked_path(config.package_dir / 'verification/solve-check.json')
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
    return result if isinstance(result, dict) and result.get('ok') is True else None
