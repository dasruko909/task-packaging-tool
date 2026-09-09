"""Bubblewrap isolation: no host home, no network, explicit read/write mounts."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import sys


def isolated(command: list[str], *, work: Path, readable=(), env: dict | None = None) -> list[str]:
    binary = shutil.which('bwrap')
    if binary is None:
        raise RuntimeError('bubblewrap (bwrap) is missing. Install the bubblewrap package; execution without isolation is disabled.')
    args = [binary, '--unshare-all', '--die-with-parent', '--new-session', '--clearenv',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    mounts = []
    for name in ('/usr', '/bin', '/sbin', '/lib', '/lib64'):
        path = Path(name)
        if path.is_symlink():
            args += ['--symlink', os.readlink(path), name]
        elif path.exists():
            mounts.append(path)
    cache = Path('/etc/ld.so.cache')
    if cache.is_file():
        mounts.append(cache)
    if Path('/etc/alternatives').is_dir():
        mounts.append(Path('/etc/alternatives'))
    # The virtualenv contains runtime dependencies, never account credentials.
    if sys.prefix != sys.base_prefix:
        mounts.append(Path(sys.prefix))
    for folder in ('bin', 'lib', 'lib64', 'include'):
        path = Path(sys.base_prefix) / folder
        if path.exists() and not str(path).startswith('/usr/'):
            mounts.append(path)
    mounts.extend(Path(p).absolute() for p in readable if Path(p).exists())
    work = work.resolve()
    for path in sorted(set(mounts), key=lambda p: (len(p.parts), str(p))):
        args += ['--ro-bind', str(path), str(path)]
    args += ['--bind', str(work), str(work), '--chdir', str(work)]
    values = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'HOME': '/tmp', 'TMPDIR': '/tmp'}
    values.update(env or {})
    for key, value in values.items():
        args += ['--setenv', key, value]
    return args + ['--'] + command
