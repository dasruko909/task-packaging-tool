"""Revision history and explicit dependencies shared by CLI and review menus."""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .storage import atomic_write_text, write_json


class RestartWorkflow(Exception):
    """Return to the first invalidated stage, rebuilding prompts from current config."""


SCOPES = ('rules', 'task_type', 'specification', 'test_plan', 'statement', 'statement_audit',
          'checker', 'interactor', 'generators', 'corners', 'solutions',
          'verification_materials', 'editorial', 'outputs')


def affected(target: str, key: str) -> bool:
    if target in {'rules', 'task_type', 'specification'}:
        return key != 'import' and (key != 'task_type' or target == 'task_type')
    if target == 'test_plan':
        return key not in {'import', 'task_type', 'specification'}
    if key == target or key == 'finalize' or key.startswith('repair:'):
        return True
    if target == 'statement':
        return key in {'statement_audit', 'editorial'}
    if target in {'checker', 'interactor'}:
        return key in {'verification_materials', 'editorial'}
    if target == 'solutions' or target.startswith('solution:'):
        return (key == 'editorial' or
                (key.startswith(('solution:', 'critique:')) and
                 (target == 'solutions' or key.split(':')[1] == target.split(':')[1])))
    if target.startswith('critique:'):
        return key == 'editorial'
    if target in {'generators', 'corners'}:
        return key.startswith('generator:' if target == 'generators' else 'corners:')
    return False


def validate_target(state, target):
    keys = {f'{prefix}:{s.index}' for s in state.config.subtasks
            for prefix in ('solution', 'critique', 'generator', 'corners')}
    if target not in SCOPES and target not in keys:
        raise ValueError('Unknown stage: ' + target)


def invalidate(state, target: str) -> None:
    validate_target(state, target)
    if affected(target, 'statement_audit'):
        state.setup.pop('statement_auto_repair', None)
    pending = set(state.setup.get('review_pending', []))
    pending.update(k for k in state.completed if affected(target, k) and k not in {'import', 'finalize'} and not k.startswith('repair:'))
    # Old task-type-specific stages may disappear entirely after a type change.
    if target in {'rules', 'task_type', 'specification', 'test_plan'}:
        pending = {'specification' if target != 'test_plan' else 'statement'}
    state.setup['review_pending'] = sorted(pending)
    for name in ('completed',):
        setattr(state, name, [key for key in getattr(state, name) if not affected(target, key)])
    for name in ('drafts', 'feedback', 'previous_drafts'):
        setattr(state, name, {key: value for key, value in getattr(state, name).items()
                              if not affected(target, key)})
    for key in list(state.setup):
        if key.startswith(('request:', 'manual:', 'format_retries:')) and affected(target, key.split(':', 1)[1]):
            state.setup.pop(key)
    state.setup['accepted'] = {k: v for k, v in state.setup.get('accepted', {}).items() if not affected(target, k)}
    state.setup['approvals'] = [k for k in state.setup.get('approvals', []) if not affected(target, k)]
    state.finished = False
    config = state.config
    if target in {'rules', 'task_type', 'specification', 'test_plan'}:
        config.test_plan = {}
        state.setup.pop('preserve_samples', None)
        config.solution_files = {}
        config.sample_files = []
        config.existing_tests = False
        # Do not re-import old tests, checker or group weights after semantic edits.
        config.input_package = ''
        state.setup.pop('custom_brutes', None)
        for subtask in config.subtasks:
            subtask.group_name = ''
    if target in {'rules', 'task_type', 'specification'}:
        config.specification = {}
        state.setup.pop('custom_brutes', None)
        state.setup.pop('missing_details', None)
        state.setup.pop('clarification_draft', None)
    if target == 'task_type':
        config.task_type = 'auto'


def snapshot(store, key: str, text: str) -> None:
    name = re.sub(r'[^a-zA-Z0-9_.-]', '_', key)
    atomic_write_text(store.path.parent / 'history' / name / f'{time.time_ns()}.txt', text)


