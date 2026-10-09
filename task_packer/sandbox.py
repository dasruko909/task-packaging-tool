"""Bubblewrap isolation: no host home, no network, explicit read/write mounts."""
from __future__ import annotations
import os
from pathlib import Path
import shutil


def isolated(command: list[str], *, work: Path, readable=(), env: dict | None = None,
             cwd: Path | None = None, writable_work: bool = True, writable=()) -> list[str]:
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
    mounts.extend(Path(p).absolute() for p in readable if Path(p).exists())
    work = work.resolve()
    cwd = (cwd or work).resolve()
    writable = [Path(path).resolve() for path in writable if Path(path).exists()]
    visible = [work, *mounts, *writable]
    if not any(cwd == path or (path.is_dir() and cwd.is_relative_to(path)) for path in visible):
        raise ValueError(f'Sandbox working directory is not mounted: {cwd}')
    for path in sorted(set(mounts), key=lambda p: (len(p.parts), str(p))):
        args += ['--ro-bind', str(path), str(path)]
    args += ['--bind' if writable_work else '--ro-bind', str(work), str(work)]
    for path in sorted(set(writable), key=lambda p: (len(p.parts), str(p))):
        args += ['--bind', str(path), str(path)]
    args += ['--chdir', str(cwd)]
    values = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'HOME': '/tmp', 'TMPDIR': '/tmp'}
    values.update(env or {})
    for key, value in values.items():
        args += ['--setenv', key, value]
    return args + ['--'] + command
