"""Regression tests use synthetic states and a temporary working directory."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from task_packer import cli
from task_packer.console import SavedExit
from task_packer.models import ProjectConfig, Subtask, WorkflowState
from task_packer.storage import StateReadError, StateStore


class StateAdministrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, self.previous)
        self.gather = self.enterContext(patch('task_packer.cli.gather_config'))
        self.client = self.enterContext(patch('task_packer.cli.OpenAIClient'))
        self.workflow = self.enterContext(patch('task_packer.cli.Workflow'))
        acquire = StateStore.acquire_lock

        def acquire_for_test(store):
            lock = acquire(store)
            # Mocks retain store arguments beyond main(), so close real handles
            # deterministically before deleting the temporary directory.
            self.addCleanup(lock.close)
            return lock

        self.enterContext(patch.object(StateStore, 'acquire_lock', acquire_for_test))

    def write_state(self, code, data):
        path = StateStore(code).path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def configured(self):
        return WorkflowState(config=ProjectConfig(
            codename='good', title='Test', origin='test', language_code='en',
            original_statement='Synthetic statement',
            subtasks=[Subtask(1, 'All', 100, 'n <= 10')])).to_dict()

    def run_cli(self, *args, exit_code=None):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['packer.py', *args]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if exit_code is None:
                cli.main()
            else:
                with self.assertRaises(SystemExit) as caught:
                    cli.main()
                self.assertEqual(caught.exception.code, exit_code)
        return out.getvalue(), err.getvalue()

    def assert_no_generation(self):
        self.gather.assert_not_called()
        self.client.assert_not_called()
        self.workflow.assert_not_called()
        self.assertFalse(Path('input').exists())
        self.assertFalse(Path('output').exists())

    def test_incomplete_config_reports_field_and_preserves_bytes(self):
        path = self.write_state('bad', {'config': {'codename': 'bad'}})
        before = path.read_bytes()
        with self.assertRaisesRegex(StateReadError, r'bad/state.json:.*missing required field'):
            StateStore('bad').load()
        self.assertEqual(path.read_bytes(), before)

    def test_missing_subtasks_is_specific(self):
        data = self.configured()
        del data['config']['subtasks']
        path = self.write_state('bad', data)
        with self.assertRaisesRegex(StateReadError, 'state.config.subtasks: missing required field'):
            StateStore('bad').load()
        self.assertEqual(json.loads(path.read_text()), data)

    def test_malformed_json(self):
        path = self.write_state('bad', {})
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaisesRegex(StateReadError, 'bad/state.json:.*line 1'):
            StateStore('bad').load()
        self.assertEqual(path.read_text(), '{broken')

    def test_invalid_utf8_is_visible_without_traceback(self):
        path = self.write_state('bad', {})
        path.write_bytes(b'\xff')
        out, err = self.run_cli('--status')
        self.assertIn('requires repair', out)
        self.assertIn(str(path), out)
        self.assertEqual(err, '')
        _, err = self.run_cli('--project', 'bad', exit_code=1)
        self.assertIn(str(path), err)
        self.assertNotIn('Traceback', err)
        self.assertEqual(path.read_bytes(), b'\xff')
        self.assert_no_generation()

    def test_invalid_types(self):
        cases = [[], None, 0, {'config': {}}, {'config': False}, {'config': []},
                 {'setup': []}, {'completed': 'done'}, {'completed': [1]},
                 {'drafts': {'x': 1}}, {'usage': {'input_tokens': '1'}},
                 {'finished': 1}, {'schema_version': '4'}, {'schema_version': True},
                 {'schema_version': 0}, {'unexpected': 1}]
        for data in cases:
            with self.subTest(data=data):
                path = self.write_state('bad', data)
                before = path.read_bytes()
                with self.assertRaises(StateReadError):
                    StateStore('bad').load()
                self.assertEqual(path.read_bytes(), before)

    def test_invalid_config_and_subtask_types(self):
        for field, value in [('title', 3), ('subtasks', {}),
                             ('subtasks', [{}]), ('subtasks', [None]),
                             ('subtasks', [{'index': True, 'name': 'All', 'points': 100, 'constraints': ''}]),
                             ('image_files', [1]), ('image_placements', {'x': 3}),
                             ('sample_files', ['bad'])]:
            with self.subTest(field=field, value=value):
                data = self.configured()
                data['config'][field] = value
                self.write_state('bad', data)
                with self.assertRaises(StateReadError):
                    StateStore('bad').load()

    def test_newer_schema_refused_before_config(self):
        self.write_state('bad', {'schema_version': 5, 'config': {'codename': 'bad'}})
        with self.assertRaisesRegex(StateReadError, 'newer version'):
            StateStore('bad').load()

    def test_legacy_config_defaults_and_no_write(self):
        config = self.configured()['config']
        required = ('codename', 'title', 'origin', 'language_code', 'original_statement', 'subtasks')
        config = {key: config[key] for key in required}
        del config['subtasks'][0]['group_name']
        for version in (None, 1, 2, 3, 4):
            with self.subTest(version=version):
                data = {'config': config}
                if version is not None:
                    data['schema_version'] = version
                path = self.write_state('good', data)
                before = path.read_bytes()
                state = StateStore('good').load()
                self.assertEqual(state.schema_version, 4)
                self.assertEqual(state.setup, {})
                self.assertEqual(state.config.task_type, 'standard')
                self.assertEqual(state.config.subtasks[0].group_name, '')
                self.assertEqual(path.read_bytes(), before)

    def test_available_keeps_corrupt_project_visible(self):
        bad = self.write_state('bad', {'config': {'codename': 'bad'}})
        before = bad.read_bytes()
        self.write_state('good', self.configured())
        projects = StateStore.available()
        self.assertEqual([code for code, _ in projects], ['bad', 'good'])
        self.assertIsInstance(projects[0][1], StateReadError)
        self.assertIsInstance(projects[1][1], WorkflowState)
        self.assertEqual(bad.read_bytes(), before)

    def test_status_lists_all_error_kinds_and_healthy_project(self):
        paths = [self.write_state('partial', {'config': {'codename': 'partial'}}),
                 self.write_state('future', {'schema_version': 5}),
                 self.write_state('types', {'finished': 'yes'}),
                 self.write_state('json', {})]
        paths[-1].write_text('{bad', encoding='utf-8')
        self.write_state('good', self.configured())
        before = [p.read_bytes() for p in paths]
        out, err = self.run_cli('--status')
        self.assertEqual(err, '')
        self.assertEqual(out.count('requires repair'), 4)
        self.assertIn('good — in progress', out)
        for path, content in zip(paths, before):
            self.assertIn(str(path), out)
            self.assertEqual(path.read_bytes(), content)
        self.assert_no_generation()

    def test_direct_project_bypasses_discovery(self):
        self.write_state('bad', {'config': {'codename': 'bad'}})
        self.write_state('good', self.configured())
        args = cli._parser().parse_args(['--project', 'good'])
        with patch.object(StateStore, 'available', side_effect=AssertionError('must not scan')):
            state, store = cli._select_state(args)
        self.assertEqual(state.config.codename, 'good')
        store._lock.close()
        self.assert_no_generation()

    def test_direct_project_runs_healthy_workflow_beside_corruption(self):
        bad = self.write_state('bad', {'config': {'codename': 'bad'}})
        before = bad.read_bytes()
        self.write_state('good', self.configured())
        with patch('sys.stdin.isatty', return_value=False):
            _, err = self.run_cli('--project', 'good')
        self.assertEqual(err, '')
        self.gather.assert_not_called()
        self.client.assert_called_once()
        self.workflow.return_value.run.assert_called_once()
        self.assertEqual(bad.read_bytes(), before)

    def test_interactive_selection_of_healthy_neighbor(self):
        bad = self.write_state('bad', {'config': {'codename': 'bad'}})
        before = bad.read_bytes()
        self.write_state('good', self.configured())
        with patch('task_packer.cli.ask_int', return_value=2), contextlib.redirect_stdout(io.StringIO()):
            state, store = cli._select_state(cli._parser().parse_args([]))
        self.assertEqual(state.config.codename, 'good')
        store._lock.close()
        self.assertEqual(bad.read_bytes(), before)
        self.assert_no_generation()

    def test_selecting_corrupt_project_has_no_traceback(self):
        path = self.write_state('bad', {'config': {'codename': 'bad'}})
        before = path.read_bytes()
        for args in [('--project', 'bad'), ()]:
            with self.subTest(args=args), patch('task_packer.cli.ask_int', return_value=1):
                _, err = self.run_cli(*args, exit_code=1)
                self.assertIn(str(path), err)
                self.assertIn('missing required field', err)
                self.assertNotIn('Traceback', err)
        self.assertEqual(path.read_bytes(), before)
        self.assert_no_generation()

    def test_history_unconfigured_does_not_cleanup_or_configure(self):
        # A pending marker verifies that read-only history skips cleanup too.
        path = self.write_state('started', {'config': None, 'setup': {'pending_cleanup': 'rules'}})
        before = path.read_bytes()
        checkpoint = path.parent / 'history/checkpoints/001/checkpoint.json'
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text(json.dumps({'reason': 'Synthetic checkpoint'}))
        out, err = self.run_cli('--project', 'started', '--history')
        self.assertIn('001: Synthetic checkpoint', out)
        self.assertEqual(err, '')
        self.assertEqual(path.read_bytes(), before)
        self.assert_no_generation()

    def test_history_selection_unconfigured(self):
        path = self.write_state('started', {'config': None})
        before = path.read_bytes()
        with patch('task_packer.cli.ask_int', return_value=1):
            _, err = self.run_cli('--history')
        self.assertEqual(err, '')
        self.assertEqual(path.read_bytes(), before)
        self.assert_no_generation()

    def test_delete_cancelled_for_unconfigured(self):
        path = self.write_state('started', {'config': None, 'setup': {'title': 'Draft'}})
        before = path.read_bytes()
        with patch('task_packer.console.ask', return_value='wrong'):
            out, err = self.run_cli('--project', 'started', '--delete')
        self.assertIn('code does not match', out)
        self.assertEqual(err, '')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path('.packer-trash').exists())
        self.assert_no_generation()

    def test_delete_confirmed_for_unconfigured(self):
        path = self.write_state('started', {'config': None, 'setup': {'title': 'Draft'}})
        before = path.read_bytes()
        with patch('task_packer.console.ask', return_value='started'):
            out, err = self.run_cli('--project', 'started', '--delete')
        self.assertIn('Recoverable copy:', out)
        self.assertEqual(err, '')
        self.assertFalse(path.exists())
        archived = list(Path('.packer-trash').glob('*/state/state.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), before)
        self.assert_no_generation()

    def test_normal_resume_still_gathers_config(self):
        self.write_state('started', {'config': None})
        self.gather.side_effect = SavedExit()
        self.run_cli('--project', 'started')
        self.gather.assert_called_once()
        self.client.assert_not_called()

    def test_history_does_not_migrate_finished_legacy_state(self):
        data = self.configured()
        data['finished'] = True
        path = self.write_state('good', data)
        before = path.read_bytes()
        self.run_cli('--project', 'good', '--history')
        self.assertEqual(path.read_bytes(), before)
        self.assert_no_generation()


if __name__ == '__main__':
    unittest.main()
