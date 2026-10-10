"""Resumable package-building workflow, from import through validation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .console import SavedExit, ask_multiline, choose, heading
from .input_sources import import_existing_materials, statement_attachments
from .models import Subtask, WorkflowState
from .openai_client import OpenAIClient
from .parsing import ModelFormatError, parse_json_object, require_string, require_test_list, require_text
from .prompts import (
    checker_prompt,
    corner_prompt,
    critique_prompt,
    editorial_prompt,
    generator_prompt,
    interactor_prompt,
    solution_prompt,
    statement_prompt,
    test_blueprint_prompt,
    task_type_prompt,
    verification_prompt,
    verification_schema,
)
from .solve4 import (
    create_package_skeleton,
    local_validation,
    solve_validation,
    source_extension,
    statement_validation_errors,
    write_package_metadata,
)
from .storage import StateStore, atomic_write_text, read_text_exact, write_json
from .execution import ExecutionError
from .verification import Verification
from .statement_format import (
    example_references,
    imported_examples,
    example_size_errors,
    EXAMPLE_FILE_MAX_BYTES,
    ensure_example_blocks,
    fill_empty_example_descriptions,
    align_example_references,
    normalize_example_blocks,
    format_subtasks,
    sample_pairs,
)
from .solve_native import native_action, archive_package
from .content_review import ContentReview
from .preview import Preview
from .review import Review
from .test_generation import TestGeneration
from .solutions import Solutions
from .registry import solution_path, load_manifest, safe_file
from .paths import checked_tree
from .storage import validate_state_identity


JsonValidator = Callable[[dict[str, Any]], Any]
AcceptJson = Callable[[dict[str, Any]], None]


class Workflow(ContentReview, Preview, Review, TestGeneration, Solutions):
    """Coordinates workflow stages and saves state after every meaningful change."""

    def __init__(self, state: WorkflowState, store: StateStore, openai: OpenAIClient):
        if state.config is None:
            raise ValueError("The project state has no configuration.")
        validate_state_identity(state, store.codename)
        checked_tree(state.config.package_dir)
        checked_tree(store.path.parent)
        self.state = state
        self.store = store
        self.openai = openai
        self.config = state.config

    def _save(self) -> None:
        self.store.save(self.state)

    def _complete(self, key: str) -> None:
        from .revisions import snapshot
        if key in self.state.drafts:
            snapshot(self.store, key, self.state.drafts[key])
        if key in self.state.drafts:
            self.state.setup.setdefault("accepted", {})[key] = self.state.drafts[key]
        approvals = self.state.setup.setdefault("approvals", [])
        if key not in {"import", "finalize"}:
            approvals.append(key)
        pending = self.state.setup.get('review_pending', [])
        if key in pending:
            pending.remove(key)
        self.state.mark_done(key)
        self.state.feedback.pop(key, None)
        self.state.previous_drafts.pop(key, None)
        self.state.setup.pop("format_retries:" + key, None)
        self._save()


    def _generate(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        attachments: list[Path] | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response",
    ) -> str:
        """Calls the model and saves cost before processing its result."""

        try:
            generate_json = getattr(self.openai, 'generate_json', None)
            if json_schema is not None and callable(generate_json):
                return generate_json(
                    system,
                    user,
                    schema=json_schema,
                    schema_name=schema_name,
                    max_tokens=max_tokens,
                    attachments=attachments,
                )
            return self.openai.generate(system, user, max_tokens=max_tokens, attachments=attachments)
        finally:
            usage_dict = getattr(self.openai, 'usage_dict', None)
            if callable(usage_dict):
                self.state.usage = usage_dict()
                self._save()
                events = getattr(self.openai, 'events', None)
                if isinstance(events, list) and events:
                    event = events.pop()
                    from .revisions import snapshot
                    snapshot(self.store, 'api-calls', json.dumps(event, ensure_ascii=False, indent=2))
                print(f"Project cost so far: ${float(self.state.usage['cost_usd']):.4f}")



















    def detect_task_type(self) -> None:
        if self.config.task_type != "auto":
            return
        print("Detecting the task type automatically…")
        metadata = {}
        if self.config.input_package:
            path = Path(self.config.input_package) / "config.json"
            if path.is_file():
                metadata = load_manifest(Path(self.config.input_package))
        system, user = task_type_prompt(self.config, metadata)
        def validate(data):
            if require_string(data, "task_type") not in {"standard", "multiple", "interactive"}:
                raise ModelFormatError("Unknown task type.")
            require_string(data, "reason")
            missing = data.get("missing_details")
            if not isinstance(missing, list) or any(not isinstance(x, str) or not x.strip() for x in missing):
                raise ModelFormatError("missing_details must be a list of questions.")
            if not isinstance(data.get("judge_notes"), str):
                raise ModelFormatError("judge_notes must be text.")
            if data["task_type"] != "standard" and not data["judge_notes"].strip():
                raise ModelFormatError("Checker rules or the interaction protocol are missing.")
        def accept(data):
            self.state.setup["missing_details"] = data["missing_details"]
            self.state.setup["task_type_reason"] = data["reason"]
            self.config.task_type = data["task_type"]
            self.config.judge_notes = "\n\n".join(
                note for note in (self.config.judge_notes, data["judge_notes"].strip()) if note)
        self._review_json(key="task_type", label="task type detection",
                          system=system, user=user, validator=validate, accept=accept,
                          max_tokens=3000, attachments=statement_attachments(self.config))


    def clarify_judge(self) -> None:
        questions = self.state.setup.get("missing_details", [])
        while questions:
            question = questions[0]
            answer = ask_multiline(
                f"Information needed to prepare judging is missing: {question}",
                initial=self.state.setup.get("clarification_draft", ""),
                on_change=self._save_clarification,
            )
            if not answer.strip():
                print("An answer is required; you can save the project and return later.")
                continue
            self.config.judge_notes += f"\n\n{question}\n{answer}"
            questions.pop(0)
            self.state.setup.pop("clarification_draft", None)
            self._save()

    def _save_clarification(self, text: str) -> None:
        self.state.setup["clarification_draft"] = text
        self._save()

    def import_materials(self) -> None:
        heading("Stage 2/8 — import materials",
                "Copying supplied files and identifying reusable task materials.")
        if self.state.is_done("import"):
            return
        import_existing_materials(self.config)
        self._complete("import")
        if self.config.input_package:
            print("The existing package was copied without removing its original.")
        if self.config.image_files:
            print(f"Copied {len(self.config.image_files)} images to description/.")

    def statement(self, *, automatic: bool = False) -> None:
        heading("Stage 3/8 — Solve 4 Markdown statement",
                "Creating the statement and its public examples from the approved plan.")
        if self.state.is_done('statement'):
            return
        automatic = automatic or self.state.setup.get('statement_auto_repair', {}).get('pending', False)
        preserve_samples = self.config.existing_tests or self.state.setup.get('preserve_samples', False)
        if not preserve_samples and self.config.task_type != "interactive":
            self._prepare_test_plan()
        from dataclasses import replace
        system, user = statement_prompt(replace(self.config, existing_tests=preserve_samples))
        if preserve_samples:
            user += (
                "\n\nThe package already contains tests. Do not replace them. Preserve existing "
                "sample references when they occur in the source statement."
            )
            if self.config.task_type == 'interactive':
                user += '\nRevise only the description and sample conversation. Return tests: []; interactor control inputs remain in existing files.'
        planned_tests = self._planned_tests()
        existing_examples = imported_examples(self.config) if preserve_samples and self.config.task_type != "interactive" else []
        if preserve_samples and self.config.task_type != "interactive" and not existing_examples:
            raise RuntimeError("No usable sample pairs exist in tests/in and tests/out. Add sample files; the statement must not invent their data.")

        def validate(data: dict[str, Any]) -> None:
            markdown = format_subtasks(require_string(data, "statement_markdown"), self.config)
            tests = require_test_list(data, minimum=0 if preserve_samples else (1 if self.config.task_type == "interactive" else 2))
            data["tests"] = tests
            if preserve_samples and tests:
                raise ModelFormatError("For existing files, return tests: []; examples must use only tests/in and tests/out.")
            if self.config.task_type != "interactive" and any("output" not in test for test in tests):
                raise ModelFormatError("Every standard example must include an 'output' field.")
            if not preserve_samples and self.config.task_type != "interactive":
                size_errors = example_size_errors(tests)
                if size_errors:
                    raise ModelFormatError(" ".join(size_errors))
            if planned_tests and not existing_examples and self.config.task_type != "interactive":
                if len(tests) != len(planned_tests):
                    raise ModelFormatError("The statement must use every planned example.")
                for expected, actual in zip(planned_tests, tests):
                    if (
                        expected.get("input") != actual.get("input")
                        or expected.get("output") != actual.get("output")
                    ):
                        raise ModelFormatError("Statement examples do not match the planned tests.")
                descriptions = [
                    str(actual.get("description", "")).strip() or str(expected.get("description", "")).strip()
                    for expected, actual in zip(planned_tests, tests)
                ]
                # Preview and file writes use the exact approved planned data.
                data["tests"] = [dict(expected, description=description)
                                 for expected, description in zip(planned_tests, descriptions)]
            else:
                descriptions = [
                    str(test.get("description", ""))
                    for test in tests
                    if isinstance(test, dict)
                ]
            if self.config.task_type != 'interactive':
                samples = existing_examples or [
                    dict(test, input_file=inp, output_file=out)
                    for (inp, out), test in zip(sample_pairs(self.config, len(tests)), data['tests'])
                ]
                markdown = normalize_example_blocks(markdown, samples)
                markdown = align_example_references(
                    markdown,
                    [(sample['input_file'], sample['output_file']) for sample in samples],
                )
                markdown = ensure_example_blocks(markdown, samples, self.config.language_code)
            markdown = fill_empty_example_descriptions(markdown, descriptions)
            data["statement_markdown"] = markdown
            structure_errors = statement_validation_errors(
                markdown, self.config.task_type, self.config.language_code
            )
            for image_name in self.config.image_files:
                image_pattern = rf"!\[[^\]]*\]\((?:\./)?{re.escape(image_name)}\)"
                if not re.search(image_pattern, markdown):
                    raise ModelFormatError(f"Image {image_name} is missing from the statement.")
            data["statement_markdown"] = markdown
            if structure_errors:
                raise ModelFormatError(" ".join(structure_errors))
            if preserve_samples:
                if self.config.task_type != "interactive":
                    references = example_references(markdown)
                    if [(i, o) for i, o, _ in references] != [(test['input_file'], test['output_file']) for test in existing_examples]:
                        raise ModelFormatError("Examples must reference exactly the selected tests/in and tests/out files in order.")
                    if any(not explanation for _, _, explanation in references):
                        raise ModelFormatError("Explain every example using its tests/in and tests/out files.")
                    if not references:
                        raise ModelFormatError("A valid .example block with imported package files is missing.")
                    for input_name, output_name, _ in references:
                        for folder, name in (("in", input_name), ("out", output_name)):
                            if not name or Path(name).name != name or not (self.config.package_dir / "tests" / folder / name).is_file():
                                raise ModelFormatError(f"Example references missing file tests/{folder}/{name}.")
                            if (self.config.package_dir / "tests" / folder / name).stat().st_size > EXAMPLE_FILE_MAX_BYTES:
                                raise ModelFormatError(f"Example file {name} exceeds 2048 bytes: Solve CLI will not send it for statement compilation.")
                if not isinstance(data.get("tests", []), list):
                    raise ModelFormatError("The 'tests' field must be an array.")
                return
            if self.config.task_type == "interactive" and len(tests) > 3:
                raise ModelFormatError("Provide at most three examples.")
            if self.config.task_type != "interactive":
                references = example_references(markdown)
                if [(i, o) for i, o, _ in references] != sample_pairs(self.config, len(tests)):
                    raise ModelFormatError(".example blocks must reference generated .in/.out files in order, one per test.")
                if not any(description for _, _, description in references):
                    raise ModelFormatError("At least one example must have an explanation in its .example block.")

        def accept(data: dict[str, Any]) -> None:
            statement = require_string(data, "statement_markdown")
            self.config.sample_files = [{'input': i, 'output': o} for i, o, _ in example_references(statement)]
            root = self.config.package_dir
            atomic_write_text(
                root / "description" / f"{self.config.language_code}.md",
                statement + "\n",
            )
            if preserve_samples:
                return
            tests = planned_tests or require_test_list(
                data, minimum=1 if self.config.task_type == "interactive" else 2
            )
            notes = ["# Sample tests", ""]
            for index, test in enumerate(tests):
                name, output_name = sample_pairs(self.config, len(tests))[index]
                atomic_write_text(root / "tests" / "in" / name, test["input"])
                if "output" in test:
                    atomic_write_text(root / "tests" / "out" / output_name, test["output"])
                notes.extend([f"## {name}", "", test["description"], ""])
            atomic_write_text(self.store.path.parent / "notes" / "samples.md", "\n".join(notes))

        self._review_json(
            key="statement",
            label="task statement and samples",
            system=system,
            user=user,
            validator=validate,
            accept=accept,
            max_tokens=8000,
            attachments=statement_attachments(self.config),
            automatic=automatic,
        )







    def verification_materials(self) -> None:
        for path in (self.config.package_dir / 'verification').glob('author_brute_*.*'):
            path.unlink()
        for index, code in enumerate(self.state.setup.get('custom_brutes', []), 1):
            atomic_write_text(self.config.package_dir / 'verification' / f'author_brute_{index:02d}.cpp', code)
        validators = self.config.package_dir / 'validators'
        if self.state.is_done("verification_materials") and any(
            (validators / f'input_validator{suffix}').is_file()
            for suffix in ('.cpp', '.py')
        ) and (self.config.package_dir / 'verification/mutants.json').is_file():
            return
        self.state.completed = [key for key in self.state.completed if key != 'verification_materials']
        system, user = verification_prompt(self.config, self._statement_text())
        directory = self.config.package_dir / "verification"

        def normalize(data: dict[str, Any]) -> None:
            if not isinstance(data.get("description"), str) or not data["description"].strip():
                data["description"] = (
                    "Independent verification covers input validation, small differential "
                    "tests, and deliberately incorrect solutions or test clients."
                )
            mutants = data.get("mutants")
            if isinstance(mutants, list):
                for index, mutant in enumerate(mutants, 1):
                    if isinstance(mutant, dict):
                        # The filename has no semantic value: it is an internal
                        # artifact name.  Assign it locally so a harmless model
                        # typo cannot cause another paid retry.
                        mutant["name"] = f"mutant_{index:02d}.cpp"
            reducer = data.get("reducer")
            if isinstance(reducer, str) and reducer.strip() and not re.search(
                r"\b(?:int|signed)\s+main\s*\(", reducer
            ):
                # A reducer is optional.  A helper function without main() is
                # not executable by the verification runner, so omit it.
                data["reducer"] = None
            cases = data.get("cases")
            if isinstance(cases, list):
                for case in cases:
                    if isinstance(case, dict) and case.get("kind") == "valid":
                        # Verification scores use the Solve 0--100 scale.  A
                        # valid case always means full acceptance, even when a
                        # model writes a boolean-style score such as 1.
                        case["score"] = 100

        def validate(data: dict[str, Any]) -> None:
            require_string(data, "description")
            require_string(data, 'input_validator')
            mutants = data.get('mutants')
            if not isinstance(mutants, list) or (self.config.task_type != 'interactive' and len(mutants) < 2):
                raise ModelFormatError('At least two deliberately incorrect solutions (mutants) are required.')
            names = set()
            for mutant in mutants:
                name = require_string(mutant, 'name')
                if not re.fullmatch(r'[A-Za-z0-9_-]+\.cpp', name) or name in names:
                    raise ModelFormatError('A mutant must have a unique simple .cpp name.')
                names.add(name)
                require_string(mutant, 'code')
                require_string(mutant, 'description')
            interactive = self.config.task_type == "interactive"
            if interactive:
                clients = data.get("clients")
                if not isinstance(clients, dict):
                    raise ModelFormatError("Interaction test programs are missing.")
                for kind in ("wrong", "truncated", "query_limit"):
                    require_string(clients, kind)
                required = {"wrong", "truncated", "query_limit", "silent"}
            else:
                require_string(data, "brute")
                require_string(data, "small_generator")
                required = ({"valid", "wrong", "empty", "truncated", "extra", "malformed"}
                            if self.config.task_type == "multiple" else set())
            cases = data.get("cases")
            if not isinstance(cases, list):
                raise ModelFormatError("cases must be a list.")
            kinds = set()
            for case in cases:
                if not isinstance(case, dict):
                    raise ModelFormatError("A case must be an object.")
                kind = require_string(case, "kind")
                kinds.add(kind)
                require_string(case, "input")
                require_string(case, "description")
                if type(case.get("score")) is not int or not 0 <= case["score"] <= 100:
                    raise ModelFormatError("Invalid expected score.")
                if kind in {"wrong", "query_limit"} and case["score"] == 100:
                    raise ModelFormatError("A wrong answer or query-limit case must be rejected.")
                if interactive:
                    if case.get("client") != kind + ".cpp" or kind not in required:
                        raise ModelFormatError("Invalid client name.")
                else:
                    for field in ("reference", "candidate"):
                        if not isinstance(case.get(field), str):
                            raise ModelFormatError(f"Missing {field} text.")
                    if kind == "valid" and case["score"] != 100:
                        raise ModelFormatError("A valid case must be accepted with score 100.")
                    if kind == "empty" and case["candidate"]:
                        raise ModelFormatError("An empty case must be empty.")
            if not required <= kinds:
                raise ModelFormatError("Missing cases: " + ", ".join(sorted(required - kinds)))

        def accept(data: dict[str, Any]) -> None:
            atomic_write_text(self.config.package_dir / 'validators/input_validator.cpp', data['input_validator'])
            mutants = data.get('mutants', [])
            write_json(directory / 'mutants.json', mutants)
            for mutant in mutants:
                atomic_write_text(safe_file(directory / 'mutants', mutant['name']), mutant['code'])
            if data.get('reducer'):
                atomic_write_text(directory / 'reducer.cpp', require_string(data, 'reducer'))
            if self.config.task_type == "interactive":
                for kind in ("wrong", "truncated", "query_limit"):
                    atomic_write_text(directory / f"{kind}.cpp", data["clients"][kind])
                atomic_write_text(directory / "silent.cpp", "#include <chrono>\n#include <thread>\nint main(){std::this_thread::sleep_for(std::chrono::hours(1));}\n")
            else:
                atomic_write_text(directory / "brute.cpp", data["brute"])
                atomic_write_text(directory / "small_generator.cpp", data["small_generator"])
            write_json(directory / "cases.json", data["cases"])
            atomic_write_text(directory / "assumptions.md", data["description"])

        self._review_json(key="verification_materials", label="independent verification tests",
                          system=system, user=user, validator=validate, accept=accept,
                          normalize=normalize,
                          max_tokens=12000, response_schema=verification_schema(self.config))

    def verify_code(self, report: Verification) -> None:
        self.verification_materials()
        for attempt in range(3):
            try:
                if not self.state.is_done("editorial"):
                    self.editorial()
                write_package_metadata(self.config, usage=self.state.usage)
                report.run()
                return
            except ExecutionError as error:
                report.record(f"Attempt {attempt + 1}", "FAIL", str(error))
                if error.source is None or attempt == 2:
                    raise RuntimeError(f"Verification failed: {error}. Details are in verification/report.md.") from error
                source = error.source
                if source.resolve().parent == (self.config.package_dir / "tests/in").resolve():
                    repaired = self._repair_invalid_test(source, error, attempt)
                    report.input_overrides[source.name] = repaired
                    continue
                print(f"Repairing {source.name} based on its execution result…")
                system = ("Repair the program based on the actual error. Preserve its interface. "
                          "Do not change the task definition or expected test outputs. "
                          "Use only the C++17 standard library; do not use Boost or other third-party dependencies. "
                          'Return JSON with code and description fields.')
                user = (f"Statement:\n{self._statement_text()}\nFile: {source.name}\n"
                        f"Code:\n{source.read_text()}\nError:\n{error}")
                def accept(data: dict[str, Any]) -> None:
                    atomic_write_text(source.with_suffix(source.suffix + f".before-repair-{attempt + 1}"), source.read_text())
                    atomic_write_text(source, require_string(data, "code") + "\n")
                    if source.parent.name == "solutions":
                        self.state.completed = [key for key in self.state.completed if key != "editorial"]
                        self.state.drafts.pop("editorial", None)
                self._review_json(
                    key=f"repair:{source.name}:{len(self.state.completed)}", label=f"repair {source.name}",
                    system=system, user=user,
                    validator=lambda data: require_string(data, "code"), accept=accept, max_tokens=8000,
                    code_preview={"code": source.name},
                )
                if source.parent.name == "solutions":
                    for item in self.config.subtasks:
                        if solution_path(self.config, item).resolve() == source.resolve():
                            critique_key = f"critique:{item.index}"
                            self.state.completed = [k for k in self.state.completed if k != critique_key]
                            self.state.drafts.pop(critique_key, None)
                            self.state.setup.setdefault('accepted', {})[f'solution:{item.index}'] = json.dumps({
                                'code': source.read_text(), 'description': 'Code after the approved execution repair.'})
                    self._save()
                    for item in self.config.subtasks:
                        if solution_path(self.config, item).resolve() == source.resolve():
                            if not self._critique(item, self._statement_text()):
                                from .revisions import RestartWorkflow
                                raise RestartWorkflow

    def _repair_invalid_test(
        self, source: Path, error: ExecutionError, attempt: int
    ) -> str:
        if self.config.existing_tests:
            raise RuntimeError(
                f"The validator rejects imported test {source.name}; author data is not "
                f"changed automatically. Error: {error}"
            ) from error

        planned_tests = self._planned_tests()
        sample_index = next(
            (
                index
                for index, (input_name, _) in enumerate(
                    sample_pairs(self.config, len(planned_tests))
                )
                if input_name == source.name
            ),
            None,
        )
        manifest = load_manifest(self.config.package_dir)
        registered = next(
            (
                test
                for group in manifest.get("test_groups", [])
                for test in group.get("tests", [])
                if test.get("input") == source.name
            ),
            None,
        )
        if registered and registered.get("output") and sample_index is None:
            output = safe_file(self.config.package_dir / "tests/out", registered["output"])
            if output.is_file():
                raise RuntimeError(
                    f"The validator rejects test {source.name}, but the test has approved output "
                        f"{registered['output']}; the example is not changed automatically. Error: {error}"
                ) from error

        subtask_index = -1
        corner_index = -1
        if sample_index is None:
            legacy_match = re.fullmatch(
                re.escape(self.config.codename) + r"\.(\d+)m(\d+)", source.name
            )
            current_match = re.fullmatch(r"(\d+)([a-z])\.in", source.name)
            if legacy_match:
                subtask_index = int(legacy_match.group(1))
                corner_index = int(legacy_match.group(2)) - 1
            elif current_match:
                subtask_index = int(current_match.group(1))
                offset = ord(current_match.group(2)) - ord('a')
                counts = self._planned_counts(subtask_index)
                generated_count = counts[0] if counts is not None else 3
                corner_index = offset - generated_count
        corner_key = f"corners:{subtask_index}" if subtask_index >= 0 else ""
        accepted_raw = self.state.setup.get("accepted", {}).get(corner_key, "")
        accepted_data: dict[str, Any] | None = None
        description = "Preserve the test purpose while changing only the input format."
        if sample_index is not None:
            description = str(planned_tests[sample_index].get("description", "")).strip() or description
        else:
            try:
                accepted_data = parse_json_object(accepted_raw)
                tests = accepted_data.get("tests", [])
                if 0 <= corner_index < len(tests):
                    description = require_string(tests[corner_index], "description")
            except ModelFormatError:
                accepted_data = None

        if sample_index is None and (accepted_data is None or not (0 <= corner_index < len(accepted_data.get("tests", [])))):
            raise RuntimeError(
                f"The validator rejects test {source.name}, but it is not a recognized "
                f"generated edge case; it is not changed automatically. Error: {error}"
            ) from error

        print(f"Repairing invalid test {source.name} based on validator feedback…")
        system = (
            "Repair one generated programming-contest test. Keep it within every task constraint "
            "and preserve the purpose of the case. Return JSON with input and description fields. "
            "The input field must contain the complete repaired test without a Markdown fence."
        )
        user = (
            f"Task statement:\n{self._statement_text()}\n\n"
            f"Subtask: {subtask_index if subtask_index >= 0 else 'unknown'}\n"
            f"Test purpose: {description}\n\n"
            f"Invalid input:\n{read_text_exact(source)}\n\n"
            f"Validator diagnosis:\n{error}"
        )

        def validate(data: dict[str, Any]) -> None:
            require_text(data, "input")
            require_string(data, "description")

        repaired_input = None

        def accept(data: dict[str, Any]) -> None:
            nonlocal repaired_input
            from .revisions import snapshot
            snapshot(self.store, f"test-{source.name}", read_text_exact(source))
            repaired = require_text(data, "input")
            repaired_input = repaired
            repaired_description = require_string(data, "description")
            if sample_index is not None:
                planned_tests[sample_index]["input"] = repaired
                planned_tests[sample_index]["description"] = repaired_description
                self.state.setup.setdefault("accepted", {})["test_plan"] = json.dumps(
                    self.config.test_plan, ensure_ascii=False
                )
            elif accepted_data is not None:
                tests = accepted_data.get("tests", [])
                if 0 <= corner_index < len(tests):
                    tests[corner_index]["input"] = repaired
                    tests[corner_index]["description"] = repaired_description
                    self.state.setup["accepted"][corner_key] = json.dumps(
                        accepted_data, ensure_ascii=False
                    )

        repair_schema = {
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["input", "description"],
            "additionalProperties": False,
        }
        self._review_json(
            key=f"repair:{source.name}:{attempt + 1}",
            label=f"test repair {source.name}",
            system=system,
            user=user,
            validator=validate,
            accept=accept,
            max_tokens=4000,
            automatic=True,
            response_schema=repair_schema,
        )

        if repaired_input is None:
            raise RuntimeError('No test repair was accepted.')
        return repaired_input

    def finalize(self) -> None:
        heading("Stage 8/8 — manifest and validation",
                "Writing the package manifest, checking it locally, then preparing the ZIP.")
        self.state.finished = False
        self._save()
        write_package_metadata(self.config, usage=self.state.usage)
        report = Verification(self.config, regenerate_outputs=True, usage=self.state.usage)
        from .revisions import RestartWorkflow
        revised = False
        try:
            errors = local_validation(self.config)
            report.record("Package structure", "FAIL" if errors else "PASS", "\n".join(errors) or "Required files and sections are present.")
            if errors:
                raise RuntimeError("Local validation failed:\n- " + "\n- ".join(errors))
            self.verify_code(report)
            native_ok, native_output = solve_validation(self.config)
            report.record("Solve CLI", "SKIP" if native_ok is None else "PASS" if native_ok else "FAIL",
                          native_output or "Solve validation completed.")
            if native_ok is False:
                raise RuntimeError(f"Solve CLI rejected the package:\n{native_output}")
            if native_ok:
                print("Solve: building the package and running solutions…")
                native = native_action(self.config)
                report.record("Solve build", "PASS" if native.get("built") else "FAIL",
                              native.get("error", "The original libsolve compiled programs and prepared tests."))
                if native["ok"] is not True:
                    raise RuntimeError("Solve detected an error: " + native.get('error', 'invalid test result') + ". Details: verification/solve-check.json and solve-check.log")
                report.record("Solve tests", "SKIP" if native.get("skipped") else "PASS",
                              native.get("skipped", f"Executed {len(native.get('tests', []))} runs."))
                for solution in native.get("solutions", []):
                    print(f"  {solution['name']}: {solution['score']:g}/100 pkt")
            if self.state.setup.get('review_pending') or self.state.drafts:
                raise RuntimeError('Unapproved stages remain. Return to their review before exporting.')
            write_package_metadata(self.config, usage=self.state.usage)
            archive = archive_package(self.config, expected=report.verified_fingerprints)
            print(f"Ready ZIP: {archive.resolve()}")
            report.record("Final result", "PASS", "Local checks completed; scope and omissions are described above.")
            self.state.finished = True
            from .freshness import certify
            certify(self.config, native=bool(native_ok), expected=report.verified_fingerprints)
            self._complete("finalize")
            label = "Checked locally and with Solve CLI" if native_ok else "Checked locally; Solve CLI validation was not run"
            print(f"\n{label}. Paczka: {self.config.package_dir.resolve()}")
        except RestartWorkflow:
            revised = True
            raise
        except (RuntimeError, OSError, ValueError) as error:
            report.record("Final result", "FAIL", str(error))
            raise
        finally:
            if not revised:
                write_package_metadata(self.config, usage=self.state.usage)
                report.usage = dict(self.state.usage)
                report.save()
                print(f"Report: {(self.config.package_dir / 'verification/report.md').resolve()}")
        print(f"Total API cost: ${float(self.state.usage['cost_usd']):.4f}")

    def _statement_text(self) -> str:
        path = self.config.package_dir / "description" / f"{self.config.language_code}.md"
        return path.read_text(encoding="utf-8")

    def run(self) -> None:
        from .revisions import RestartWorkflow, cleanup
        cleanup(self.state, self.store)
        while True:
            try:
                self._run_stages()
                return
            except RestartWorkflow:
                self.config = self.state.config
                print("Wracam do pierwszego etapu wymagajacego zatwierdzenia.")

    def _run_stages(self) -> None:
        self.detect_task_type()
        self.clarify_judge()
        create_package_skeleton(self.config)
        self.import_materials()
        # Record group names before semantic hashing, even for newly generated tasks.
        for item in self.config.subtasks:
            if not item.group_name:
                item.group_name = f'{item.index:02d}'
        self.specification()
        self._prepare_test_plan()
        self.statement()
        self.audit_statement()
        self.special_judge()
        self.tests()
        self.solutions()
        self.editorial()
        self.verification_materials()
        self.finalize()
