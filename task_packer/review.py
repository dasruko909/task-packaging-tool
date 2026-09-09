"""Persistent artifact review and revision."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any
from collections.abc import Callable
from .console import ask_multiline, choose, SavedExit, project_command
from .parsing import ModelFormatError, parse_json_object
from .storage import atomic_write_text
JsonValidator = Callable[[dict[str, Any]], Any]
AcceptJson = Callable[[dict[str, Any]], None]
NormalizeJson = Callable[[dict[str, Any]], None]
MAX_FORMAT_RETRIES = 2


class Review:
    def _author_system(self, system):
        return system + ("\nApproved author changes, target subtasks, and the approved "
                         "specification override the original statement. Keep rules relative to "
                         "this target definition. Replace samples conflicting with new rules with "
                         "valid ones. Approved specification: "
                         + json.dumps(self.config.specification, ensure_ascii=False))

    def _change_decisions(self):
        from .revisions import edit_menu, RestartWorkflow
        if edit_menu(self.state, self.store):
            raise RestartWorkflow

    def _before_generation(self, key, label, system, user, *, code_preview=None, text=False):
        if self.state.setup.get("request:" + key):
            return True
        path = self.store.path.parent / "requests" / (key.replace(":", "_") + ".txt")
        request = user
        if self.state.feedback.get(key):
            request += "\n\nUser feedback:\n" + self.state.feedback[key]
        if self.state.previous_drafts.get(key):
            request += "\n\nPrevious version:\n" + self.state.previous_drafts[key]
        atomic_write_text(path, system + "\n\n" + request)
        print(f"\n{label}. Prompt available at: {path.resolve()}")
        action = choose("How would you like to prepare this stage?", {
            "g": "generate with AI", "i": "add your own idea or guidance",
            "w": "paste your own version", "f": "load a file",
            "c": "undo or change decisions", "z": "save and exit",
        }, "g")
        if action == "z":
            raise SavedExit
        if action == "c":
            self._change_decisions()
            return False
        if action in {"w", "f"}:
            self._manual_draft(key, action, None, code_preview, text=text)
            return False
        if action == "i":
            self._read_feedback(key, self.state.previous_drafts.get(key, ""),
                                "Describe your algorithm, brute force, or guidance for this stage.")
            return False
        self.state.setup["request:" + key] = True
        self._save()
        return True

    def _manual_draft(self, key, action, raw, code_preview=None, *, text=False):
        from .console import ask
        from .revisions import snapshot
        data = None
        if raw and not text:
            try:
                data = parse_json_object(raw)
            except ModelFormatError:
                pass
        field = None
        if action == "e":
            if data is None:
                print("Load valid JSON first.")
                return
            print("Fields: " + ", ".join(data))
            field = ask("Field to replace (e.g. brute, input_validator, statement_markdown)")
            if field not in data:
                print("No such field.")
                return
        elif not text and code_preview and len(code_preview) == 1 and "code" in code_preview:
            field = "code"
            data = data or {"description": "Code supplied by the author."}
        prompt = ("Paste text" if text else
                  f"Paste field {field} (code without a Markdown fence)" if field else
                  "Paste complete JSON matching the prompt; brute and small_generator fields contain Python")
        slot = "manual:" + key
        try:
            if action == "f":
                value = Path(ask("UTF-8 file path")).expanduser().read_text(encoding="utf-8")
            else:
                def save(value):
                    self.state.setup[slot] = value
                    self._save()
                value = ask_multiline(prompt, initial=self.state.setup.get(slot, ""), on_change=save)
            if not value.strip():
                print("Version cannot be empty.")
                return
            if field == 'code' and action in {'w', 'f'}:
                try:
                    supplied = parse_json_object(value)
                    if 'code' in supplied:
                        field = None
                except ModelFormatError:
                    pass
            if field:
                data[field] = value if isinstance(data.get(field, ""), str) else json.loads(value)
                value = json.dumps(data, ensure_ascii=False, indent=2)
        except (OSError, ValueError) as error:
            print(f"Could not load: {error}")
            return
        if raw:
            snapshot(self.store, key, raw)
        self.state.drafts[key] = value
        self.state.setup.pop(slot, None)
        self.state.setup.pop("request:" + key, None)
        self._save()

    def _read_feedback(self, key: str, raw: str, prompt: str) -> str:
        from .revisions import snapshot
        snapshot(self.store, key, raw)
        self.state.previous_drafts[key] = raw
        self._save()
        def save(text):
            self.state.feedback[key] = text
            self._save()
        feedback = ask_multiline(prompt, initial=self.state.feedback.get(key, ''), on_change=save)
        save(feedback)
        return feedback


    def _review_json(
        self,
        *,
        key: str,
        label: str,
        system: str,
        user: str,
        validator: JsonValidator,
        accept: AcceptJson,
        normalize: NormalizeJson | None = None,
        max_tokens: int = 6000,
        attachments: list[Path] | None = None,
        code_preview: dict[str, str] | None = None,
        automatic: bool = False,
        quiet: bool = False,
        response_schema: dict[str, Any] | None = None,
    ) -> None:
        if self.state.is_done(key):
            return

        system = self._author_system(system)
        retry_key = "format_retries:" + key
        auto_repaired = False
        repairing_format = False
        while True:
            feedback = self.state.feedback.get(key, "")
            raw = self.state.drafts.get(key)
            freshly_generated = False
            if raw is None:
                if (not automatic and not repairing_format
                        and not self._before_generation(
                            key, label, system, user, code_preview=code_preview
                        )):
                    continue
                request = user
                if feedback:
                    request += f"\n\nUser feedback on the previous version:\n{feedback}"
                if key in self.state.previous_drafts:
                    request += '\n\nPrevious version to revise:\n' + self.state.previous_drafts[key]
                if automatic:
                    path = self.store.path.parent / 'requests' / (key.replace(':', '_') + '.txt')
                    atomic_write_text(path, system + '\n\n' + request)
                if not quiet:
                    print(f"\nOpenAI is preparing: {label}…")
                raw = self._generate(
                    system,
                    request,
                    max_tokens=max_tokens,
                    attachments=attachments,
                    json_schema=response_schema,
                    # Responses API schema names may contain only letters,
                    # digits, underscores, and hyphens.  Review keys can
                    # include source filenames such as solution_01.cpp.
                    schema_name=re.sub(r"[^A-Za-z0-9_-]+", "_", key).strip("_") or "response",
                )
                freshly_generated = True
                self.state.drafts[key] = raw
                self.state.setup.pop("request:" + key, None)
                self._save()

            parsed: dict[str, Any] | None = None
            preview_data: dict[str, Any] | None = None
            error_message = ""
            try:
                parsed = parse_json_object(raw)
                preview_data = parsed
                if normalize is not None:
                    before_normalization = json.dumps(
                        parsed, ensure_ascii=False, sort_keys=True
                    )
                    normalize(parsed)
                    after_normalization = json.dumps(
                        parsed, ensure_ascii=False, sort_keys=True
                    )
                    if after_normalization != before_normalization:
                        from .revisions import snapshot
                        snapshot(self.store, key, raw)
                        raw = json.dumps(parsed, ensure_ascii=False, indent=2)
                        self.state.drafts[key] = raw
                        self._save()
                        print("Safe descriptive fields were completed locally.")
                validator(parsed)
            except ModelFormatError as error:
                error_message = str(error)
                parsed = None
                print(f"Response format needs revision: {error_message}")

            repair_was_already_requested = any(
                marker in feedback
                for marker in ("Fix format error:", "Fix response error:")
            )
            retry_count = self.state.setup.get(retry_key, 0)
            if type(retry_count) is not int or retry_count < 0:
                retry_count = 0
            if (error_message and not auto_repaired
                    and retry_count < MAX_FORMAT_RETRIES
                    and (automatic or freshly_generated or repair_was_already_requested)):
                from .revisions import snapshot
                snapshot(self.store, key, raw)
                self.state.previous_drafts[key] = raw
                repair_note = "Fix format error: " + error_message
                if repair_note not in feedback:
                    self.state.feedback[key] = (feedback + "\n" + repair_note).strip()
                self.state.drafts.pop(key, None)
                self.state.setup[retry_key] = retry_count + 1
                # The marker resumes just the repair generation if the API call is
                # interrupted. Without it, a manual stage would ask about AI again.
                self.state.setup["request:" + key] = True
                self._save()
                auto_repaired = True
                repairing_format = True
                print('Revising the model response automatically…')
                continue

            code_paths: list[Path] = []
            if parsed is not None and code_preview:
                for field, filename in code_preview.items():
                    value = parsed.get(field)
                    if isinstance(value, str) and value.strip():
                        code_paths.append(self._write_code_preview(key, filename, value))
                preview_markdown = self._json_preview_markdown({k: v for k, v in parsed.items() if k not in code_preview})
            elif parsed is not None:
                preview_markdown = self._json_preview_markdown(parsed)
            else:
                if preview_data and isinstance(preview_data.get('statement_markdown'), str):
                    # The terminal already reports the validation error.  Do not
                    # inject it into a statement preview, where it looks like
                    # broken task content.
                    preview_markdown = self._statement_examples_preview(
                        preview_data['statement_markdown'], preview_data.get('tests'))
                else:
                    preview_markdown = (
                        f"# {label}\n\n## Format error\n\n{error_message}\n\n"
                        "The draft was not accepted. Revise it or choose ‘revise’ to have "
                        "the generator prepare a new version. The raw response remains saved "
                        "in project state.\n"
                    )
            draft_path = self.store.path.parent / "drafts" / (key.replace(":", "_") + ".json")
            atomic_write_text(draft_path, raw)
            if error_message:
                report_path = draft_path.with_suffix(".format-error.md")
                atomic_write_text(
                    report_path,
                    f"# Format diagnosis — {label}\n\n"
                    f"- **Stage:** `{key}`\n"
                    f"- **Problem:** {error_message}\n"
                    f"- **Draft:** `{draft_path.name}`\n\n"
                    "The raw draft was preserved unchanged. Correct it against the stage "
                    "contract and load it with `f`, or select `p` to request a focused AI revision.\n",
                )
            else:
                draft_path.with_suffix(".format-error.md").unlink(missing_ok=True)
            show_preview = not quiet or parsed is None
            if show_preview:
                print(f"Full draft for editing: {draft_path.resolve()}")
                if error_message:
                    print(f"Format diagnosis: {report_path.resolve()}")
                print(f"\n----- {label} -----")
                for path in code_paths:
                    print(f"Code: {path.resolve()}")
            if preview_markdown or not code_paths:
                md_path = self._write_markdown_preview(key, preview_markdown)
                if show_preview:
                    print(f"Markdown: {md_path.resolve()}")
            if show_preview:
                print("----- end -----\n")
            if automatic and parsed is not None:
                accept(parsed)
                self._complete(key)
                if not quiet:
                    print(f"Ready: {label}.")
                if key == 'specification':
                    print('To change decisions, run: ' + project_command(
                        self.config.codename, '--revise', key, '--feedback', 'Describe the change'))
                return
            if parsed is not None:
                options = {
                    "a": "accept and continue", "p": "revise with AI",
                    "w": "paste your own version", "f": "load a file",
                    "e": "edit a field", "c": "undo or change decisions",
                    "z": "save and exit",
                }
                default_action = "a"
            else:
                options = {
                    "w": "paste your own version", "f": "load a file",
                    "c": "undo or change decisions", "z": "save and exit",
                }
                if retry_count < MAX_FORMAT_RETRIES:
                    options = {"p": "retry with AI", **options}
                    default_action = "p"
                else:
                    default_action = "z"
            if parsed is None:
                if retry_count < MAX_FORMAT_RETRIES:
                    print(
                        "The draft has a format error. Press Enter to retry automatically "
                        f"with the error details ({retry_count + 1}/{MAX_FORMAT_RETRIES})."
                    )
                else:
                    print(
                        "The draft still has a format error after two automatic retries. "
                        "Paste a corrected version, load one from a file, or save and exit."
                    )
            action = choose("What would you like to do with this draft?", options, default_action)
            if action == "z":
                raise SavedExit
            if action == "c":
                self._change_decisions()
                continue
            if action in {"w", "f", "e"}:
                self._manual_draft(key, action, raw, code_preview)
                continue
            if action == "a" and parsed is not None:
                accept(parsed)
                self._complete(key)
                return

            if action == "p" and parsed is None:
                from .revisions import snapshot
                snapshot(self.store, key, raw)
                self.state.previous_drafts[key] = raw
                repair_note = "Fix format error: " + error_message
                if repair_note not in feedback:
                    self.state.feedback[key] = (feedback + "\n" + repair_note).strip()
                self.state.drafts.pop(key, None)
                self.state.setup["request:" + key] = True
                self.state.setup[retry_key] = retry_count + 1
                self._save()
                repairing_format = True
                print("Retrying with the required format…")
                continue

            feedback = self._read_feedback(key, raw, "Describe what needs to be revised.")
            if error_message:
                feedback = f"Fix format error: {error_message}\n{feedback}".strip()
            self.state.feedback[key] = feedback
            self.state.drafts.pop(key, None)
            self.state.setup["request:" + key] = True
            self._save()


    def _review_text(
        self, *, key: str, label: str, system: str, user: str, destination: Path
    ) -> None:
        if self.state.is_done(key):
            return
        system = self._author_system(system)
        feedback = self.state.feedback.get(key, '')
        while True:
            feedback = self.state.feedback.get(key, "")
            raw = self.state.drafts.get(key)
            if raw is None:
                if not self._before_generation(key, label, system, user, text=True):
                    continue
                request = user + (f"\n\nUser feedback:\n{feedback}" if feedback else "")
                if key in self.state.previous_drafts:
                    request += '\n\nPrevious version:\n' + self.state.previous_drafts[key]
                print(f"\nOpenAI is preparing: {label}…")
                raw = self._generate(system, request, max_tokens=6000)
                self.state.drafts[key] = raw
                self.state.setup.pop("request:" + key, None)
                self._save()
            md_path = self._write_markdown_preview(key, raw.strip() or f"# {label}\n")
            print(f"\n----- {label} -----")
            print(f"Markdown: {md_path.resolve()}")
            print("----- end -----\n")
            action = choose(
                "What would you like to do with this draft?",
                {"a": "accept", "p": "revise with AI", "w": "paste your own text",
                 "f": "load a file", "c": "undo or change decisions", "z": "save and exit"},
                "a",
            )
            if action == "z":
                raise SavedExit
            if action == "c":
                self._change_decisions()
                continue
            if action in {"w", "f"}:
                self._manual_draft(key, action, raw, text=True)
                continue
            if action == "a":
                atomic_write_text(destination, raw.strip() + "\n")
                self._complete(key)
                return
            feedback = self._read_feedback(key, raw, "Describe what needs to be revised.")
            self.state.drafts.pop(key, None)
            self.state.setup["request:" + key] = True
            self._save()
