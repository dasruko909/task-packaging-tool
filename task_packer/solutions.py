"""Judge, solutions and editorial stages."""
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
from .statement_format import example_size_errors

class Solutions:
    def special_judge(self) -> None:
        heading("Stage 4/8 — judging method",
                "Selecting the standard checker or preparing a custom checker/interactor.")
        if self.config.task_type == "standard":
            print("Standard task: the standard Solve 4 checker remains in use.")
            return
        key = "checker" if self.config.task_type == "multiple" else "interactor"
        if self.state.is_done(key):
            return

        checker_dir = self.config.package_dir / "checker"
        judge = load_manifest(self.config.package_dir).get('checker', {}).get('name')
        existing = [checker_dir / judge] if judge and (checker_dir / judge).is_file() else []
        if self.config.input_package and existing:
            print(f"Keeping checker/interactor from the imported package: {existing[0].name}")
            self._complete(key)
            return

        statement = self._statement_text()
        if self.config.task_type == "multiple":
            system, user = checker_prompt(self.config, statement)

            def validate(data: dict[str, Any]) -> None:
                code = require_string(data, "code")
                require_string(data, "description")
                if "argc" not in code or "return 0" not in code:
                    raise ModelFormatError(
                        "The checker must validate arguments and always exit with code 0."
                    )

            def accept(data: dict[str, Any]) -> None:
                atomic_write_text(checker_dir / "checker.cpp", require_string(data, "code") + "\n")
                atomic_write_text(
                    self.store.path.parent / "notes" / "checker.md",
                    require_string(data, "description") + "\n",
                )
        else:
            system, user = interactor_prompt(self.config, statement)

            def validate(data: dict[str, Any]) -> None:
                for field in ("code", "public_header", "local_tester", "description"):
                    require_string(data, field)
                code = require_string(data, "code")
                if "argc" not in code or "flush" not in code:
                    raise ModelFormatError(
                        "The interactor must validate arguments and explicitly flush responses."
                    )

            def accept(data: dict[str, Any]) -> None:
                atomic_write_text(
                    checker_dir / "interactor.cpp",
                    require_string(data, "code") + "\n",
                )
                atomic_write_text(
                    self.config.package_dir / "public" / "solve_dlazaw.hpp",
                    require_string(data, "public_header") + "\n",
                )
                atomic_write_text(
                    self.config.package_dir / "public" / "interactor_local.cpp",
                    require_string(data, "local_tester") + "\n",
                )
                atomic_write_text(
                    self.store.path.parent / "notes" / "interactor.md",
                    require_string(data, "description") + "\n",
                )

        self._review_json(
            key=key,
            label=(
                "checker"
                if self.config.task_type == "multiple"
                else "interactor and public files"
            ),
            system=system,
            user=user,
            validator=validate,
            accept=accept,
            max_tokens=8000,
            code_preview=(
                {"code": "checker.cpp"}
                if self.config.task_type == "multiple"
                else {
                    "code": "interactor.cpp",
                    "public_header": "solve_dlazaw.hpp",
                    "local_tester": "interactor_local.cpp",
                }
            ),
        )


    def solutions(self) -> None:
        heading("Stage 6/8 — solutions and self-critique",
                "Creating partial and reference solutions, then checking their reasoning.")
        statement = self._statement_text()
        extension = source_extension(self.config.solution_language)
        for subtask in self.config.subtasks:
            while True:
                key = f"solution:{subtask.index}"
                if not self.state.is_done(key):
                    if str(subtask.index) in self.config.solution_files and not self.state.feedback.get(key):
                        print(f'Using existing solution: {solution_path(self.config, subtask)}')
                        self.state.drafts.setdefault(key, json.dumps({
                            "code": solution_path(self.config, subtask).read_text(),
                            "description": "Solution from the imported package."}, ensure_ascii=False))
                        self._save()
                    destination_solution = (
                        self.config.package_dir
                        / "solutions"
                        / f"solution_{subtask.index:02d}.{extension}"
                    )

                    if str(subtask.index) in self.config.solution_files:
                        destination_solution = solution_path(self.config, subtask)
                    system, user = solution_prompt(self.config, subtask, statement)

                    def normalize(data: dict[str, Any]) -> None:
                        # This text is saved only as a local note; it does not
                        # affect the solution source or judging behavior.
                        if not isinstance(data.get("description"), str) or not data["description"].strip():
                            data["description"] = f"Generated solution for subtask {subtask.index}."

                    def validate(data):
                        require_string(data, "code")
                        require_string(data, "description")
                    def accept(data):
                        atomic_write_text(destination_solution, data["code"] + "\n")
                        atomic_write_text(self.store.path.parent / "notes" / f"solution_{subtask.index:02d}.md",
                                          data["description"] + "\n")
                    self._review_json(key=key, label=f"solution for subtask {subtask.index}",
                                      system=system, user=user, validator=validate, accept=accept,
                                      normalize=normalize,
                                      max_tokens=7000, code_preview={"code": destination_solution.name})
                if self._critique(subtask, statement):
                    break

    def _critique(self, subtask, statement) -> bool:
        key = f"critique:{subtask.index}"
        if self.state.is_done(key):
            return True
        system, user = critique_prompt(self.config, subtask, statement,
                                       solution_path(self.config, subtask).read_text())
        verdict: dict[str, Any] = {}

        def validate(data: dict[str, Any]) -> None:
            if type(data.get("correct")) is not bool:
                raise ModelFormatError("Field 'correct' must be a boolean.")
            require_string(data, "summary")
            issues = data.get("blocking_issues")
            if not isinstance(issues, list) or any(
                not isinstance(issue, str) or not issue.strip() for issue in issues
            ):
                raise ModelFormatError("Field 'blocking_issues' must be a list of non-empty strings.")
            if data["correct"] and issues:
                raise ModelFormatError("A correct solution cannot have blocking issues.")
            if not data["correct"] and not issues:
                raise ModelFormatError("An incorrect verdict must identify a specific blocking issue.")

        def accept(data: dict[str, Any]) -> None:
            verdict.update(data)
            lines = [
                f"# Solution self-critique {subtask.index}",
                "",
                "Verdict: " + ("correct" if data["correct"] else "incorrect"),
                "",
                require_string(data, "summary"),
            ]
            if data["blocking_issues"]:
                lines.extend(["", "## Blocking issues", ""])
                lines.extend(f"- {issue.strip()}" for issue in data["blocking_issues"])
            atomic_write_text(
                self.store.path.parent / "notes" / f"critique_{subtask.index:02d}.md",
                "\n".join(lines) + "\n",
            )

        self._review_json(
            key=key,
            label=f"solution self-critique {subtask.index}",
            system=system,
            user=user,
            validator=validate,
            accept=accept,
            max_tokens=3000,
            automatic=True,
            quiet=True,
        )
        # The automatic verdict completes a stage but is not author approval.
        # Therefore `--undo` returns to the last actually approved material: the solution.
        self.state.setup["approvals"] = [
            approved for approved in self.state.setup.get("approvals", [])
            if approved != key
        ]
        self._save()
        if verdict["correct"]:
            print(f"Solution self-critique {subtask.index} found no issue.")
            return True

        issues = "\n".join(f"- {issue.strip()}" for issue in verdict["blocking_issues"])
        feedback = (
            "The self-critique found an issue that makes the solution fail:\n"
            + issues
            + "\nFix the code while keeping the task definition and required subtask scope."
        )
        from .revisions import revise
        revise(self.state, self.store, f"solution:{subtask.index}", feedback)
        print(
            f"The self-critique rejected solution {subtask.index}; "
            f"returning to revise it:\n{issues}"
        )
        return False


    def editorial(self) -> None:
        heading("Stage 7/8 — editorial",
                "Writing an English explanation of the approved reference solution.")
        final = self.config.subtasks[-1]
        extension = source_extension(self.config.solution_language)
        model_path = solution_path(self.config, final)
        system, user = editorial_prompt(
            self.config,
            self._statement_text(),
            model_path.read_text(encoding="utf-8"),
        )
        self._review_text(
            key="editorial",
            label="task editorial",
            system=system,
            user=user,
            destination=self.config.package_dir / "editorial" / "en.md",
        )
