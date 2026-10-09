"""Local Markdown and source-code previews."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from .registry import safe_file
from typing import Any
from urllib.parse import quote
from .storage import atomic_write_text, read_text_exact
from .statement_format import sample_pairs, example_references, normalize_example_blocks


class Preview:
    @staticmethod
    def _slug(value: str) -> str:
        cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._-")
        return cleaned or "draft"


    @staticmethod
    def _code_fence(text: str, language: str = "") -> str:
        longest = max((len(match) for match in re.findall(r"`+", text)), default=0)
        fence = "`" * max(3, longest + 1)
        suffix = language.strip()
        opener = f"{fence}{suffix}" if suffix else fence
        return f"{opener}\n{text}\n{fence}"


    @staticmethod
    def _guess_code_language(name: str, code: str) -> str:
        lower = f"{name}\n{code}".lower()
        if any(token in lower for token in ("import ", "def ", "print(", "python")):
            return "python"
        if any(token in lower for token in ("#include", "std::", "int main", "cout", "scanf", "cin >>")):
            return "cpp"
        return "text"


    def _preview_path(self, stem: str) -> Path:
        slug = self._slug(stem)
        # Previews are not part of the output package. This keeps the output
        # directory limited to files intended for Solve.
        directory = self.store.path.parent / "previews" / slug
        return directory / f"{slug}.md"


    def _write_markdown_preview(self, stem: str, markdown: str) -> Path:
        """Write Markdown only; Solve CLI compiles HTML later."""
        md_path = self._preview_path(stem)
        preview_markdown = (self._statement_examples_preview(markdown, None)
                            if re.search(r'^```[ \t]*\{\.example', markdown, re.M) else markdown)
        # The preview lives outside the package, while images are copied to description/.
        # The final statement keeps short paths, so rewrite them only here.
        for image_name in self.config.image_files:
            escaped_name = re.escape(image_name)
            image_path = self.config.package_dir / 'description' / image_name
            relative_path = quote(Path(os.path.relpath(image_path, md_path.parent)).as_posix())
            preview_markdown = re.sub(
                rf"(!\[[^\]]*\]\()\.?/?{escaped_name}(\))",
                lambda match: match[1] + relative_path + match[2],
                preview_markdown,
            )
        atomic_write_text(md_path, preview_markdown.rstrip() + "\n")
        return md_path


    def _write_code_preview(self, stem: str, filename: str, code: str) -> Path:
        directory = self.store.path.parent / "previews" / self._slug(stem)
        path = safe_file(directory, filename)
        atomic_write_text(path, code.rstrip() + "\n")
        return path


    def _tests_preview(self, tests: list[dict[str, Any]]) -> str:
        sections: list[str] = ["## Tests", ""]
        for index, test in enumerate(tests, 1):
            if not isinstance(test, dict):
                continue
            sections.extend([f"### Test {index}", "", "#### Input", ""])
            sections.append(self._code_fence(str(test.get("input", "")), "text"))
            if "output" in test:
                sections.extend(["", "#### Output", ""])
                sections.append(self._code_fence(str(test.get("output", "")), "text"))
            description = str(test.get("description", "")).strip()
            if description:
                sections.extend(["", "#### Description", "", description])
            sections.append("")
        return "\n".join(sections).strip()


    def _cases_preview(self, cases: list[dict[str, Any]]) -> str:
        sections: list[str] = ["## Verification cases", ""]
        for index, case in enumerate(cases, 1):
            if not isinstance(case, dict):
                continue
            sections.extend([f"### Case {index}", ""])
            for key in ("kind", "score", "client"):
                if key in case:
                    sections.append(f"- {key}: `{case[key]}`")
            description = str(case.get("description", "")).strip()
            if description:
                sections.append(f"- Description: {description}")
            for key in ("input", "reference", "candidate"):
                if key in case:
                    sections.extend(["", f"#### {key.capitalize()}", "", self._code_fence(str(case[key]), "text")])
            sections.append("")
        return "\n".join(sections).strip()


    def _statement_examples_preview(self, markdown: str, tests: Any) -> str:
        """Expand example data for preview purposes only."""
        from .statement_format import EXAMPLES

        if self.config.task_type == 'interactive':
            return markdown.strip() + '\n'
        preserve_samples = self.config.existing_tests or self.state.setup.get('preserve_samples', False)
        if tests is None and not preserve_samples:
            tests = self.config.test_plan.get('tests', [])
        tests_by_name = {
            pair: test
            for pair, test in zip(sample_pairs(self.config, len(tests)), tests)
            if isinstance(test, dict)
        } if isinstance(tests, list) else {}
        samples = [dict(test, input_file=inp, output_file=out)
                   for (inp, out), test in tests_by_name.items()]
        markdown = normalize_example_blocks(markdown, samples)
        if not example_references(markdown) and samples:
            # Even an incomplete draft shows the data returned by the model.
            markdown += '\n\n' + '\n\n'.join(
                f'``` {{.example input_file="{s["input_file"]}" output_file="{s["output_file"]}"}}\n```'
                for s in samples)
        number = 0

        def expand(match: re.Match[str]) -> str:
            nonlocal number
            number += 1
            input_name, output_name, explanation = example_references(match.group(0))[0]
            test = tests_by_name.get((input_name, output_name), {})
            parts = [f"### Example {number}", ""]
            for label, folder, name, field in (
                ("Input", "in", input_name, "input"),
                ("Output", "out", output_name, "output"),
            ):
                value = test.get(field) if not preserve_samples else None
                if value is None and name and Path(name).name == name:
                    try:
                        value = read_text_exact(safe_file(self.config.package_dir / "tests" / folder, name))
                    except (OSError, UnicodeError):
                        pass
                parts.extend([f"**{label}**", ""])
                parts.append(self._code_fence(str(value), "text") if value is not None
                             else f"Preview data for `{name}` is unavailable.")
                parts.append("")
            explanation = explanation or str(test.get("description", "")).strip()
            if explanation:
                parts.extend([explanation, ""])
            return "\n".join(parts)

        return EXAMPLES.sub(expand, markdown).strip() + "\n"


    def _json_preview_markdown(self, data: dict[str, Any]) -> str:
        sections: list[str] = []
        statement_markdown = data.get("statement_markdown")
        if isinstance(statement_markdown, str) and statement_markdown.strip():
            return self._statement_examples_preview(statement_markdown, data.get("tests"))

        code_fields = (
            ("code", "Code"),
            ("public_header", "Public header"),
            ("local_tester", "Local tester"),
            ("brute", "Brute force"),
            ("small_generator", "Small generator"),
            ('input_validator', 'Input and subtask validator'),
            ('reducer', 'Counterexample reducer'),
        )
        for key, label in code_fields:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                sections.extend([f"## {label}", ""])
                sections.append(self._code_fence(value.strip(), self._guess_code_language(key, value)))
        for key, label in (
            ("description", "Description"),
            ("critique", "Critique"),
        ):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                sections.extend([f"## {label}", "", value.strip()])
        self_check = data.get('self_check')
        if isinstance(self_check, dict) and isinstance(self_check.get('summary'), str):
            sections.extend(['## Self-check', '', self_check['summary']])
        clients = data.get("clients")
        if isinstance(clients, dict) and clients:
            sections.extend(["## Test clients", ""])
            for name in sorted(clients):
                value = clients[name]
                if isinstance(value, str) and value.strip():
                    sections.extend([f"### {name}", "", self._code_fence(value.strip(), self._guess_code_language(name, value))])
        tests = data.get("tests")
        if isinstance(tests, list) and tests:
            sections.append(self._tests_preview(tests))
        cases = data.get("cases")
        if isinstance(cases, list) and cases:
            sections.append(self._cases_preview(cases))
        for rule in data.get('rules', []):
            sections.extend([f"## {rule['id']}", rule['rule'], '**Source:** ' + rule['evidence']])
        for mutant in data.get('mutants', []):
            sections.extend([f"## {mutant['name']}", mutant['description'], self._code_fence(mutant['code'], 'cpp')])
        for question in data.get('missing_details', []):
            sections.extend(['## Open questions', question])
        if not sections:
            sections.append(json.dumps(data, ensure_ascii=False, indent=2))
        return "\n\n".join(section for section in sections if section).strip() + "\n"
