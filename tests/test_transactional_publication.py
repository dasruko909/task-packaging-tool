"""Synthetic regression cases for test replacement, export and certificates."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from task_packer.execution import ExecutionError
from task_packer.freshness import certify, changed_files, certified_native_result, fingerprints
from task_packer.models import ProjectConfig, Subtask, WorkflowState
from task_packer.solve_native import archive_package, copy_package, native_action, recover_export
from task_packer.storage import StateStore, write_json
from task_packer.workflow import Workflow
from task_packer.verification import Verification, recover_tests


class TransactionalPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        previous = Path.cwd()
        os.chdir(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(os.chdir, previous)
        self.config = ProjectConfig(codename='safe', title='Synthetic', origin='test',
                                    language_code='en', original_statement='Synthetic',
                                    subtasks=[Subtask(1, 'Full', 100, '')])
        self.root = self.config.package_dir
        self.manifest = {
            'type': 'solve-standard', 'limits': {'time': 1000}, 'checker': {'name': 'check.cpp'},
            'solutions': [{'name': 'solution_01.cpp'}],
            'generators': [{'name': 'gen.cpp'}],
            'test_generation': [
                {'generator': 'gen.cpp', 'parameters': name, 'filename': name + '.in'}
                for name in ('a', 'b')],
            'test_groups': [{'name': '01', 'tests': [
                {'input': 'a.in'}, {'input': 'b.in'},
                {'input': 'fixed.in', 'output': 'fixed.out'}]}],
        }
        for name in ('checker/check.cpp', 'solutions/solution_01.cpp', 'generators/gen.cpp',
                     'validators/input_validator.cpp', 'verification/small_generator.cpp',
                     'verification/brute.cpp', 'verification/mutants/one.cpp',
                     'verification/mutants/two.cpp'):
            self.write(name, b'// synthetic')
        self.write('config.json', json.dumps(self.manifest).encode())
        self.write('verification/mutants.json', json.dumps([
            {'name': 'one.cpp', 'description': 'one'},
            {'name': 'two.cpp', 'description': 'two'}]).encode())
        for name in ('a', 'b', 'fixed'):
            self.write('tests/in/' + name + '.in', ('old ' + name + '\r\n\r\n').encode())
            self.write('tests/out/' + name + '.out', ('old answer ' + name + '\r\n').encode())
        self.write('tests/in/user-notes.txt', b'\xff\x00unrelated')
        self.write('tests/out/prepared_user-file', b'user file')
        self.write('readme.md', b'Synthetic README')
        self.write('description/en.md', b'Synthetic statement')
        self.failure = None
        self.generator_calls = 0
        self.reference_calls = 0
        owner = self

        class FakeRunner:
            def __init__(self, directory, includes):
                self.directory = directory

            def compile(self, source, **kwargs):
                return [str(source)]

            def program(self, source, data='', **kwargs):
                if source.name == 'gen.cpp':
                    owner.generator_calls += 1
                    if owner.failure == 'generator' and owner.generator_calls == 4:
                        return 'different'
                    return 'new ' + kwargs['args'][0] + '\r\n\r\n'
                if source.name == 'input_validator.cpp':
                    if owner.failure == 'validator' and data.startswith('new b'):
                        raise ExecutionError('late invalid input', source)
                    return ''
                if source.name == 'solution_01.cpp':
                    owner.reference_calls += 1
                    if owner.failure == 'output' and owner.reference_calls == 2:
                        raise ExecutionError('late output failure', source)
                    if owner.failure == 'source_change':
                        owner.write('readme.md', b'changed while checking')
                    return 'new answer \t\r\n'
                if source.name == 'small_generator.cpp':
                    if owner.failure == 'brute':
                        raise ExecutionError('late brute failure', source)
                    return 'small\r\n'
                if source.name == 'brute.cpp':
                    return 'new answer \t\r\n'
                if source.parent.name == 'mutants':
                    if owner.failure == 'mutant':
                        return 'survivor'
                    raise ExecutionError('synthetic mutant runtime failure', source)
                raise AssertionError(source)

        self.runner = FakeRunner

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def tests_bytes(self):
        return {str(p.relative_to(self.root / 'tests')): p.read_bytes()
                for p in (self.root / 'tests').rglob('*') if p.is_file()}

    def verification(self):
        report = Verification(self.config, regenerate_outputs=True)
        report.check_answer = lambda *args: 100
        return report

    def test_late_checks_preserve_complete_previous_test_set(self):
        before = self.tests_bytes()
        for failure in ('generator', 'validator', 'output', 'brute', 'mutant', 'source_change'):
            with self.subTest(failure=failure):
                self.failure = failure
                self.generator_calls = self.reference_calls = 0
                report = self.verification()
                with patch('task_packer.verification.Runner', self.runner), self.assertRaises(ExecutionError):
                    report.run()
                self.assertEqual(self.tests_bytes(), before)
                self.assertIsNone(report.verified_fingerprints)

    def test_success_replaces_generated_files_and_preserves_fixed_and_unrelated_bytes(self):
        before = self.tests_bytes()
        report = self.verification()
        with patch('task_packer.verification.Runner', self.runner):
            report.run()
        after = self.tests_bytes()
        for name in ('a', 'b'):
            self.assertEqual(after['in/' + name + '.in'], ('new ' + name + '\r\n\r\n').encode())
            self.assertEqual(after['out/' + name + '.out'], b'new answer \t\r\n')
        for name in ('in/fixed.in', 'out/fixed.out', 'in/user-notes.txt', 'out/prepared_user-file'):
            self.assertEqual(after[name], before[name])
        self.assertEqual(report.verified_fingerprints, fingerprints(self.root))

    def test_late_output_staging_write_failure_preserves_live_files(self):
        before = self.tests_bytes()
        from task_packer.storage import atomic_write_text

        def fail(path, content):
            if path.name == 'b.out' and path.parent.name == 'out':
                raise OSError('late staged output write failure')
            atomic_write_text(path, content)

        with patch('task_packer.verification.Runner', self.runner), \
                patch('task_packer.verification.atomic_write_text', side_effect=fail), \
                self.assertRaises(OSError):
            self.verification().run()
        self.assertEqual(self.tests_bytes(), before)

    def test_test_publication_failure_rolls_back(self):
        before = self.tests_bytes()
        replace = Path.replace

        def fail(path, target):
            if path.name == 'tests' and path.parent.name.startswith('.safe-tests-'):
                raise OSError('candidate replacement failed')
            return replace(path, target)

        with patch('task_packer.verification.Runner', self.runner), \
                patch.object(Path, 'replace', fail), self.assertRaises(OSError):
            self.verification().run()
        self.assertEqual(self.tests_bytes(), before)

    def test_pending_repaired_sample_preserves_approved_files_on_failure(self):
        before = self.tests_bytes()
        report = self.verification()
        report.input_overrides['fixed.in'] = 'repaired \r\n'
        self.failure = 'brute'
        with patch('task_packer.verification.Runner', self.runner), self.assertRaises(ExecutionError):
            report.run()
        self.assertEqual(self.tests_bytes(), before)
        self.failure = None
        with patch('task_packer.verification.Runner', self.runner):
            report.run()
        self.assertEqual((self.root / 'tests/in/fixed.in').read_bytes(), b'repaired \r\n')
        self.assertEqual((self.root / 'tests/out/fixed.out').read_bytes(), b'new answer \t\r\n')

    def test_test_recovery_restores_original_and_rejects_unsafe_work(self):
        before = self.tests_bytes()
        work = self.root.parent / '.safe-test-replacement'
        work.mkdir()
        write_json(work / 'journal.json', {'phase': 'ready', 'original': True})
        (self.root / 'tests').replace(work / 'original')
        self.write('tests/in/a.in', b'partly published')
        recover_tests(self.root)
        self.assertEqual(self.tests_bytes(), before)
        work.mkdir()
        write_json(work / 'journal.json', {'phase': 'ready', 'original': True, 'path': '/tmp'})
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            recover_tests(self.root)
        shutil.rmtree(work)
        work.symlink_to(self.root / 'tests', target_is_directory=True)
        with self.assertRaises(ValueError):
            recover_tests(self.root)
        work.unlink()
        self.assertEqual(self.tests_bytes(), before)

    def old_export(self):
        zip_path = self.root.parent / 'safe.zip'
        checksum = zip_path.with_suffix('.zip.sha256')
        zip_path.write_bytes(b'previous complete ZIP')
        checksum.write_bytes(b'previous checksum')
        return zip_path, checksum

    def test_export_publication_failure_preserves_both_artifacts(self):
        for failing in ('package.zip', 'package.sha256'):
            with self.subTest(failing=failing):
                zip_path, checksum = self.old_export()
                replace = Path.replace

                def fail(path, target):
                    if path.name == failing:
                        raise OSError('publication failure')
                    return replace(path, target)

                with patch.object(Path, 'replace', fail), self.assertRaises(OSError):
                    archive_package(self.config)
                self.assertEqual(zip_path.read_bytes(), b'previous complete ZIP')
                self.assertEqual(checksum.read_bytes(), b'previous checksum')

    def test_failed_checksum_preparation_preserves_both_artifacts(self):
        zip_path, checksum = self.old_export()
        from task_packer.storage import atomic_write_text

        def fail(path, content):
            if path.name == 'package.sha256':
                raise OSError('checksum preparation failed')
            atomic_write_text(path, content)

        with patch('task_packer.solve_native.atomic_write_text', side_effect=fail), self.assertRaises(OSError):
            archive_package(self.config)
        self.assertEqual(zip_path.read_bytes(), b'previous complete ZIP')
        self.assertEqual(checksum.read_bytes(), b'previous checksum')

    def test_successful_export_publishes_matching_zip_and_checksum(self):
        zip_path, checksum = self.old_export()
        archive_package(self.config)
        self.assertEqual(checksum.read_text(), hashlib.sha256(zip_path.read_bytes()).hexdigest() + '  safe.zip\n')
        import zipfile
        with zipfile.ZipFile(zip_path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read('tests/in/fixed.in'), b'old fixed\r\n\r\n')
            self.assertEqual(archive.read('tests/in/user-notes.txt'), b'\xff\x00unrelated')

    def test_export_recovery_restores_pair_and_rejects_symlinks_and_malformed_journal(self):
        zip_path, checksum = self.old_export()
        work = zip_path.parent / '.safe.zip-publication'
        work.mkdir()
        shutil.copy2(zip_path, work / 'original-0')
        shutil.copy2(checksum, work / 'original-1')
        write_json(work / 'journal.json', {'phase': 'ready', 'originals': [True, True]})
        zip_path.write_bytes(b'interrupted new ZIP')
        recover_export(zip_path)
        self.assertEqual(zip_path.read_bytes(), b'previous complete ZIP')
        self.assertEqual(checksum.read_bytes(), b'previous checksum')
        work.mkdir()
        write_json(work / 'journal.json', {'phase': [], 'originals': [True, True]})
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            recover_export(zip_path)
        shutil.rmtree(work)
        work.mkdir()
        write_json(work / 'journal.json', {'phase': 'ready', 'originals': [True, True]})
        (work / 'original-0').symlink_to(zip_path.absolute())
        with self.assertRaises(ValueError):
            recover_export(zip_path)
        shutil.rmtree(work)
        self.assertEqual(zip_path.read_bytes(), b'previous complete ZIP')

    def test_export_change_during_copy_or_replacement_preserves_pair(self):
        for point in ('copy', 'replace'):
            with self.subTest(point=point):
                zip_path, checksum = self.old_export()
                if point == 'copy':
                    def mutate(source, destination):
                        copy_package(source, destination)
                        self.write('readme.md', b'edited during copy')
                    context = patch('task_packer.solve_native.copy_package', side_effect=mutate)
                else:
                    replace = Path.replace
                    def mutate(path, target):
                        result = replace(path, target)
                        if path.name == 'package.zip':
                            self.write('readme.md', b'edited during publication')
                        return result
                    context = patch.object(Path, 'replace', mutate)
                with context, self.assertRaisesRegex(RuntimeError, 'changed'):
                    archive_package(self.config)
                self.assertEqual(zip_path.read_bytes(), b'previous complete ZIP')
                self.assertEqual(checksum.read_bytes(), b'previous checksum')

    def test_publication_commit_write_failures_roll_back_complete_artifacts(self):
        def fail_commit(path, state):
            if path.name == 'journal.json' and state.get('phase') == 'committed':
                raise OSError('commit journal write failed')
            write_json(path, state)

        before = self.tests_bytes()
        with patch('task_packer.verification.Runner', self.runner), \
                patch('task_packer.verification.write_json', side_effect=fail_commit), \
                self.assertRaises(OSError):
            self.verification().run()
        self.assertEqual(self.tests_bytes(), before)
        zip_path, checksum = self.old_export()
        with patch('task_packer.solve_native.write_json', side_effect=fail_commit), self.assertRaises(OSError):
            archive_package(self.config)
        self.assertEqual(zip_path.read_bytes(), b'previous complete ZIP')
        self.assertEqual(checksum.read_bytes(), b'previous checksum')

    def test_first_export_failure_leaves_no_partial_artifacts(self):
        zip_path = self.root.parent / 'safe.zip'
        replace = Path.replace

        def fail(path, target):
            if path.name == 'package.sha256':
                raise KeyboardInterrupt('interrupted replacement')
            return replace(path, target)

        with patch.object(Path, 'replace', fail), self.assertRaises(KeyboardInterrupt):
            archive_package(self.config)
        self.assertFalse(zip_path.exists())
        self.assertFalse(zip_path.with_suffix('.zip.sha256').exists())

    def test_project_load_recovers_interrupted_test_and_export_replacements(self):
        before = self.tests_bytes()
        tests_work = self.root.parent / '.safe-test-replacement'
        tests_work.mkdir()
        write_json(tests_work / 'journal.json', {'phase': 'ready', 'original': True})
        (self.root / 'tests').replace(tests_work / 'original')
        self.write('tests/in/a.in', b'partial tests')
        zip_path, checksum = self.old_export()
        export_work = zip_path.parent / '.safe.zip-publication'
        export_work.mkdir()
        shutil.copy2(zip_path, export_work / 'original-0')
        shutil.copy2(checksum, export_work / 'original-1')
        write_json(export_work / 'journal.json', {'phase': 'ready', 'originals': [True, True]})
        zip_path.write_bytes(b'partial export')
        store = StateStore('safe')
        store.save(WorkflowState(config=self.config))
        store.load()
        self.assertEqual(self.tests_bytes(), before)
        self.assertEqual(zip_path.read_bytes(), b'previous complete ZIP')
        self.assertEqual(checksum.read_bytes(), b'previous checksum')

    def test_copy_snapshot_mismatch_cannot_publish_export(self):
        zip_path, checksum = self.old_export()

        def mutate_copy(source, destination):
            copy_package(source, destination)
            (destination / 'readme.md').write_bytes(b'different copied bytes')

        with patch('task_packer.solve_native.copy_package', side_effect=mutate_copy), \
                self.assertRaisesRegex(RuntimeError, 'changed while copying'):
            archive_package(self.config)
        self.assertEqual(zip_path.read_bytes(), b'previous complete ZIP')
        self.assertEqual(checksum.read_bytes(), b'previous checksum')

    def test_native_check_binds_success_to_checked_sources(self):
        owner = self
        changed = False

        class Process:
            def __init__(self, command, **kwargs):
                self.command = command

            def wait(self, **kwargs):
                write_json(Path(self.command[6]), {'ok': True, 'built': True})
                if changed:
                    owner.write('readme.md', b'changed during native run')

        health = {'ready': True, 'sandbox_ready': True, 'versions': {'synthetic': '1'}}
        with patch('task_packer.solve_native.doctor', return_value=health), \
                patch('task_packer.solve_native.isolated', side_effect=lambda command, **kwargs: command), \
                patch('task_packer.solve_native.subprocess.Popen', Process), \
                patch('task_packer.solve_native.kill'):
            result = native_action(self.config)
            self.assertTrue(result['ok'])
            self.assertEqual(result['source_sha256'], fingerprints(self.root))
            changed = True
            result = native_action(self.config)
            self.assertFalse(result['ok'])
            self.assertIn('changed', result['error'])
            self.assertNotIn('source_sha256', result)

    def test_workflow_refreshes_editorial_and_metadata_before_each_verification_attempt(self):
        state = WorkflowState(config=self.config, completed=['editorial'])
        store = Mock(codename='safe')
        store.path = Path('.packer-projects/safe/state.json')
        workflow = Workflow(state, store, Mock())
        workflow.verification_materials = Mock()
        events = []
        sample = self.root / 'tests/in/fixed.in'
        report = self.verification()
        calls = 0

        def run():
            nonlocal calls
            events.append('run')
            calls += 1
            if calls == 1:
                raise ExecutionError('invalid input', sample)

        def repair(*args):
            state.completed.remove('editorial')
            return 'repaired'

        def editorial():
            events.append('editorial')
            state.completed.append('editorial')

        report.run = run
        report.record = Mock()
        workflow._repair_invalid_test = repair
        workflow.editorial = editorial
        with patch('task_packer.workflow.write_package_metadata',
                   side_effect=lambda *args, **kwargs: events.append('metadata')):
            workflow.verify_code(report)
        self.assertEqual(events, ['metadata', 'run', 'editorial', 'metadata', 'run'])
        self.assertEqual(report.input_overrides, {'fixed.in': 'repaired'})

    def test_workflow_sample_repair_returns_candidate_without_deleting_approved_answer(self):
        self.config.test_plan = {'tests': [{'input': 'old', 'output': 'approved',
                                            'description': 'sample'}]}
        self.manifest['test_groups'][0]['tests'].append({'input': '0a.in', 'output': '0a.out'})
        self.write('config.json', json.dumps(self.manifest).encode())
        source = self.write('tests/in/0a.in', b'approved input')
        output = self.write('tests/out/0a.out', b'approved answer')
        store = Mock(codename='safe')
        store.path = Path('.packer-projects/safe/state.json')
        workflow = Workflow(WorkflowState(config=self.config), store, Mock())

        def review(**kwargs):
            data = {'input': 'repaired \r\n', 'description': 'repaired sample'}
            kwargs['validator'](data)
            kwargs['accept'](data)

        workflow._review_json = review
        with patch('task_packer.revisions.snapshot'):
            repaired = workflow._repair_invalid_test(source, ExecutionError('invalid', source), 0)
        self.assertEqual(repaired, 'repaired \r\n')
        self.assertEqual(source.read_bytes(), b'approved input')
        self.assertEqual(output.read_bytes(), b'approved answer')

    def test_certificate_covers_exported_material_edits_additions_and_deletions(self):
        for name in ('readme.md', 'description/en.pdf', 'description/en.html',
                     'extra/data.bin', 'solutions/include/helper.hpp',
                     'packer_tools/task_packer/portable.py'):
            with self.subTest(name=name):
                path = self.write(name, b'original')
                certify(self.config, native=False)
                self.assertEqual(changed_files(self.config), [])
                path.write_bytes(b'changed')
                self.assertIn(name, changed_files(self.config))
                path.unlink()
                self.assertIn(name, changed_files(self.config))
                certify(self.config, native=False)
                path.write_bytes(b'added')
                self.assertIn(name, changed_files(self.config))

    def test_certificate_selection_matches_export_and_ignores_only_transient_reports(self):
        self.write('verification/assumptions.md', b'relevant local dependency')
        before = fingerprints(self.root)
        for name in ('verification/report.json', 'verification/report.md',
                     'verification/coverage.json', 'verification/counterexample.json',
                     'verification/output-history/old.out', 'verification/solve-check.log',
                     'solutions/prepared_solution', 'solutions/__pycache__/cache',
                     'solutions/a.cpp.before-repair-1'):
            self.write(name, b'transient')
        self.assertEqual(fingerprints(self.root), before)
        copy_package(self.root, Path('export-copy'))
        exported = {str(p.relative_to('export-copy')) for p in Path('export-copy').rglob('*') if p.is_file()}
        self.assertTrue(exported <= before.keys())
        self.write('verification/assumptions.md', b'changed dependency')
        self.assertNotEqual(fingerprints(self.root), before)

    def test_invalid_or_old_certificates_fail_closed(self):
        certificate = self.root / 'verification/certificate.json'
        for data in ([], None, 7, 'bad', {}, {'sha256': []},
                     {'local': True, 'native': False, 'task_type': 'standard', 'sha256': fingerprints(self.root)},
                     {'version': 2, 'local': True, 'native': False, 'sha256': {'file': 1}}):
            with self.subTest(data=data):
                write_json(certificate, data)
                self.assertTrue(changed_files(self.config))
                self.assertIsNone(certified_native_result(self.config))
        certificate.write_text('{')
        self.assertEqual(changed_files(self.config), ['unreadable verification certificate'])

    def test_native_digest_remains_separate_and_certification_rejects_changed_sources(self):
        before = fingerprints(self.root)
        result_path = self.root / 'verification/solve-check.json'
        write_json(result_path, {'ok': True, 'source_sha256': before})
        certify(self.config, native=True, expected=before)
        self.assertIsNotNone(certified_native_result(self.config))
        write_json(result_path, {'ok': True, 'source_sha256': before, 'changed': True})
        self.assertEqual(changed_files(self.config), [])
        self.assertIsNone(certified_native_result(self.config))
        self.write('readme.md', b'late material change')
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            certify(self.config, native=True)
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            certify(self.config, native=False, expected=before)
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            archive_package(self.config, expected=before)


if __name__ == '__main__':
    unittest.main()