def checkpoint(state, store, reason: str) -> Path:
    """Copy before any mutation; incomplete copies are not offered for restoration."""
    path = store.path.parent / 'history' / 'checkpoints' / str(time.time_ns())
    path.mkdir(parents=True)
    root = state.config.package_dir
    if root.exists():
        shutil.copytree(root, path / 'package', symlinks=True)
    for name in ('notes', 'specification.json', 'statement-audit.json'):
        source = store.path.parent / name
        if source.is_dir():
            shutil.copytree(source, path / name, symlinks=True)
        elif source.is_file():
            shutil.copy2(source, path / name)
    write_json(path / 'state.json', state.to_dict())
    write_json(path / 'checkpoint.json', {'reason': reason, 'complete': True})
    return path


def _remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def cleanup(state, store):
    """Idempotent cleanup; a crash is resumed before the next workflow action."""
    target = state.setup.get('pending_cleanup')
    if not target:
        return
    root = state.config.package_dir
    broad = target in {'rules', 'task_type', 'specification', 'test_plan'}
    paths = [root / 'verification/certificate.json', root / 'verification/report.json',
             root / 'verification/report.md']
    paths += list(root.glob('prepared_*')) + list(root.glob('*.zip'))
    paths += list((root / 'verification').glob('solve-*'))
    # archive_package stores the distributable next to the package directory.
    paths += [root.parent / (state.config.codename + '.zip'), root.parent / (state.config.codename + '.zip.sha256')]
    if broad:
        paths += [root / name for name in ('tests', 'generators', 'solutions', 'checker',
                                           'public', 'validators', 'verification', 'editorial', 'config.json')]
        paths += list((root / 'description').glob('*.md'))
    elif target == 'verification_materials':
        paths += [root / 'validators', root / 'verification']
    elif target in {'checker', 'interactor'}:
        paths += [root / 'checker', root / 'public', root / 'validators', root / 'verification']
    if affected(target, 'statement_audit'):
        paths.append(store.path.parent / 'statement-audit.json')
        state.setup.pop('audit_digest', None)
    if affected(target, 'editorial'):
        paths.append(root / 'editorial')
    # Stale corner files otherwise survive when the new list is shorter.
    if target == 'corners' or target.startswith('corners:'):
        from .statement_format import MAX_TESTS_PER_SUBTASK, test_filename
        from .test_plan import counts_for
        plan = state.config.test_plan if isinstance(state.config.test_plan, dict) else {}
        for item in state.config.subtasks:
            if target == 'corners' or target == f'corners:{item.index}':
                counts = counts_for(plan, item.index)
                generator_runs = counts[0] if counts is not None else 3
                for folder in ('in', 'out'):
                    extension = folder
                    paths += [
                        root / 'tests' / folder / test_filename(item.index, offset, extension)
                        for offset in range(min(generator_runs, MAX_TESTS_PER_SUBTASK), MAX_TESTS_PER_SUBTASK)
                    ]
                    # Compatibility with files created by older packer versions.
                    paths += list((root / 'tests' / folder).glob(f'{state.config.codename}.{item.index:02d}m[0-9]*'))
    for path in paths:
        _remove(path)
    state.setup.pop('pending_cleanup', None)
    store.save(state)


