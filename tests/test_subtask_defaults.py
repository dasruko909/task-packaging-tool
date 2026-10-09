"""Subtask defaults and preservation without model calls or user data."""
import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from task_packer.models import ProjectConfig, Subtask, WorkflowState
from task_packer.onboarding import _detected_subtasks, gather_config
from task_packer.parsing import ModelFormatError


class SubtaskDefaultsTests(unittest.TestCase):
    def detect(self, explicit, subtasks):
        return _detected_subtasks({'has_explicit_subtasks': explicit,
                                  'subtasks': subtasks, 'reason': 'Synthetic test'})

    def test_no_explicit_subtasks_empty_list_defaults_to_full(self):
        self.assertEqual(self.detect(False, []),
                         [Subtask(1, 'Full', 100, 'No additional constraints')])

    def test_no_explicit_subtasks_does_not_add_group_to_model_list(self):
        self.assertEqual(self.detect(False, [
            {'index': 1, 'name': 'Invented', 'points': 30, 'constraints': 'n <= 5'}]),
            [Subtask(1, 'Full', 100, 'No additional constraints')])

    def test_explicit_empty_subtasks_rejected(self):
        with self.assertRaisesRegex(ModelFormatError, 'at least one subtask'):
            self.detect(True, [])

    def test_explicit_groups_preserve_points_and_constraints(self):
        groups = [{'index': 1, 'name': 'Small', 'points': 30, 'constraints': 'n <= 5'},
                  {'index': 2, 'name': 'Large', 'points': 70, 'constraints': 'n <= 100'}]
        self.assertEqual(self.detect(True, groups),
                         [Subtask(1, 'Small', 30, 'n <= 5'),
                          Subtask(2, 'Large', 70, 'n <= 100')])

    def test_explicit_single_subtask_preserves_constraints(self):
        self.assertEqual(self.detect(True, [
            {'index': 1, 'name': 'Full', 'points': 100, 'constraints': 'n <= 100'}]),
            [Subtask(1, 'Full', 100, 'n <= 100')])

    def test_explicit_groups_cannot_total_more_than_100(self):
        with self.assertRaisesRegex(ModelFormatError, 'do not total 100'):
            self.detect(True, [
                {'index': 1, 'name': 'Small', 'points': 100, 'constraints': 'n <= 5'},
                {'index': 2, 'name': 'Large', 'points': 100, 'constraints': 'n <= 100'}])

    def manual_config(self, **updates):
        # Skip unrelated intake questions, but use the real subtask intake path.
        setup = dict(codename='synthetic', has_package=False, language_code='en',
                     statement_source='paste', original_statement='Main constraints: n <= 100.',
                     statement_idea='', has_images=False, title='Synthetic', origin='Test',
                     time_limit_ms=2000, memory_limit_mb=256, subtask_mode='manual')
        setup.update(updates)
        state = WorkflowState(setup=setup)
        store = Mock(codename='synthetic')
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch('task_packer.onboarding.prepare_drop_zones'))
            stack.enter_context(patch('task_packer.openai_client.OpenAIClient',
                                      side_effect=AssertionError('API must not be used')))
            ask = stack.enter_context(patch('task_packer.onboarding.ask',
                                           side_effect=lambda prompt, default='': default))
            ask_int = stack.enter_context(patch('task_packer.onboarding.ask_int',
                                               side_effect=lambda prompt, **kwargs: kwargs['default']))
            multiline = stack.enter_context(patch('task_packer.onboarding.ask_multiline',
                                                  side_effect=lambda prompt, **kwargs: kwargs['initial']))
            config, _ = gather_config(state, store)
        return config, ask, ask_int, multiline

    def test_new_manual_single_subtask_defaults_to_full(self):
        config, _, ask_int, multiline = self.manual_config()
        self.assertEqual(config.subtasks, [Subtask(1, 'Full', 100, 'No additional constraints')])
        ask_int.assert_called_once_with('How many subtasks are there?', default=1, minimum=1)
        self.assertEqual(multiline.call_args.kwargs['initial'], 'No additional constraints')

    def test_saved_single_subtask_preserves_points_and_constraints(self):
        # Loading or resuming does not silently change an existing score.
        config, ask, ask_int, multiline = self.manual_config(
            subtask_count=1, subtasks=[{'name': 'Existing', 'points': 80, 'constraints': 'n <= 20'}])
        self.assertEqual(config.subtasks, [Subtask(1, 'Existing', 80, 'n <= 20')])
        ask.assert_not_called()
        ask_int.assert_not_called()
        multiline.assert_not_called()
        restored = ProjectConfig.from_dict(WorkflowState(config=config).to_dict()['config'])
        self.assertEqual(restored.subtasks, config.subtasks)

    def test_saved_multiple_subtasks_preserve_scoring(self):
        config, _, _, _ = self.manual_config(subtask_count=2, subtasks=[
            {'name': 'Small', 'points': 30, 'constraints': 'n <= 5'},
            {'name': 'Large', 'points': 70, 'constraints': 'n <= 100'}])
        self.assertEqual(config.subtasks, [Subtask(1, 'Small', 30, 'n <= 5'),
                                          Subtask(2, 'Large', 70, 'n <= 100')])
        self.assertEqual(sum(group.points for group in config.subtasks), 100)

    def test_saved_zero_count_is_rejected_before_config_creation(self):
        with self.assertRaisesRegex(RuntimeError, 'at least one subtask'):
            self.manual_config(subtask_count=0, subtasks=[])

    def test_loading_imported_groups_preserves_binding_and_scoring(self):
        config = ProjectConfig(
            codename='imported', title='Synthetic', origin='Test', language_code='en',
            original_statement='Main constraints',
            subtasks=[Subtask(1, 'Small', 30, 'n <= 5', 'group-a'),
                      Subtask(2, 'Large', 70, 'n <= 100', 'group-b')])
        restored = WorkflowState.from_dict(WorkflowState(config=config).to_dict())
        self.assertEqual(restored.config.subtasks, config.subtasks)


if __name__ == '__main__':
    unittest.main()
