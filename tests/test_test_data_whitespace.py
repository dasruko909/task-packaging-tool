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
from task_packer.execution import ExecutionError
from task_packer.input_sources import import_existing_materials
from task_packer.models import ProjectConfig, Subtask, WorkflowState
from task_packer.parsing import ModelFormatError, require_test_list
from task_packer.preview import Preview
from task_packer.statement_format import example_size_errors, imported_examples
from task_packer.verification import Verification
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

    def test_test_plan_accepts_empty_and_whitespace_only_outputs(self) -> None:
        config = self._config()
        workflow = self._workflow(config)
        draft = {
            "tests": [
                {"input": "one", "output": "", "description": " empty output "},
                {"input": "two", "output": " \t\r\n", "description": " whitespace output "},
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
            {"input": "one", "output": "", "description": "empty output"},
            {"input": "two", "output": " \t\r\n", "description": "whitespace output"},
        ])

    def test_test_plan_rejects_missing_or_non_text_output(self) -> None:
        for invalid in ({}, {"output": None}):
            with self.subTest(invalid=invalid):
                config = self._config()
                workflow = self._workflow(config)
                first = {"input": "one", "description": "first", **invalid}
                draft = {
                    "tests": [
                        first,
                        {"input": "two", "output": "", "description": "second"},
                    ],
                    "subtasks": [{"index": 1, "generator_runs": 18, "corner_tests": 0}],
                }

                def review(**kwargs) -> None:
                    kwargs["validator"](copy.deepcopy(draft))

                workflow._review_json = review
                with (
                    patch("task_packer.test_generation.test_blueprint_prompt", return_value=("", "")),
                    self.assertRaises(ModelFormatError),
                ):
                    workflow._prepare_test_plan()

    def test_statement_saves_planned_samples_without_adding_newlines(self) -> None:
        planned = [
            {"input": " \tone  \n\n", "output": "", "description": "First"},
            {"input": "two\r\nlines", "output": " \t\r\n", "description": "Second"},
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

    def test_statement_rejects_missing_or_non_text_output(self) -> None:
        planned = [
            {"input": "one", "output": "", "description": "First"},
            {"input": "two", "output": " \t", "description": "Second"},
        ]
        markdown = (
            '``` {.example input_file="0a.in" output_file="0a.out"}\nFirst\n```\n\n'
            '``` {.example input_file="0b.in" output_file="0b.out"}\nSecond\n```'
        )
        for invalid in ({}, {"output": 3}):
            with self.subTest(invalid=invalid):
                config = self._config(test_plan={
                    "tests": copy.deepcopy(planned),
                    "subtasks": [{"index": 1, "generator_runs": 18, "corner_tests": 0}],
                })
                workflow = self._workflow(config, completed=["test_plan"])
                tests = copy.deepcopy(planned)
                tests[0].pop("output")
                tests[0].update(invalid)
                draft = {"statement_markdown": markdown, "tests": tests}

                def review(**kwargs) -> None:
                    kwargs["validator"](copy.deepcopy(draft))

                workflow._review_json = review
                with (
                    patch("task_packer.workflow.statement_prompt", return_value=("", "")),
                    patch("task_packer.workflow.format_subtasks", side_effect=lambda text, config: text),
                    patch("task_packer.workflow.statement_validation_errors", return_value=[]),
                    self.assertRaises(ModelFormatError),
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

    def test_code_fences_add_only_a_required_delimiter_newline(self) -> None:
        cases = [
            ("value", "```text\nvalue\n```", 3),
            ("value\n", "```text\nvalue\n```", 3),
            ("value\r\n", "```text\nvalue\r\n```", 3),
            ("value\n\n", "```text\nvalue\n\n```", 4),
            ("", "```text\n```", 2),
        ]
        for payload, expected, line_count in cases:
            with self.subTest(payload=payload):
                fenced = Preview._code_fence(payload, "text")
                self.assertEqual(fenced, expected)
                self.assertEqual(len(fenced.splitlines()), line_count)

    def test_shared_previews_embed_payloads_without_trimming(self) -> None:
        input_data = " \talpha  \r\n\r\n"
        output_data = "omega\t "
        empty_preview = Preview()._tests_preview([
            {"input": input_data, "output": output_data, "description": " test "},
            {"input": "", "output": "", "description": "empty"},
        ])

        self.assertIn(f"```text\n{input_data}```", empty_preview)
        self.assertIn(f"```text\n{output_data}\n```", empty_preview)
        self.assertIn("```text\n```", empty_preview)

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
        self.assertIn(f"```text\n{input_data}```", rendered)
        self.assertIn(f"```text\n{output_data}\n```", rendered)
        self.assertEqual(input_path.read_bytes(), input_data.encode())
        self.assertEqual(output_path.read_bytes(), output_data.encode())

    def test_example_size_uses_exact_payload_length(self) -> None:
        at_limit = [{"input": "x" * 2048, "output": "", "description": "limit"}]
        over_limit = [{"input": "x" * 2049, "output": "", "description": "over"}]
        self.assertEqual(example_size_errors(at_limit), [])
        self.assertEqual(len(example_size_errors(over_limit)), 1)

    def test_verification_forwards_exact_files_and_lets_validator_accept_empty_input(self) -> None:
        config = self._config(existing_tests=False)
        root = config.package_dir
        for directory in (
            "checker", "generators", "public", "solutions", "tests/in", "tests/out",
            "validators", "verification/mutants",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        for name in (
            "checker/checker.cpp", "generators/generator_01.cpp", "solutions/solution_01.cpp",
            "validators/input_validator.cpp", "verification/brute.cpp",
            "verification/small_generator.cpp", "verification/mutants/first.cpp",
            "verification/mutants/second.cpp",
            "checker/not-selected.cpp", "solutions/helper.cpp", "solutions/notes.txt",
            "generators/unrelated.java", "solutions/include/helper.hpp",
            "generators/data/profile.txt",
        ):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("", encoding="utf-8")

        input_data = "first\r\n\r\n"
        output_data = "answer \t\r\n"
        (root / "tests/in/1a.in").write_bytes(input_data.encode())
        (root / "tests/out/1a.out").write_bytes(output_data.encode())
        (root / "tests/in/1b.in").write_bytes(b"placeholder")
        (root / "tests/out/1b.out").write_bytes(b"")
        manifest = {
            "checker": {"name": "checker.cpp"},
            "solutions": [{
                "name": "solution_01.cpp",
                "additional_files_names": ["include/helper.hpp"],
            }],
            "generators": [{
                "name": "generator_01.cpp",
                "additional_files_names": ["data/profile.txt"],
            }],
            "test_generation": [{
                "generator": "generator_01.cpp", "filename": "1b.in", "parameters": "",
            }],
            "test_groups": [{
                "name": "01",
                "tests": [
                    {"input": "1a.in", "output": "1a.out"},
                    {"input": "1b.in", "output": "1b.out"},
                ],
            }],
        }
        (root / "config.json").write_text(json.dumps(manifest), encoding="utf-8")
        (root / "verification/mutants.json").write_text(json.dumps([
            {"name": "first.cpp", "description": "first"},
            {"name": "second.cpp", "description": "second"},
        ]), encoding="utf-8")

        validations: list[str] = []
        solution_inputs: list[str] = []
        mutant_inputs: list[str] = []
        comparisons: list[tuple[str, str, str]] = []
        compiled: list[tuple[str, tuple[str, ...]]] = []

        class FakeRunner:
            def __init__(self, directory: Path, includes: Path):
                self.directory = directory

            def compile(self, source: Path, **kwargs) -> list[str]:
                compiled.append((source.name, tuple(path.name for path in kwargs.get('dependencies', []))))
                return [str(source)]

            def program(self, source: Path, data: str = "", **kwargs) -> str:
                if source.name == "input_validator.cpp":
                    validations.append(data)
                    return ""
                if source.name in {"generator_01.cpp", "small_generator.cpp", "brute.cpp"}:
                    return ""
                if source.parent.name == "mutants":
                    mutant_inputs.append(data)
                    raise ExecutionError("synthetic mutant failure", source)
                if source.name == "solution_01.cpp":
                    solution_inputs.append(data)
                    return output_data if data == input_data else ""
                raise AssertionError(f"Unexpected program: {source}")

        verification = Verification(config)
        verification.record = lambda *args: None

        def check_answer(runner, judge, data, reference, candidate):
            comparisons.append((data, reference, candidate))
            return 100

        verification.check_answer = check_answer
        with patch("task_packer.verification.Runner", FakeRunner):
            verification.run()

        self.assertEqual(validations[0], input_data)
        self.assertIn("", validations)
        self.assertIn(input_data, solution_inputs)
        self.assertIn(input_data, mutant_inputs)
        self.assertIn((input_data, output_data, output_data), comparisons)
        self.assertEqual((root / "tests/in/1b.in").read_bytes(), b"")
        self.assertNotIn('not-selected.cpp', {name for name, _ in compiled})
        self.assertNotIn('helper.cpp', {name for name, _ in compiled})
        self.assertNotIn('notes.txt', {name for name, _ in compiled})
        self.assertNotIn('unrelated.java', {name for name, _ in compiled})
        self.assertIn(('solution_01.cpp', ('helper.hpp',)), compiled)
        self.assertIn(('generator_01.cpp', ('profile.txt',)), compiled)

    def test_checker_files_keep_exact_line_endings(self) -> None:
        config = self._config()
        verification = Verification(config)
        runner = Mock()
        runner.directory = Path("checker-work")
        runner.directory.mkdir()
        input_data = "input\r\n\r\n"
        reference = " expected \t\r\n"
        candidate = "candidate"

        def program(source, **kwargs):
            paths = kwargs["args"]
            self.assertEqual(kwargs["readable"], [Path(path) for path in paths])
            self.assertEqual(Path(paths[0]).read_bytes(), input_data.encode())
            self.assertEqual(Path(paths[1]).read_bytes(), reference.encode())
            self.assertEqual(Path(paths[2]).read_bytes(), candidate.encode())
            return "100\naccepted\n"

        runner.program.side_effect = program
        self.assertEqual(
            verification.check_answer(
                runner, Path("checker.cpp"), input_data, reference, candidate
            ),
            100,
        )


if __name__ == "__main__":
    unittest.main()