def revise(state, store, target: str, feedback: str = '', *, reuse: bool = True,
           updates: dict | None = None, setup_updates: dict | None = None) -> None:
    if state.config.existing_tests and (target in {'generators', 'corners'} or target.startswith(('generator:', 'corners:'))):
        print('The imported package has fixed tests; opening a new test-generation plan first.')
        target = 'test_plan'
    if target.startswith('repair:'):
        filename = target.split(':')[1]
        from .registry import solution_path
        target = next((f'solution:{s.index}' for s in state.config.subtasks
                       if solution_path(state.config, s).name == filename),
                      'checker' if filename == 'checker.cpp' else
                      'interactor' if filename == 'interactor.cpp' else 'verification_materials')
    validate_target(state, target)
    previous = dict(state.setup.get('accepted', {}))
    previous.update(state.drafts)
    if target == 'specification' and not previous.get(target) and state.config.specification:
        previous[target] = json.dumps(state.config.specification, ensure_ascii=False)
    if target == 'test_plan' and not previous.get(target) and state.config.test_plan:
        previous[target] = json.dumps(state.config.test_plan, ensure_ascii=False)
    # Older state files predate the accepted-draft register.
    from .registry import solution_path
    for item in state.config.subtasks:
        key = f'solution:{item.index}'
        path = solution_path(state.config, item)
        if path.is_file():
            previous.setdefault(key, json.dumps({'code': path.read_text(),
                                'description': 'Previously approved solution.'}, ensure_ascii=False))
    statement_path = state.config.package_dir / 'description' / f'{state.config.language_code}.md'
    if target == 'statement' and statement_path.is_file():
        previous['statement'] = json.dumps({'statement_markdown': statement_path.read_text(), 'tests': []}, ensure_ascii=False)
    backup = checkpoint(state, store, target + (': ' + feedback if feedback else ''))
    invalidate(state, target)
    if target == 'statement' and statement_path.is_file():
        from .statement_format import example_references
        refs = example_references(statement_path.read_text())
        if state.config.task_type == 'interactive':
            state.setup['preserve_samples'] = True
        elif refs and all((state.config.package_dir / 'tests' / folder / name).is_file()
                        for inp, out, _ in refs for folder, name in (('in', inp), ('out', out))):
            state.setup['preserve_samples'] = True
        elif not state.config.existing_tests:
            previous.pop('statement', None)
    if target == 'rules' and feedback:
        state.config.judge_notes += '\nRule change approved by the author:\n' + feedback
    keys = ([f'solution:{s.index}' for s in state.config.subtasks] if target == 'solutions' else [target])
    for key in keys:
        if reuse and key in previous and target not in {'rules', 'task_type'}:
            if feedback:
                state.previous_drafts[key] = previous[key]
            else:
                state.drafts[key] = previous[key]
        if feedback and target != 'rules':
            state.feedback[key] = feedback
    for key, value in (updates or {}).items():
        setattr(state.config, key, value)
    if target == 'task_type' and state.config.task_type in {'multiple', 'interactive'}:
        state.setup['missing_details'] = ['Provide a complete judging or protocol specification for the new task type.']
    state.setup['pending_cleanup'] = target
    state.setup.update(setup_updates or {})
    store.save(state)
    cleanup(state, store)
    print(f'History saved: {backup.resolve()}')


def history(store):
    result = []
    for path in sorted((store.path.parent / 'history/checkpoints').glob('*')):
        if (path / 'checkpoint.json').is_file():
            result.append(path)
    return result


