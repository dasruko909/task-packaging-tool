"""Filesystem regressions: synthetic data only, no API or Solve connection."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from task_packer import cli
from task_packer.console import SavedExit
from task_packer.input_sources import (
    _copy_tree_without_symlinks, import_existing_materials, input_dir,
    statement_attachments, validate_pdf,
)
from task_packer.models import ProjectConfig, Subtask, WorkflowState
from task_packer.onboarding import gather_config
from task_packer.paths import project_code
from task_packer.registry import load_manifest, safe_file
from task_packer.workflow import Workflow
from task_packer.freshness import fingerprints
from task_packer.revisions import checkpoint, cleanup, restore
from task_packer.solve4 import create_package_skeleton
from task_packer.solve_menu import download_for_editing
from task_packer.solve_native import archive_package, copy_package
from task_packer.storage import (
    StateReadError, StateStore, acquire_project_lock, atomic_write_text, delete_project,
)


class SafePathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        self.base = Path(self.temp.name)
        self.work = self.base / 'workspace'
        self.work.mkdir()
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        self.sentinel = self.outside / 'sentinel'
        self.original = b'SYNTHETIC SENTINEL\x00\xff\n'
        self.sentinel.write_bytes(self.original)
        os.chdir(self.work)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, self.previous)
        self.addCleanup(lambda: self.assertEqual(self.sentinel.read_bytes(), self.original))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.api = stack.enter_context(patch('task_packer.openai_client.OpenAIClient',
                                            side_effect=AssertionError('No API')))
        self.network = stack.enter_context(patch('task_packer.solve_menu.read_connection',
                                                side_effect=AssertionError('No Solve connection')))
        acquire = StateStore.acquire_lock

        def acquire_for_test(store):
            lock = acquire(store)
            self.addCleanup(lock.close)
            return lock

        stack.enter_context(patch.object(StateStore, 'acquire_lock', acquire_for_test))

    def config(self, code='safe', **updates):
        values = dict(codename=code, title='Synthetic', origin='test', language_code='en',
                      original_statement='Synthetic statement', subtasks=[Subtask(1, 'Full', 100, '')])
        values.update(updates)
        return ProjectConfig(**values)

    def write(self, path, content=b'synthetic'):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def raw_state(self, data, code='safe'):
        return self.write(Path('.packer-projects') / code / 'state.json', json.dumps(data).encode())

    def conflict(self, code, kind):
        path = {'state': Path('.packer-projects') / code / 'state.json',
                'directory': Path('output') / code / 'keep',
                'zip': Path('output') / f'{code}.zip',
                'sha256': Path('output') / f'{code}.zip.sha256'}[kind]
        return self.write(path, b'Existing bytes must survive')

    def test_creation_refuses_every_conflict_before_material_writes(self):
        for mode in ('form', 'download'):
            for kind in ('state', 'directory', 'zip', 'sha256'):
                code = f'{mode}-{kind}'
                with self.subTest(mode=mode, kind=kind):
                    existing = self.conflict(code, kind)
                    before = existing.read_bytes()
                    with patch('task_packer.onboarding.ask', return_value=code), self.assertRaisesRegex(RuntimeError, 'already exists'):
                        gather_config() if mode == 'form' else download_for_editing(code)
                    self.assertEqual(existing.read_bytes(), before)
                    self.assertFalse((Path('input') / code).exists())
                    if kind != 'state':
                        self.assertFalse(StateStore(code).path.exists())
        self.network.assert_not_called()

    def test_conflicts_created_during_lock_acquisition_are_rechecked(self):
        acquire = StateStore.acquire_lock
        for mode in ('form', 'download'):
            for kind in ('state', 'directory', 'zip', 'sha256'):
                code = f'{mode}-{kind}'
                created = []

                def race(store):
                    lock = acquire(store)
                    created.append(self.conflict(code, kind))
                    return lock

                with self.subTest(mode=mode, kind=kind), patch.object(StateStore, 'acquire_lock', race), patch('task_packer.onboarding.ask', return_value=code):
                    with self.assertRaisesRegex(RuntimeError, 'already exists'):
                        gather_config() if mode == 'form' else download_for_editing(code)
                    self.assertEqual(created[0].read_bytes(), b'Existing bytes must survive')
                    self.assertFalse((Path('input') / code).exists())
                    if kind != 'state':
                        self.assertFalse(StateStore(code).path.exists())
        self.network.assert_not_called()

    def test_prefilled_input_is_legal_for_new_project(self):
        prepared = self.write('input/safe/package/tests/in/1a.in', b'123\n')
        with patch('task_packer.onboarding.ask', return_value='safe'), patch('task_packer.onboarding.ask_yes_no', side_effect=SavedExit):
            with self.assertRaises(SavedExit):
                gather_config()
        self.assertEqual(prepared.read_bytes(), b'123\n')
        self.assertEqual(StateStore('safe').load().setup['codename'], 'safe')
        self.assertFalse(Path('output').exists())

    def test_download_keeps_nonempty_input_without_network(self):
        prepared = self.write('input/safe/package/tests/in/1a.in')
        with self.assertRaisesRegex(RuntimeError, 'not empty'):
            download_for_editing('safe')
        self.assertEqual(prepared.read_bytes(), b'synthetic')
        self.network.assert_not_called()

    def test_downloaded_language_checked_before_copy_with_synthetic_download(self):
        for index, language in enumerate((' PL ', 'EN', None, '../bad', str(self.sentinel), 'de')):
            code = f'download{index}'

            def fake_download(command, **kwargs):
                package = Path(command[command.index('-p') + 1])
                manifest = {'title': {'pl': 'Synthetic'}}
                if language is not None:
                    manifest['default_language'] = language
                self.write(package / 'config.json', json.dumps(manifest).encode())
                self.write(package / 'description/pl.md', b'Synthetic statement')
                return SimpleNamespace(returncode=0, stdout='')

            with self.subTest(language=language), contextlib.ExitStack() as stack:
                stack.enter_context(patch('task_packer.solve_menu.doctor', return_value={'ready': True}))
                stack.enter_context(patch('task_packer.solve_menu.read_connection', return_value={'host': 'https://synthetic.invalid', 'token': 'synthetic'}))
                stack.enter_context(patch('task_packer.solve_menu.subprocess.run', side_effect=fake_download))
                if language in (' PL ', 'EN', None):
                    store = download_for_editing(code)
                    self.assertEqual(store.load().setup['language_code'], 'pl')
                else:
                    with self.assertRaises(ValueError):
                        download_for_editing(code)
                    self.assertFalse((Path('input') / code).exists())
                    self.assertFalse(StateStore(code).path.exists())

    def test_project_codes_shared_by_cli_and_filesystem_operations(self):
        for code in ('../outside', str(self.outside), 'UPPER', '', 'a' * 21, 'ż', r'a\b'):
            with self.subTest(code=code):
                for operation in (StateStore, acquire_project_lock, input_dir, delete_project,
                                  download_for_editing, lambda value: self.config(value).package_dir):
                    with self.assertRaises(ValueError):
                        operation(code)
                with patch('sys.argv', ['packer.py', '--project', code]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    cli.main()
        for code in ('a', 'a_z-09', 'a' * 20):
            self.assertEqual(project_code(code), code)
        self.assertFalse(Path('.packer-projects').exists())
        self.assertFalse(Path('input').exists())
        self.assertFalse(Path('output').exists())

    def test_language_normalization_on_load_without_rewrite(self):
        for value in ('pl', 'en', 'PL', 'EN', '  Pl  ', '\tEN\n'):
            for configured in (True, False):
                with self.subTest(value=value, configured=configured):
                    data = WorkflowState(config=self.config()).to_dict() if configured else {'setup': {'codename': 'safe'}}
                    data['config' if configured else 'setup']['language_code'] = value
                    path = self.raw_state(data)
                    before = path.read_bytes()
                    state = StateStore('safe').load()
                    actual = state.config.language_code if configured else state.setup['language_code']
                    self.assertEqual(actual, value.strip().lower())
                    self.assertEqual(path.read_bytes(), before)

    def test_invalid_saved_languages_report_path_and_preserve_state(self):
        for value in ('de', '../outside', str(self.sentinel), 'pl/en', r'pl\en', ''):
            for configured in (True, False):
                with self.subTest(value=value, configured=configured):
                    data = WorkflowState(config=self.config()).to_dict() if configured else {'setup': {'codename': 'safe'}}
                    data['config' if configured else 'setup']['language_code'] = value
                    path = self.raw_state(data)
                    before = path.read_bytes()
                    with self.assertRaisesRegex(StateReadError, r'safe/state.json:.*language_code'):
                        StateStore('safe').load()
                    self.assertEqual(path.read_bytes(), before)

    def test_form_normalizes_language_and_retries_invalid_input(self):
        for index, value in enumerate(('pl', 'en', 'PL', ' EN ')):
            code = f'form{index}'
            setup = dict(codename=code, has_package=False, statement_source='paste',
                         original_statement='Synthetic', statement_idea='', has_images=False,
                         title='Synthetic', origin='test', time_limit_ms=2000, memory_limit_mb=256,
                         subtask_mode='manual', subtask_count=1,
                         subtasks=[{'name': 'Full', 'points': 100, 'constraints': ''}])
            state, store = WorkflowState(setup=setup), StateStore(code)
            answers = iter(('../bad', value))

            def answer(prompt, default=''):
                return next(answers) if prompt == 'Statement language code' else default

            with patch('task_packer.onboarding.ask', side_effect=answer):
                config, _ = gather_config(state, store)
            self.assertEqual(config.language_code, value.strip().lower())
            self.assertEqual(store.load().config.language_code, value.strip().lower())

    def test_foreign_or_invalid_state_codes_cannot_redirect_writes(self):
        for field in ('config', 'setup'):
            for code in ('other', '../outside', str(self.outside), None):
                with self.subTest(field=field, code=code):
                    data = WorkflowState(config=self.config()).to_dict()
                    data[field]['codename'] = code
                    path = self.raw_state(data)
                    before = path.read_bytes()
                    with self.assertRaises(StateReadError):
                        StateStore('safe').load()
                    self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            StateStore('safe').save(WorkflowState(config=self.config('other')))
        self.assertFalse(Path('output').exists())

    def test_symlink_roots_and_lock_are_rejected_before_changes(self):
        for root in ('.packer-projects', 'input', 'output'):
            with self.subTest(root=root):
                link = Path(root)
                link.symlink_to(self.outside, target_is_directory=True)
                try:
                    with self.assertRaisesRegex(ValueError, 'Symbolic link'):
                        acquire_project_lock('safe')
                    with self.assertRaises(ValueError):
                        delete_project('safe')
                    self.assertEqual(list(self.outside.iterdir()), [self.sentinel])
                finally:
                    link.unlink()
        lock = Path('.packer-projects/safe/.lock')
        lock.parent.mkdir(parents=True)
        lock.symlink_to(self.sentinel)
        with self.assertRaisesRegex(ValueError, r'\.lock'):
            acquire_project_lock('safe')
        self.assertEqual(self.sentinel.read_bytes(), self.original)

    def test_symlink_file_and_intermediate_directory_block_read_write_copy_delete(self):
        root = Path('output/safe')
        root.mkdir(parents=True)
        for directory_link in (False, True):
            link = root / ('tests' if directory_link else 'config.json')
            link.symlink_to(self.outside if directory_link else self.sentinel,
                            target_is_directory=directory_link)
            target = link / 'sentinel' if directory_link else link
            try:
                for operation in (
                    lambda: atomic_write_text(target, 'changed'),
                    lambda: safe_file(root, target.relative_to(root).as_posix()),
                    lambda: load_manifest(root),
                    lambda: fingerprints(root),
                    lambda: create_package_skeleton(self.config()),
                    lambda: copy_package(root, Path('copy')),
                    lambda: delete_project('safe'),
                ):
                    with self.assertRaisesRegex(ValueError, 'Symbolic link'):
                        operation()
                    self.assertEqual(self.sentinel.read_bytes(), self.original)
            finally:
                link.unlink()
        self.assertFalse(Path('copy').exists())
        self.assertFalse(Path('.packer-trash').exists())

    def test_import_rejects_source_and_destination_links_before_copy(self):
        source = Path('input/safe/package')
        self.write(source / 'a-first', b'new')
        destination = Path('output/safe')
        self.write(destination / 'a-first', b'old')
        for root in (source, destination):
            link = root / 'z-link'
            link.symlink_to(self.outside, target_is_directory=True)
            try:
                with self.assertRaisesRegex(ValueError, 'Symbolic link'):
                    _copy_tree_without_symlinks(source, destination)
                self.assertEqual((destination / 'a-first').read_bytes(), b'old')
            finally:
                link.unlink()

    def manifest(self):
        return {'type': 'solve', 'default_language': 'en', 'title': {'en': 'Synthetic'},
                'descriptions': {'en': 'en.md'}, 'editorials': {'en': 'en.md'},
                'checker': {'name': 'check.cpp', 'additional_files_names': ['./include/check.hpp']},
                'solutions': [{'name': 'model.cpp', 'type': 'model', 'additional_files_names': ['include/model.hpp']}],
                'generators': [{'name': 'gen.py', 'additional_files_names': ['lib/data.txt']}],
                'test_groups': [{'name': 'all', 'score': 100, 'tests': [{'input': '1a.in', 'output': '1a.out'}]}],
                'test_generation': [{'generator': 'gen.py', 'filename': ['1a.in', '1b.in']}]}

    def test_all_manifest_file_fields_reject_escape(self):
        root = Path('output/safe')
        fields = [('checker', 'name'), ('checker', 'additional_files_names', 0),
                  ('solutions', 0, 'name'), ('solutions', 0, 'additional_files_names', 0),
                  ('generators', 0, 'name'), ('generators', 0, 'additional_files_names', 0),
                  ('test_groups', 0, 'tests', 0, 'input'), ('test_groups', 0, 'tests', 0, 'output'),
                  ('test_generation', 0, 'generator'), ('test_generation', 0, 'filename', 0),
                  ('descriptions', 'en'), ('editorials', 'en'), ('default_language',)]
        for field in fields:
            for value in ('../outside/sentinel', str(self.sentinel), r'..\sentinel'):
                with self.subTest(field=field, value=value):
                    data = self.manifest()
                    target = data
                    for key in field[:-1]:
                        target = target[key]
                    target[field[-1]] = value
                    manifest = self.write(root / 'config.json', json.dumps(data).encode())
                    before = manifest.read_bytes()
                    with self.assertRaisesRegex(ValueError, 'config.json'):
                        load_manifest(root)
                    with self.assertRaises(ValueError):
                        archive_package(self.config())
                    self.assertEqual(manifest.read_bytes(), before)
        self.assertFalse(Path('output/safe.zip').exists())

    def test_legal_imported_names_and_nested_dependencies_survive_import_and_export(self):
        source = Path('input/safe/package')
        data = self.manifest()
        files = ['checker/check.cpp', 'checker/include/check.hpp', 'solutions/model.cpp',
                 'solutions/include/model.hpp', 'generators/gen.py', 'generators/lib/data.txt',
                 'tests/in/1a.in', 'tests/out/1a.out', 'description/en.md',
                 'description/figure-1.png', 'editorial/en.md']
        for name in files:
            self.write(source / name, name.encode())
        self.write(source / 'config.json', json.dumps(data).encode())
        image = self.write('input/safe/images/Manual figure.png', b'synthetic image')
        config = self.config(input_package=str(source), existing_tests=True,
                             image_files=[image.name])
        import_existing_materials(config)
        self.assertEqual(load_manifest(config.package_dir), data)
        self.assertEqual(config.solution_files, {'1': 'model.cpp'})
        for name in files:
            self.assertEqual((config.package_dir / name).read_bytes(), name.encode())
        self.assertEqual((config.package_dir / 'description' / image.name).read_bytes(), b'synthetic image')
        self.assertEqual(len(statement_attachments(config)), 2)
        self.assertTrue(archive_package(config).is_file())

    def test_saved_material_names_and_package_sources_cannot_escape_project(self):
        for field, value in [('image_files', ['../sentinel']), ('image_placements', {'/outside': ''}),
                             ('solution_files', {'1': str(self.sentinel)}),
                             ('sample_files', [{'input': '../sentinel', 'output': '1a.out'}]),
                             ('input_package', str(self.outside)),
                             ('input_package', 'input/other/package')]:
            with self.subTest(field=field):
                data = WorkflowState(config=self.config()).to_dict()
                data['config'][field] = value
                path = self.raw_state(data)
                before = path.read_bytes()
                with self.assertRaises(StateReadError):
                    StateStore('safe').load()
                self.assertEqual(path.read_bytes(), before)

    def test_external_explicit_document_source_remains_legal(self):
        pdf = self.write(self.outside / 'source.pdf', b'%PDF-synthetic')
        validate_pdf(pdf)
        config = self.config(statement_pdf=str(pdf))
        self.assertEqual(statement_attachments(config), [pdf])
        store = StateStore('safe')
        store.save(WorkflowState(config=config))
        self.assertEqual(store.load().config.statement_pdf, str(pdf))

    def test_cleanup_and_export_refuse_linked_zip_and_checksum(self):
        for suffix in ('.zip', '.zip.sha256'):
            state = WorkflowState(config=self.config(), setup={'pending_cleanup': 'outputs'})
            store = StateStore('safe')
            store.save(state)
            before = store.path.read_bytes()
            link = Path('output/safe' + suffix)
            link.parent.mkdir(exist_ok=True)
            link.symlink_to(self.sentinel)
            try:
                with self.assertRaises(ValueError):
                    cleanup(state, store)
                with self.assertRaises(ValueError):
                    archive_package(state.config)
                self.assertEqual(store.path.read_bytes(), before)
            finally:
                link.unlink()

    def test_recovery_journal_cannot_delete_outside_project(self):
        store = StateStore('safe')
        store.save(WorkflowState(config=self.config()))
        journal = store.path.parent / '.restore-transaction.json'
        for data in ({'transaction': str(self.outside), 'assets': [], 'expected_state_sha256': ''},
                     {'transaction': str((store.path.parent / '.restore-work').absolute()),
                      'assets': [{'destination': str(self.sentinel), 'original': str(self.sentinel),
                                  'staged': str(self.outside / 'missing')}], 'expected_state_sha256': ''}):
            journal.write_text(json.dumps(data))
            before = journal.read_bytes()
            with self.assertRaisesRegex(ValueError, 'recovery'):
                store.load()
            self.assertEqual(journal.read_bytes(), before)

    def test_manual_import_accepts_explicit_external_file(self):
        source = self.write(self.outside / 'manual.cpp', b'int main() {}\n')
        state, store = WorkflowState(config=self.config()), StateStore('safe')
        workflow = Workflow(state, store, object())
        with patch('task_packer.console.ask', return_value=str(source)):
            workflow._manual_draft('solution:1', 'f', None, {'code': 'model.cpp'})
        self.assertEqual(json.loads(state.drafts['solution:1'])['code'], 'int main() {}\n')
        self.assertEqual(source.read_bytes(), b'int main() {}\n')
        self.assertEqual(store.load().drafts, state.drafts)

    def test_state_and_input_intermediate_links_are_rejected(self):
        for target in ('.packer-projects/safe', 'input/safe', 'output/safe'):
            link = Path(target)
            link.parent.mkdir(exist_ok=True)
            link.symlink_to(self.outside, target_is_directory=True)
            try:
                with self.assertRaisesRegex(ValueError, 'Symbolic link'):
                    acquire_project_lock('safe')
                self.assertEqual(list(self.outside.iterdir()), [self.sentinel])
            finally:
                link.unlink()
        path = Path('.packer-projects/safe/state.json')
        path.parent.mkdir(exist_ok=True)
        path.symlink_to(self.sentinel)
        with self.assertRaises(ValueError):
            StateStore('safe').load()
        project = StateStore.available()[0]
        self.assertEqual(project[0], 'safe')
        self.assertIsInstance(project[1], StateReadError)

    def test_saved_setup_without_optional_code_still_resumes_same_project(self):
        state = WorkflowState(setup={'has_package': False})
        store = StateStore('safe')
        store.save(state)
        with patch('task_packer.onboarding.ask', side_effect=SavedExit):
            with self.assertRaises(SavedExit):
                gather_config(store.load(), store)
        self.assertFalse(Path('.packer-projects/task').exists())
        self.assertFalse(Path('input/task').exists())

    def test_invalid_setup_language_fails_before_form_writes(self):
        state = WorkflowState(setup={'codename': 'safe', 'language_code': '../bad'})
        store = StateStore('safe')
        path = self.raw_state(state.to_dict())
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            gather_config(state, store)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path('input').exists())

    def test_valid_history_still_restores(self):
        state, store = WorkflowState(config=self.config()), StateStore('safe')
        store.save(state)
        document = self.write('output/safe/description/en.md', b'old')
        version = checkpoint(state, store, 'synthetic')
        document.write_bytes(b'new')
        restore(state, store, version.name)
        self.assertEqual(document.read_bytes(), b'old')
        self.assertFalse((store.path.parent / '.restore-transaction.json').exists())


if __name__ == '__main__':
    unittest.main()
