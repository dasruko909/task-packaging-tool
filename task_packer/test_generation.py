"""Test planning and generation stages."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any
from .console import SavedExit, choose, heading
from .models import Subtask
from .parsing import ModelFormatError, parse_json_object, require_string, require_test_list
from .prompts import *
from .storage import atomic_write_text
from .input_sources import statement_attachments
from .registry import load_manifest, solution_path
from .solve4 import source_extension
from .statement_format import MAX_TESTS_PER_SUBTASK, example_size_errors, test_filename
from .test_plan import counts_for, response_schema, total_tests


class TestGeneration:
    def _test_plan(self) -> dict[str, Any]:
        return self.config.test_plan if isinstance(self.config.test_plan, dict) else {}


    def _planned_tests(self) -> list[dict[str, Any]]:
        tests = self._test_plan().get("tests")
        return tests if isinstance(tests, list) else []


    def _planned_counts(self, subtask_index: int) -> tuple[int, int] | None:
        return counts_for(self._test_plan(), subtask_index)


    def _planned_total_tests(self) -> int | None:
        return total_tests(self._test_plan())


    def _prepare_test_plan(self) -> None:
        if self.config.existing_tests or self.config.task_type == "interactive":
            return
        if self._test_plan():
            if self.state.is_done('test_plan'):
                return
            self.state.drafts.setdefault('test_plan', json.dumps(self.config.test_plan, ensure_ascii=False))
            self.config.test_plan = {}
            self._save()
        print("Planning public samples and the total number of tests…")
        system, user = test_blueprint_prompt(self.config)

        def validate(data):
            tests = require_test_list(data, minimum=2)
            data["tests"] = tests
            if len(tests) > MAX_TESTS_PER_SUBTASK:
                raise ModelFormatError(
                    f"At most {MAX_TESTS_PER_SUBTASK} sample tests can be prepared."
                )
            if any("output" not in test for test in tests):
                raise ModelFormatError("Every public sample must include an 'output' field.")
            if example_size_errors(tests):
                raise ModelFormatError(' '.join(example_size_errors(tests)))
            subtasks = data.get("subtasks")
            if not isinstance(subtasks, list) or len(subtasks) != len(self.config.subtasks):
                raise ModelFormatError("The test plan must contain an entry for every subtask.")
            normalized_subtasks: list[dict[str, int]] = []
            for expected, raw_item in zip(self.config.subtasks, subtasks):
                if not isinstance(raw_item, dict):
                    raise ModelFormatError("A subtask plan must be an object.")
                index = raw_item.get("index")
                generator_runs = raw_item.get("generator_runs")
                corner_tests = raw_item.get("corner_tests")
                if (type(index) is not int or type(generator_runs) is not int
                        or type(corner_tests) is not int):
                    raise ModelFormatError(
                        "A subtask plan must contain index, generator_runs, and corner_tests."
                    )
                if index != expected.index:
                    raise ModelFormatError("The test plan has an incorrect subtask order.")
                if generator_runs < 1:
                    raise ModelFormatError("Every subtask needs at least one generator run.")
                if corner_tests < 0:
                    raise ModelFormatError("corner_tests cannot be negative.")
                if generator_runs + corner_tests > MAX_TESTS_PER_SUBTASK:
                    raise ModelFormatError(
                        f"Subtask {index} can have at most "
                        f"{MAX_TESTS_PER_SUBTASK} tests in total (generated and manual)."
                    )
                normalized_subtasks.append(
                    {
                        "index": index,
                        "generator_runs": generator_runs,
                        "corner_tests": corner_tests,
                    }
                )
            computed_total = len(tests) + sum(
                item["generator_runs"] + item["corner_tests"] for item in normalized_subtasks
            )
            if not 20 <= computed_total <= 100:
                raise ModelFormatError("The total number of tests must be between 20 and 100.")

        def accept(data):
            data["total_tests"] = len(data["tests"]) + sum(
                item["generator_runs"] + item["corner_tests"]
                for item in data["subtasks"]
            )
            self.config.test_plan = data
            print(f"Planned {data['total_tests']} tests, including {len(data['tests'])} samples.")
        self._review_json(key="test_plan", label="test plan and public samples",
                          system=system, user=user, validator=validate, accept=accept,
                          max_tokens=5000, attachments=statement_attachments(self.config),
                          response_schema=response_schema(len(self.config.subtasks)))


    def generators(self) -> None:
        if self.config.existing_tests:
            print("Existing tests detected: generation is skipped and nothing is overwritten.")
            return
        statement = self._statement_text()
        extension = source_extension(self.config.generator_language)
        for subtask in self.config.subtasks:
            key = f"generator:{subtask.index}"
            system, user = generator_prompt(self.config, subtask, statement)

            def validate(data: dict[str, Any]) -> None:
                require_string(data, "code")
                require_string(data, "description")

            def accept(data: dict[str, Any], item: Subtask = subtask) -> None:
                atomic_write_text(
                    self.config.package_dir
                    / "generators"
                    / f"generator_{item.index:02d}.{extension}",
                    require_string(data, "code") + "\n",
                )
                atomic_write_text(
                    self.store.path.parent / "notes" / f"generator_{item.index:02d}.md",
                    require_string(data, "description") + "\n",
                )

            self._review_json(
                key=key,
                label=f"generator for subtask {subtask.index}",
                system=system,
                user=user,
                validator=validate,
                accept=accept,
                code_preview={"code": f"generator_{subtask.index:02d}.{extension}"},
            )


    def corner_cases(self) -> None:
        if self.config.existing_tests:
            return
        statement = self._statement_text()
        for subtask in self.config.subtasks:
            counts = self._planned_counts(subtask.index)
            if counts is not None and counts[1] == 0:
                continue
            key = f"corners:{subtask.index}"
            system, user = corner_prompt(
                self.config,
                subtask,
                statement,
                None if counts is None else counts[1],
            )

            def validate(data: dict[str, Any]) -> None:
                tests = require_test_list(data, minimum=2 if counts is None else counts[1] or 1)
                data["tests"] = tests
                expected = counts[1] if counts is not None else None
                if expected is not None and len(tests) != expected:
                    raise ModelFormatError(
                        f"Prepare exactly {expected} edge tests for this subtask."
                    )

            def accept(data: dict[str, Any], item: Subtask = subtask) -> None:
                tests = require_test_list(data, minimum=2 if counts is None else counts[1] or 1)
                if counts is not None:
                    tests = tests[: counts[1]]
                else:
                    tests = tests[:5]
                notes = [f"# Edge tests — subtask {item.index}", ""]
                generated_count = counts[0] if counts is not None else 3
                for index, test in enumerate(tests, 1):
                    name = test_filename(item.index, generated_count + index - 1, "in")
                    atomic_write_text(
                        self.config.package_dir / "tests" / "in" / name,
                        test["input"],
                    )
                    notes.extend([f"## {name}", "", test["description"], ""])
                atomic_write_text(
                    self.store.path.parent / "notes" / f"corner_cases_{item.index:02d}.md",
                    "\n".join(notes),
                )

            self._review_json(
                key=key,
                label=f"edge tests for subtask {subtask.index}",
                system=system,
                user=user,
                validator=validate,
                accept=accept,
            )


    def tests(self) -> None:
        heading("Stage 5/8 — tests",
                "Creating deterministic generators and edge cases for every subtask.")
        self.generators()
        self.corner_cases()