def restore(state, store, identifier: str):
    from .models import WorkflowState
    paths = history(store)
    path = next((p for p in paths if p.name == identifier), None)
    if path is None:
        raise ValueError('No such version exists in history.')
    restored = WorkflowState.from_dict(json.loads((path / 'state.json').read_text()))
    if restored.config.codename != state.config.codename:
        raise ValueError('This history belongs to another project.')
    checkpoint(state, store, 'before restoring ' + identifier)
    restored.usage = state.usage.copy()  # restoring content never refunds API usage
    restored.finished = False
    restored.completed = [key for key in restored.completed if key != 'finalize']
    restored.setup['pending_cleanup'] = 'outputs'
    root = state.config.package_dir
    transaction = (store.path.parent / '.restore-work').resolve()
    journal = store.path.parent / '.restore-transaction.json'
    if transaction.exists():
        shutil.rmtree(transaction)
    staged = transaction / 'staged'
    originals = transaction / 'originals'
    staged.mkdir(parents=True)
    mappings = [(root, path / 'package', 'package')]
    mappings.extend(
        (store.path.parent / name, path / name, name)
        for name in ('notes', 'specification.json', 'statement-audit.json')
    )
    for _, source, name in mappings:
        if source.is_symlink():
            raise RuntimeError(f'History contains a forbidden symbolic link: {source}')
        if source.is_dir():
            if any(item.is_symlink() for item in source.rglob('*')):
                raise RuntimeError(f'History contains a forbidden symbolic link: {source}')
            shutil.copytree(source, staged / name)
        elif source.is_file():
            shutil.copy2(source, staged / name)
    serialized = json.dumps(restored.to_dict(), ensure_ascii=False, indent=2) + '\n'
    import hashlib
    assets = [
        {
            'destination': str(destination.resolve()),
            'original': str((originals / name).resolve()),
            'staged': str((staged / name).resolve()),
        }
        for destination, _, name in mappings
    ]
    write_json(journal, {
        'transaction': str(transaction),
        'expected_state_sha256': hashlib.sha256(serialized.encode()).hexdigest(),
        'assets': assets,
    })
    try:
        originals.mkdir(parents=True)
        for asset in assets:
            destination = Path(asset['destination'])
            original = Path(asset['original'])
            replacement = Path(asset['staged'])
            if destination.exists() or destination.is_symlink():
                shutil.move(str(destination), str(original))
            if replacement.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(replacement), str(destination))
        store.save(restored)
    except BaseException:
        store._recover_restore()
        raise
    store._recover_restore()
    from dataclasses import fields
    for field in fields(state):
        setattr(state, field.name, getattr(restored, field.name))
    cleanup(state, store)


