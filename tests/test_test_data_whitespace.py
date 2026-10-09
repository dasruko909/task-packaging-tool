"""Exact preservation of generated and imported test data."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from task_packer.content_review import audit_input
from task_packer.input_sources import import_existing_materials
from task_packer.models import ProjectConfig, Subtask, WorkflowState
from task_packer.parsing import ModelFormatError, require_test_list
from task_packer.preview import Preview
from task_packer.statement_format import example_size_errors, imported_examples
from task_packer.workflow import Workflow


class TestDataWhitespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_cwd = Path.cwd()
        self._temporary = tempfile.TemporaryDirectory()
        os.chdir(self._temporary.name)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._temporary.cleanup()

    @staticmethod
    def _config(**updates) -> ProjectConfig:
        values = dict(
            codename="spaces",
            title="Whitespace",
            origin="Synthetic test",
            language_code="en",
            original_statement="Synthetic statement.",
            subtasks=[Subtask(1, "Full", 100, "No additional constraints")],
            task_type="standard",
        )
        values.update(updates)
        return ProjectConfig(**values)

    @staticmethod
    def _workflow(config: ProjectConfig, *, completed: list[str] | None = None) -> Workflow:
        state = WorkflowState(config=config, completed=completed or [])
        store = Mock(codename=config.codename)
        store.path = Path(".packer-projects") / config.codename / "state.json"
        store.path.parent.mkdir(parents=True, exist_ok=True)
        return Workflow(state, store, Mock())

    def test_parser_preserves_payloads_but_trims_descriptions(self) -> None:
        data = {
            "tests": [
                {
                    "input": " \talpha  \r\n\r\n",
                    "output": "omega\t ",
                    "description": "  meaningful spacing  ",
                },
                {"input": "", "output": " \t\r\n", "description": " empty input "},
            ]
        }

        self.assertEqual(
            require_test_list(data, minimum=2),
            [
                {
                    "input": " \talpha  \r\n\r\n",
                    "output": "omega\t ",
                    "description": "meaningful spacing",
                },
                {"input": "", "output": " \t\r\n", "description": "empty input"},
            ],
        )

    def test_parser_rejects_missing_or_non_text_payload_fields(self) -> None:
        for test in (
            {"description": "missing input"},
            {"input": None, "description": "wrong input type"},
            {"input": "ok", "output": None, "description": "wrong output type"},
        ):
            with self.subTest(test=test), self.assertRaises(ModelFormatError):
                require_test_list({"tests": [test]})

    def test_test_plan_keeps_exact_payloads(self) -> None:
        config = self._config()
        workflow = self._workflow(config)
        draft = {
            "tests": [
                {"input": "  one\r\n", "output": "1\t ", "description": " first "},
                {"input": "", "output": "2", "description": " second "},
            ],
            "subtasks": [{"index": 1, "generator_runs": 18, "corner_tests": 0}],
        }

        def review(**kwargs) -> None:
            data = copy.deepcopy(draft)
            kwargs["validator"](data)
            kwargs["accept"](data)

        workflow._review_json = review
        with patch("task_packer.test_generation.test_blueprint_prompt", return_value=("", "")):
            workflow._prepare_test_plan()

        self.assertEqual(config.test_plan["tests"], [
            {"input": "  one\r\n", "output": "1\t ", "description": "first"},
            {"input": "", "output": "2", "description": "second"},
        ])

    def test_test_plan_validation_decides_whitespace_only_output_is_invalid(self) -> None:
        config = self._config()
        workflow = self._workflow(config)
        draft = {
            "tests": [
                {"input": "", "output": " \t\r\n", "description": "empty output"},
                {"input": "two", "output": "2", "description": "second"},
            ],
            "subtasks": [{"index": 1, "generator_runs": 18, "corner_tests": 0}],
        }

        def review(**kwargs) -> None:
            kwargs["validator"](copy.deepcopy(draft))

        workflow._review_json = review
        with (
            patch("task_packer.test_generation.test_blueprint_prompt", return_value=("", "")),
            self.assertRaisesRegex(ModelFormatError, "non-empty 'output'"),
        ):
            workflow._prepare_test_plan()

    def test_statement_saves_planned_samples_without_adding_newlines(self) -> None:
        planned = [
            {"input": " \tone  \n\n", "output": "first\t ", "description": "First"},
            {"input": "two\r\nlines", "output": "second\r\n", "description": "Second"},
        ]
        config = self._config(test_plan={
            "tests": copy.deepcopy(planned),
            "subtasks": [{"index": 1, "generator_runs": 18, "corner_tests": 0}],
            "total_tests": 20,
        })
        workflow = self._workflow(config, completed=["test_plan"])
        markdown = (
            '# Task\n\n``` {.example input_file="0a.in" output_file="0a.out"}\nFirst\n```\n\n'
            '``` {.example input_file="0b.in" output_file="0b.out"}\nSecond\n```\n'
        )
        draft = {"statement_markdown": markdown, "tests": copy.deepcopy(planned)}

        def review(**kwargs) -> None:
            data = copy.deepcopy(draft)
            kwargs["validator"](data)
            kwargs["accept"](data)

        workflow._review_json = review
        with (
            patch("task_packer.workflow.statement_prompt", return_value=("", "")),
            patch("task_packer.workflow.format_subtasks", side_effect=lambda text, config: text),
            patch("task_packer.workflow.statement_validation_errors", return_value=[]),
        ):
            workflow.statement()

        for index, test in enumerate(planned):
            letter = chr(ord("a") + index)
            self.assertEqual(
                (config.package_dir / "tests" / "in" / f"0{letter}.in").read_bytes(),
                test["input"].encode(),
            )
            self.assertEqual(
                (config.package_dir / "tests" / "out" / f"0{letter}.out").read_bytes(),
                test["output"].encode(),
            )

    def test_statement_requires_exact_planned_sample_payloads(self) -> None:
        planned = [
            {"input": "one\n", "output": "1\n", "description": "First"},
            {"input": "two\n", "output": "2\n", "description": "Second"},
        ]
        config = self._config(test_plan={
            "tests": copy.deepcopy(planned),
            "subtasks": [{"index": 1, "generator_runs": 18, "corner_tests": 0}],
        })
        workflow = self._workflow(config, completed=["test_plan"])
        changed = copy.deepcopy(planned)
        changed[0]["input"] = " one\n"
        draft = {
            "statement_markdown": (
                '``` {.example input_file="0a.in" output_file="0a.out"}\nFirst\n```\n\n'
                '``` {.example input_file="0b.in" output_file="0b.out"}\nSecond\n```'
            ),
            "tests": changed,
        }

        def review(**kwargs) -> None:
            kwargs["validator"](copy.deepcopy(draft))

        workflow._review_json = review
        with (
            patch("task_packer.workflow.statement_prompt", return_value=("", "")),
            patch("task_packer.workflow.format_subtasks", side_effect=lambda text, config: text),
            patch("task_packer.workflow.statement_validation_errors", return_value=[]),
            self.assertRaisesRegex(ModelFormatError, "do not match"),
        ):
            workflow.statement()

    def test_corner_files_keep_blank_lines_crlf_and_empty_content(self) -> None:
        payloads = [" \talpha  \n\n", "beta\r\n\r\n", "gamma\t ", ""]
        config = self._config(test_plan={
            "tests": [],
            "subtasks": [{"index": 1, "generator_runs": 1, "corner_tests": len(payloads)}],
        })
        workflow = self._workflow(config)
        workflow._statement_text = lambda: "Synthetic statement"
        draft = {
            "tests": [
                {"input": payload, "description": f" case {index} "}
                for index, payload in enumerate(payloads, 1)
            ]
        }

        def review(**kwargs) -> None:
            data = copy.deepcopy(draft)
            kwargs["validator"](data)
            kwargs["accept"](data)

        workflow._review_json = review
        with patch("task_packer.test_generation.corner_prompt", return_value=("", "")):
            workflow.corner_cases()

        for offset, payload in enumerate(payloads, 1):
            name = f"1{chr(ord('a') + offset)}.in"
            self.assertEqual(
                (config.package_dir / "tests" / "in" / name).read_bytes(),
                payload.encode(),
            )

    def test_shared_previews_embed_payloads_without_trimming(self) -> None:
        input_data = " \talpha  \r\n\r\n"
        output_data = "omega\t "
        empty_preview = Preview()._tests_preview([
            {"input": input_data, "output": output_data, "description": " test "},
            {"input": "", "output": "", "description": "empty"},
        ])

        self.assertIn(f"```text\n{input_data}\n```", empty_preview)
        self.assertIn(f"```text\n{output_data}\n```", empty_preview)
        self.assertIn("```text\n\n```", empty_preview)

    def test_imported_samples_keep_line_endings_in_review_and_preview(self) -> None:
        source = Path("input/spaces/package")
        config = self._config(
            existing_tests=True,
            sample_files=[{"input": "0a.in", "output": "0a.out"}],
            input_package=str(source),
        )
        input_data = " \talpha  \r\n\r\n"
        output_data = "omega\t "
        source_input = source / "tests" / "in" / "0a.in"
        source_output = source / "tests" / "out" / "0a.out"
        source_input.parent.mkdir(parents=True)
        source_output.parent.mkdir(parents=True)
        source_input.write_bytes(input_data.encode())
        source_output.write_bytes(output_data.encode())
        import_existing_materials(config)
        input_path = config.package_dir / "tests" / "in" / "0a.in"
        output_path = config.package_dir / "tests" / "out" / "0a.out"
        markdown = '``` {.example input_file="0a.in" output_file="0a.out"}\nExplanation\n```\n'

        samples = imported_examples(config)
        self.assertEqual(samples[0]["input"], input_data)
        self.assertEqual(samples[0]["output"], output_data)
        audit = json.loads(audit_input(config, markdown))
        self.assertEqual(audit["samples"][0]["input"], input_data)
        self.assertEqual(audit["samples"][0]["output"], output_data)

        preview = Preview()
        preview.config = config
        preview.state = WorkflowState(config=config)
        rendered = preview._statement_examples_preview(markdown, None)
        self.assertIn(f"```text\n{input_data}\n```", rendered)
        self.assertIn(f"```text\n{output_data}\n```", rendered)
        self.assertEqual(input_path.read_bytes(), input_data.encode())
        self.assertEqual(output_path.read_bytes(), output_data.encode())

    def test_example_size_uses_exact_payload_length(self) -> None:
        at_limit = [{"input": "x" * 2048, "output": "", "description": "limit"}]
        over_limit = [{"input": "x" * 2049, "output": "", "description": "over"}]
        self.assertEqual(example_size_errors(at_limit), [])
        self.assertEqual(len(example_size_errors(over_limit)), 1)


if __name__ == "__main__":
    unittest.main()
