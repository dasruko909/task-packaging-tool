"""Model settings, strict preflight and persisted billing; no API or Solve calls."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from task_packer.costs import TokenUsage, conservative_project_estimate, usage_cost
from task_packer.models import ProjectConfig, Subtask, WorkflowState
from task_packer.openai_client import OpenAIClient
from task_packer.settings import API_BASE_URL, DEFAULT_MODEL, MODELS, Settings
from task_packer.storage import StateStore
from task_packer.verification import Verification
from task_packer.workflow import Workflow


SCHEMA = {'type': 'object', 'properties': {'text': {'type': 'string'}},
          'required': ['text'], 'additionalProperties': False}


def response(text='synthetic', input_tokens=100, output_tokens=20, cached=10, writes=5, **fields):
    return SimpleNamespace(
        id='synthetic-response', output_text=text,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              input_tokens_details=SimpleNamespace(cached_tokens=cached,
                                                                   cache_write_tokens=writes)),
        **fields,
    )


def fake_client(model=None, starting_usage=None, result=None, error=None):
    client = OpenAIClient(model=model, starting_usage=starting_usage)
    create = Mock(return_value=result if result is not None else response(), side_effect=error)
    client._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return client, create


class IsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self.contexts = contextlib.ExitStack()
        self.addCleanup(self.contexts.close)

    def enter_context(self, context):
        return self.contexts.enter_context(context)


class ModelBudgetTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.enter_context(patch.dict(os.environ, {}, clear=True))
        self.enter_context(patch('task_packer.openai_client.saved_api_key',
                                side_effect=AssertionError('Real credentials must never be read')))

    def test_default_and_empty_budget_have_no_cap(self):
        self.assertEqual(Settings.from_environment(), Settings(DEFAULT_MODEL, None))
        for value in ('', '  '):
            with self.subTest(value=value), patch.dict(os.environ, PACKER_BUDGET_USD=value):
                self.assertIsNone(OpenAIClient().settings.budget_usd)

    def test_invalid_cap_and_model_rejected_before_client_initialization(self):
        factory = Mock(side_effect=AssertionError('Must reject before client initialization'))
        with patch.dict(sys.modules, openai=SimpleNamespace(OpenAI=factory)):
            for value in ('0', '-1', 'NaN', 'nan', 'inf', '-inf', 'Infinity', 'broken', '1e999'):
                with self.subTest(value=value), patch.dict(os.environ, PACKER_BUDGET_USD=value):
                    with self.assertRaisesRegex(ValueError, 'PACKER_BUDGET_USD'):
                        OpenAIClient()
            for value in ('', 'gpt-unknown', 'gpt-6.1-sol-2026-10-01', ' gpt-6-luna'):
                with self.subTest(value=value), patch.dict(os.environ, OPENAI_MODEL=value):
                    with self.assertRaisesRegex(ValueError, 'Unsupported OPENAI_MODEL'):
                        OpenAIClient()
            with self.assertRaisesRegex(ValueError, 'Unsupported OPENAI_MODEL'):
                OpenAIClient(model='')
        factory.assert_not_called()

    def test_environment_selection_and_explicit_override(self):
        with patch.dict(os.environ, OPENAI_MODEL='gpt-6-luna', PACKER_BUDGET_USD=' 12.5 '):
            self.assertEqual(OpenAIClient().settings, Settings('gpt-6-luna', 12.5))
            self.assertEqual(OpenAIClient(model='gpt-6.1-sol').model, 'gpt-6.1-sol')

    def test_supported_models_send_compatible_text_json_and_media_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / 'source.pdf'; pdf.write_bytes(b'%PDF synthetic')
            png = Path(temporary) / 'source.png'; png.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic')
            for model in MODELS:
                with self.subTest(model=model), patch.dict(os.environ, OPENAI_MODEL=model):
                    client, create = fake_client()
                    self.assertEqual(client.generate('system', 'user', max_tokens=321), 'synthetic')
                    request = create.call_args.kwargs
                    self.assertEqual(request['model'], model)
                    self.assertEqual(request['max_output_tokens'], 321)
                    self.assertEqual(request['reasoning'], {'effort': 'low'})
                    self.assertEqual(request['service_tier'], 'default')
                    self.assertEqual(request['truncation'], 'disabled')
                    self.assertNotIn('temperature', request)
                    self.assertNotIn('text', request)
                    client.generate_json('system', 'user', schema=SCHEMA, schema_name='synthetic',
                                         attachments=[pdf, png], max_tokens=123)
                    request = create.call_args.kwargs
                    self.assertEqual(request['text']['format'], {
                        'type': 'json_schema', 'name': 'synthetic', 'schema': SCHEMA, 'strict': True})
                    media = request['input'][0]['content'][1:]
                    self.assertEqual([item['type'] for item in media], ['input_file', 'input_image'])
                    self.assertTrue(all(item['detail'] == 'low' for item in media))
                    self.assertTrue(media[0]['file_data'].startswith('data:application/pdf;base64,'))
                    self.assertTrue(media[1]['image_url'].startswith('data:image/png;base64,'))

    def test_sdk_constructor_disables_hidden_retries_and_uses_public_endpoint(self):
        fake = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response())))
        factory = Mock(return_value=fake)
        with patch.dict(os.environ, OPENAI_API_KEY='synthetic-not-a-key'), \
                patch.dict(sys.modules, openai=SimpleNamespace(OpenAI=factory)):
            OpenAIClient().generate('system', 'user')
        factory.assert_called_once_with(api_key='synthetic-not-a-key', base_url=API_BASE_URL, max_retries=0)
        fake.responses.create.assert_called_once()

    def test_unknown_endpoint_rejected_without_credentials_or_call(self):
        with patch.dict(os.environ, OPENAI_BASE_URL='https://synthetic.invalid/v1'):
            with self.assertRaisesRegex(ValueError, 'OPENAI_BASE_URL'):
                OpenAIClient().generate('system', 'user')

    def test_default_continues_after_old_five_dollar_stop_including_attachments(self):
        client, create = fake_client(starting_usage={'cost_usd': 7.5})
        for _ in range(3):
            client.generate('system', 'user')
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / 'source.pdf'; pdf.write_bytes(b'%PDF synthetic')
            client.generate('system', 'user', attachments=[pdf])
        self.assertEqual(create.call_count, 4)
        self.assertGreater(client.cost_usd, 7.5)

    def test_configured_cap_prevents_call_and_credential_read(self):
        with patch.dict(os.environ, PACKER_BUDGET_USD='0.0001'):
            client = OpenAIClient()
            with self.assertRaisesRegex(RuntimeError, 'not started.*project budget'):
                client.generate('system', 'user')
            self.assertEqual(client.events, [])
            self.assertEqual(client.cost_usd, 0)
        with patch.dict(os.environ, PACKER_BUDGET_USD='1'):
            client, create = fake_client(starting_usage={'cost_usd': 1})
            with self.assertRaisesRegex(RuntimeError, 'PACKER_BUDGET_USD'):
                client.generate('system', 'user')
            create.assert_not_called()

    def test_attachment_bound_includes_context_long_rates_and_cache_writes(self):
        # Astra: (1,050,000 - 6,000)*$25 + 6,000*$75 per million.
        expected = 26.55
        with patch.dict(os.environ, PACKER_BUDGET_USD='5'):
            client, create = fake_client()
            with self.assertRaisesRegex(RuntimeError, 'next request bound: \\$26.5500'):
                client.generate('system', 'user', attachments=[Path('not-read.pdf')])
            create.assert_not_called()
        with patch.dict(os.environ, PACKER_BUDGET_USD=str(expected)):
            client, _ = fake_client()
            self.assertAlmostEqual(client._ensure_budget(6000), expected)
        for model in MODELS:
            with self.subTest(model=model):
                client, _ = fake_client(model=model)
                bound = client._ensure_budget(6000)
                # Every combination of accepted input and output stays below the bound.
                for tokens in (1, 272_000, 272_001, 1_044_000):
                    for cached, writes in ((0, 0), (tokens, 0), (0, tokens)):
                        cost = usage_cost(model, TokenUsage(tokens, cached, 6000, writes))
                        self.assertLessEqual(cost, bound)

    def test_cap_boundary_allows_one_call_then_prevents_the_next(self):
        client, _ = fake_client(model='gpt-6.1-sol')
        exact_bound = client._ensure_budget(100, 4106)  # system + user + framing
        with patch.dict(os.environ, PACKER_BUDGET_USD=str(exact_bound)):
            client, create = fake_client(model='gpt-6.1-sol')
            self.assertEqual(client.generate('system', 'user', max_tokens=100), 'synthetic')
            with self.assertRaisesRegex(RuntimeError, 'not started'):
                client.generate('system', 'user', max_tokens=100)
            create.assert_called_once()

    def test_schema_and_utf8_text_are_included_in_preflight(self):
        client, create = fake_client(model='gpt-6.1-sol')
        schema = dict(SCHEMA, description='ź' * 5000)
        client.generate_json('system', 'ź', schema=schema, schema_name='synthetic', max_tokens=100)
        expected = len(('system' + 'ź' + json.dumps(schema, ensure_ascii=False) + 'synthetic').encode()) + 4096
        self.assertEqual(client.events[-1]['input_bound'], expected)
        bound = client.events[-1]['request_cost_bound_usd']
        with patch.dict(os.environ, PACKER_BUDGET_USD=str(bound - 0.000001)):
            blocked, call = fake_client(model='gpt-6.1-sol')
            with self.assertRaisesRegex(RuntimeError, 'not started'):
                blocked.generate_json('system', 'ź', schema=schema, schema_name='synthetic', max_tokens=100)
            call.assert_not_called()
        create.assert_called_once()

    def test_output_limits_and_context_checked_even_without_cap(self):
        for model in MODELS:
            for invalid in (0, -1, 128001, 1.5, True, '6000'):
                with self.subTest(model=model, invalid=invalid):
                    client, create = fake_client(model=model)
                    with self.assertRaisesRegex(ValueError, 'max_tokens'):
                        client.generate('system', 'user', max_tokens=invalid)
                    create.assert_not_called()
            client, create = fake_client(model=model)
            with self.assertRaisesRegex(RuntimeError, 'context limit'):
                client.generate('system', 'a' * 1_050_000)
            create.assert_not_called()

    def test_actual_cost_uses_cache_writes_and_per_request_long_context(self):
        for model in MODELS:
            for tokens in (272_000, 272_001):
                with self.subTest(model=model, tokens=tokens):
                    spec = MODELS[model]
                    input_multiplier = 2 if tokens > 272_000 else 1
                    output_multiplier = 1.5 if tokens > 272_000 else 1
                    expected = ((tokens - 30) * spec.input_price * input_multiplier
                                + 10 * spec.cached_input_price * input_multiplier
                                + 20 * spec.cache_write_price * input_multiplier
                                + 50 * spec.output_price * output_multiplier) / 1_000_000
                    self.assertAlmostEqual(usage_cost(model, TokenUsage(tokens, 10, 50, 20)), expected)

    def test_estimates_follow_selected_model_and_do_not_treat_project_as_one_call(self):
        for model, expected in (('gpt-6-astra', 3.825), ('gpt-6.1-sol', .765), ('gpt-6-luna', .03825)):
            with self.subTest(model=model), patch.dict(os.environ, OPENAI_MODEL=model):
                self.assertEqual(conservative_project_estimate(1, 'standard'), (60_000, 64_500, expected))
        with patch.dict(os.environ, OPENAI_MODEL='gpt-6-astra'):
            inputs, outputs, cost = conservative_project_estimate(20, 'standard', 'gpt-6.1-sol')
            self.assertGreater(inputs, 272_000)
            self.assertEqual(cost, (inputs * 2 + outputs * 10) / 1_000_000)

    def test_empty_failed_and_interrupted_requests_preserve_accounting(self):
        for text in ('', '   '):
            client, _ = fake_client(result=response(text=text))
            with self.assertRaisesRegex(RuntimeError, 'no text'):
                client.generate('system', 'user')
            self.assertEqual(client.input_tokens, 100)
            self.assertGreater(client.cost_usd, 0)
            self.assertEqual(client.events[-1]['status'], 'empty')
        failure = RuntimeError('synthetic failure')
        failure.body = {'usage': {'input_tokens': 100, 'output_tokens': 20,
                                  'input_tokens_details': {'cached_tokens': 10, 'cache_write_tokens': 5}}}
        client, _ = fake_client(error=failure)
        with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
            client.generate('system', 'user')
        self.assertEqual(client.input_tokens, 100)
        self.assertEqual(client.cache_write_tokens, 5)
        self.assertEqual(client.unconfirmed_cost_usd, 0)
        self.assertTrue(client.events[-1]['usage_confirmed'])
        for failure in (ConnectionError('synthetic interruption'), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                client, create = fake_client(error=failure, starting_usage={'cost_usd': 1.25})
                with self.assertRaises(KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else RuntimeError):
                    client.generate('system', 'user')
                self.assertEqual(client.cost_usd, 1.25)
                self.assertEqual(client.input_tokens, 0)
                self.assertGreater(client.unconfirmed_cost_usd, 0)
                self.assertEqual(client.events[-1]['status'], 'failed')
                create.assert_called_once()
                cap = 1.25 + client.unconfirmed_cost_usd
                with patch.dict(os.environ, PACKER_BUDGET_USD=str(cap)):
                    resumed, call = fake_client(starting_usage=client.usage_dict())
                    with self.assertRaisesRegex(RuntimeError, 'not started'):
                        resumed.generate('system', 'user')
                    call.assert_not_called()

    def test_failed_response_and_missing_usage_success_are_accounted(self):
        client, _ = fake_client(result=response(status='failed'))
        with self.assertRaisesRegex(RuntimeError, 'failed response'):
            client.generate('system', 'user')
        self.assertGreater(client.cost_usd, 0)
        self.assertEqual(client.events[-1]['status'], 'failed')
        client, _ = fake_client(result=SimpleNamespace(output_text='synthetic', usage=None))
        self.assertEqual(client.generate('system', 'user'), 'synthetic')
        self.assertEqual(client.cost_usd, 0)
        self.assertGreater(client.unconfirmed_cost_usd, 0)

    def test_invalid_saved_usage_cannot_bypass_cap(self):
        for value in ({'cost_usd': float('nan')}, {'cost_usd': float('inf')},
                      {'cost_usd': -1}, {'unconfirmed_cost_usd': -1}, {'input_tokens': -1}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Saved API usage'):
                OpenAIClient(starting_usage=value)


class PersistedBudgetTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.enter_context(patch.dict(os.environ, {}, clear=True))
        self.enter_context(contextlib.redirect_stdout(io.StringIO()))
        temporary = self.enter_context(tempfile.TemporaryDirectory())
        previous = Path.cwd()
        os.chdir(temporary)
        self.addCleanup(os.chdir, previous)
        self.config = ProjectConfig(codename='synthetic', title='Synthetic', origin='test',
                                    language_code='en', original_statement='Synthetic',
                                    subtasks=[Subtask(1, 'Full', 100, '')])
        self.config.package_dir.mkdir(parents=True)
        self.store = StateStore('synthetic')
        self.state = WorkflowState(config=self.config, completed=['import'])
        self.store.save(self.state)

    def test_resume_mixed_models_empty_failure_and_cap_preserve_saved_progress(self):
        expected_cost = 0
        for model in MODELS:
            state = self.store.load()
            client, _ = fake_client(model=model, starting_usage=state.usage)
            workflow = Workflow(state, self.store, client)
            workflow._generate('system', 'user', max_tokens=6000)
            expected_cost += usage_cost(model, TokenUsage(100, 10, 20, 5))
        state = self.store.load()
        self.assertEqual(state.usage['input_tokens'], 300)
        self.assertAlmostEqual(state.usage['cost_usd'], expected_cost)
        client, _ = fake_client(model='gpt-6-luna', starting_usage=state.usage, result=response(text=''))
        with self.assertRaisesRegex(RuntimeError, 'no text'):
            Workflow(state, self.store, client)._generate('system', 'user', max_tokens=100)
        expected_cost += usage_cost('gpt-6-luna', TokenUsage(100, 10, 20, 5))
        state = self.store.load()
        self.assertAlmostEqual(state.usage['cost_usd'], expected_cost)
        client, _ = fake_client(starting_usage=state.usage, error=ConnectionError('synthetic'))
        with self.assertRaises(RuntimeError):
            Workflow(state, self.store, client)._generate('system', 'user', max_tokens=100)
        state = self.store.load()
        self.assertEqual(state.usage['input_tokens'], 400)
        self.assertGreater(state.usage['unconfirmed_cost_usd'], 0)
        saved_usage = dict(state.usage)
        with patch.dict(os.environ, PACKER_BUDGET_USD='0.0001'):
            client, create = fake_client(starting_usage=state.usage)
            with self.assertRaisesRegex(RuntimeError, 'not started'):
                Workflow(state, self.store, client)._generate('system', 'user', max_tokens=100)
            create.assert_not_called()
        self.assertEqual(self.store.load().usage, saved_usage)
        self.assertEqual(self.store.load().completed, ['import'])
        verification = Verification(self.config, usage=saved_usage)
        self.assertEqual(verification._api_history()['recorded_calls'], 5)
        details = verification._ai_details()
        self.assertIsNone(details['pricing'])
        self.assertEqual(details['models'], sorted(MODELS))
        self.assertAlmostEqual(details['cost_usd'], expected_cost)

    def test_onboarding_saves_empty_response_cost_and_model_history(self):
        from task_packer.onboarding import _detect_subtasks_with_ai
        client, _ = fake_client(model='gpt-6.1-sol', result=response(text=''))
        with patch('task_packer.openai_client.OpenAIClient', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'no text'):
                _detect_subtasks_with_ai(self.state, self.store, 'Synthetic', [], [])
        self.assertEqual(self.store.load().usage['input_tokens'], 100)
        self.assertEqual(Verification(self.config)._api_history()['models'], ['gpt-6.1-sol'])

    def test_report_formats_no_cap_and_current_model_budget_without_repricing(self):
        usage = {'input_tokens': 300000, 'cached_input_tokens': 10000,
                 'output_tokens': 15000, 'cost_usd': 1.2345, 'unconfirmed_cost_usd': .25}
        verification = Verification(self.config, usage=usage)
        with patch.dict(os.environ, OPENAI_MODEL='gpt-6-luna'):
            verification.save()
            report = json.loads((self.config.package_dir / 'verification/report.json').read_text())
            details = report['ai_usage']
            self.assertIsNone(details['project_budget_usd'])
            self.assertIsNone(details['budget_used_percent'])
            self.assertEqual(details['selected_model'], 'gpt-6-luna')
            self.assertEqual(details['cost_usd'], 1.2345)
            markdown = (self.config.package_dir / 'verification/report.md').read_text()
            self.assertIn('no cap', markdown)
            self.assertIn('$1.23450000', markdown)
            self.assertNotIn('None', markdown)
        with patch.dict(os.environ, OPENAI_MODEL='gpt-6.1-sol', PACKER_BUDGET_USD='10'):
            details = verification._ai_details()
            self.assertEqual(details['project_budget_usd'], 10)
            self.assertEqual(details['budget_used_percent'], 14.84)
            self.assertEqual(details['selected_model'], 'gpt-6.1-sol')

    def test_restore_earlier_content_does_not_refund_mixed_model_cost_or_reserves(self):
        from task_packer.revisions import checkpoint, restore
        previous = checkpoint(self.state, self.store, 'synthetic checkpoint')
        self.state.usage = {'input_tokens': 300, 'cached_input_tokens': 30,
                            'cache_write_tokens': 15, 'output_tokens': 60,
                            'cost_usd': 1.25, 'unconfirmed_cost_usd': .25}
        self.store.save(self.state)
        expected = dict(self.state.usage)
        restore(self.state, self.store, previous.name)
        self.assertEqual(self.store.load().usage, expected)


if __name__ == '__main__':
    unittest.main()