def edit_menu(state, store) -> bool:
    from .console import ProjectDeleted, ask, ask_int, ask_multiline, choose
    action = choose('All task actions:', {
        'e': 'open any stage for another review',
        'u': 'undo the last approval',
        'r': 'change rules, data format, or variable constraints',
        'l': 'change time and memory limits',
        's': 'change subtasks and scoring',
        't': 'change task type',
        'i': 'change the narrative or statement style',
        'm': 'change task title or source',
        'h': 'show history and restore a version',
        'b': 'add custom brute force',
        'd': 'delete task (recoverable)',
        'q': 'return without changes',
    }, 'q')
    if action == 'q':
        return False
    if action == 'd':
        print(
            f"Project {store.codename!r}, its input/ directory, output/, and ZIP "
            "will disappear from the current list. Data moves to .packer-trash and can be recovered."
        )
        confirmation = ask(f"To delete, type the exact project code: {store.codename}")
        if confirmation != store.codename:
            print('The code does not match. Project was not changed.')
            return False
        from .storage import delete_project
        archive = delete_project(store.codename)
        raise ProjectDeleted(str(archive))
    if action == 'h':
        paths = history(store)
        for i, path in enumerate(paths, 1):
            print(f"{i}. {path.name}: {json.loads((path / 'checkpoint.json').read_text())['reason']}")
        selected = ask_int('Version number to restore (0 = return)', default=0, minimum=0,
                           validator=lambda n: n <= len(paths))
        if not selected:
            return False
        if choose('Restore this version? The current version will be kept in history.', {'t': 'yes', 'n': 'no'}, 'n') != 't':
            return False
        restore(state, store, paths[selected - 1].name)
        return True
    if action == 'u':
        keys = [k for k in state.setup.get('approvals', state.completed) if k not in {'import', 'finalize'}]
        if not keys:
            print('There are no approvals to undo.')
            return False
        target = keys[-1]
    elif action == 'e':
        keys = list(dict.fromkeys([k for k in state.completed if k not in {'import', 'finalize'}] + list(SCOPES)))
        for i, key in enumerate(keys, 1):
            print(f'{i}. {key}')
        selected = ask_int('Stage (0 = return)', default=0, minimum=0, validator=lambda n: n <= len(keys))
        if not selected:
            return False
        target = keys[selected - 1]
        if target in {'rules', 'task_type'}:
            action = 'r' if target == 'rules' else 't'
    else:
        target = {'r': 'rules', 'l': 'statement', 's': 'rules', 't': 'task_type',
                  'i': 'statement', 'm': 'outputs', 'b': 'verification_materials'}[action]
    changes = ''
    updates = {}
    if action == 'r':
        def save(value):
            state.setup['rules_change'] = value
            store.save(state)
        changes = ask_multiline('Describe the new rules, variable ranges, data format, or constraints.',
                                initial=state.setup.get('rules_change', ''), on_change=save)
        if not changes.strip():
            return False
    if action == 'l':
        updates['time_limit_ms'] = ask_int('Time limit in ms', default=state.config.time_limit_ms, minimum=1)
        updates['memory_limit_kb'] = ask_int('Memory limit in KiB', default=state.config.memory_limit_kb, minimum=1)
    if action == 's':
        from .models import Subtask
        items = []
        count = ask_int('Number of subtasks', default=len(state.config.subtasks), minimum=1)
        for i in range(1, count + 1):
            old = state.config.subtasks[i - 1] if i <= len(state.config.subtasks) else None
            items.append(Subtask(i, ask(f'Subtask name {i}', old.name if old else ''),
                ask_int('Points', default=old.points if old else None, minimum=0),
                ask(f'Subtask constraints {i}', old.constraints if old else '')))
        if sum(s.points for s in items) != 100:
            print('Points must total 100. The change was not saved.')
            return False
        updates['subtasks'] = items
    if action == 't':
        updates['task_type'] = choose('New task type', {'auto': 'detect again', 'standard': 'standard',
                                         'multiple': 'multiple answers', 'interactive': 'interactive'}, 'auto')
    if action == 'i':
        updates['statement_idea'] = ask(
            'New narrative or style idea (one line)',
            state.config.statement_idea,
        )
    if action == 'm':
        updates['title'] = ask('Task title', state.config.title)
        updates['origin'] = ask('Task source', state.config.origin)
    if action == 'b':
        return edit_brutes(state, store)
    impacted = [k for k in state.completed if affected(target, k)]
    print('Needs another approval or run: ' + ', '.join(impacted or [target]))
    if choose('Apply the change? Previous files will be retained in history.', {'t': 'yes', 'n': 'no'}, 'n') != 't':
        return False
    revise(state, store, target, changes, updates=updates)
    state.setup.pop('rules_change', None)
    store.save(state)
    return True


def edit_brutes(state, store):
    from .console import ask, ask_multiline, choose
    if state.config.task_type == 'interactive':
        print('For interactions, edit clients in the verification_materials stage.')
        return False
    mode = choose('Brute force', {'w': 'paste C++17 code', 'f': 'load a C++ file', 'q': 'return'}, 'q')
    if mode == 'q':
        return False
    if mode == 'f':
        code = Path(ask('File path')).expanduser().read_text(encoding='utf-8')
    else:
        def save(value):
            state.setup['brute_draft'] = value
            store.save(state)
        code = ask_multiline('Custom C++17 brute force: stdin input, stdout output.',
                            initial=state.setup.get('brute_draft', ''), on_change=save)
    if not code.strip():
        return False
    print(code)
    if choose('Add this brute force to independent comparisons?', {'t': 'yes', 'n': 'no'}, 'n') != 't':
        return False
    checkpoint(state, store, 'custom brute force')
    state.setup.setdefault('custom_brutes', []).append(code)
    state.setup.pop('brute_draft', None)
    invalidate(state, 'outputs')
    state.setup['pending_cleanup'] = 'outputs'
    store.save(state)
    cleanup(state, store)
    return True
