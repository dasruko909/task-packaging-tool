"""Solve Markdown examples and a deterministic grid table for subtasks."""
from __future__ import annotations

import re
import json
from pathlib import Path
from .models import ProjectConfig


EXAMPLES = re.compile(r'^```[ \t]*\{\.example\s+([^}\n]+)\}[ \t]*\n(.*?)^```[ \t]*$', re.M | re.S)

# libsolve 1.0.11: DESCRIPTION_COMPILATION_MAX_FILE_SIZE.
EXAMPLE_FILE_MAX_BYTES = 2048
MAX_TESTS_PER_SUBTASK = 26


def example_size_errors(tests: list[dict[str, str]]) -> list[str]:
    return [
        f"Example {index}, {field}: the file with its final newline exceeds "
        f"{EXAMPLE_FILE_MAX_BYTES} bytes and will not be sent by Solve CLI for statement compilation."
        for index, test in enumerate(tests, 1)
        for field in ("input", "output")
        if len((test.get(field, "") + "\n").encode("utf-8")) > EXAMPLE_FILE_MAX_BYTES
    ]


def imported_examples(config: ProjectConfig) -> list[dict[str, str]]:
    """Read examples from the source or manifest; never create their data."""
    root = config.package_dir
    pairs = [(test['input'], test['output']) for test in config.sample_files]
    if not pairs:
        pairs = [(i, o) for i, o, _ in example_references(config.original_statement)]
    from .registry import load_manifest, records, safe_file
    manifest = load_manifest(root)
    registry = records(manifest)
    if not pairs:
        pairs = [(test.input, test.output) for test in registry if test.sample]
    designated = bool(pairs)
    if not pairs:
        pairs = [(test.input, test.output) for test in registry]
        if not pairs:
            pairs = [(p.name, p.name.replace(".in", ".out"))
                     for p in sorted((root / "tests/in").glob("*")) if p.is_file()]
    result = []
    for input_name, output_name in pairs:
        paths = [safe_file(root / "tests" / folder, name) for folder, name in (("in", input_name), ("out", output_name))]
        if any(not path.is_file() or path.stat().st_size > EXAMPLE_FILE_MAX_BYTES for path in paths):
            if designated:
                raise ValueError(f"Example {input_name}/{output_name}: a file is missing or exceeds 2048 bytes.")
            continue
        result.append(dict(input_file=input_name, output_file=output_name,
                           input=paths[0].read_text(encoding="utf-8"), output=paths[1].read_text(encoding="utf-8")))
        if not designated and len(result) == 3:
            break
    return result


def test_filename(subtask_index: int, test_index: int, extension: str) -> str:
    """Solve name: subtask number (0 = examples), letter a--z, and extension."""

    if subtask_index < 0:
        raise ValueError("subtask number must be non-negative")
    if not 0 <= test_index < MAX_TESTS_PER_SUBTASK:
        raise ValueError(
            f"A subtask can have at most {MAX_TESTS_PER_SUBTASK} tests."
        )
    if extension not in {"in", "out"}:
        raise ValueError("test extension must be in or out")
    return f"{subtask_index}{chr(ord('a') + test_index)}.{extension}"


def sample_pairs(config: ProjectConfig, count: int = 3) -> list[tuple[str, str]]:
    if count > MAX_TESTS_PER_SUBTASK:
        raise ValueError(f"At most {MAX_TESTS_PER_SUBTASK} examples can be created.")
    return [
        (test_filename(0, i, "in"), test_filename(0, i, "out"))
        for i in range(count)
    ]


def example_references(markdown: str) -> list[tuple[str, str, str]]:
    result = []
    for match in EXAMPLES.finditer(markdown):
        attributes = dict(re.findall(r'(input_file|output_file)="([^"\n]+)"', match[1]))
        result.append((attributes.get("input_file", ""), attributes.get("output_file", ""), match[2].strip()))
    return result


def normalize_example_blocks(markdown: str, samples: list[dict[str, str]]) -> str:
    """Fix a missing brace and remove only exact duplicate file-data blocks.

    It does not change example names or order; the statement validator checks those.
    """
    by_pair = {(s['input_file'], s['output_file']): s for s in samples}
    blocks = re.compile(
        r'^```[ \t]*\{\.example((?:[ \t]+(?:input_file|output_file)="[^"\n]+"){2})'
        r'[ \t]*\}?[ \t]*\n(.*?)^```[ \t]*$', re.M | re.S)

    def consume(text: str, value: str) -> str | None:
        if not isinstance(value, str):
            return None
        text, value = text.lstrip('\n'), value.strip('\n')
        if value and (text == value or text.startswith(value + '\n')):
            return text[len(value):].lstrip('\n')
        return None

    def normalize(match):
        attrs = dict(re.findall(r'(input_file|output_file)="([^"\n]+)"', match[1]))
        sample = by_pair.get((attrs.get('input_file'), attrs.get('output_file')))
        body = match[2]
        if sample:
            rest = consume(body, sample.get('input', ''))
            if rest is not None:
                rest = consume(rest, sample.get('output', ''))
                if rest is not None:
                    body = rest
        return '``` {.example' + match[1] + '}\n' + body.rstrip('\n') + '\n```'

    return blocks.sub(normalize, markdown)


def align_example_references(
    markdown: str, pairs: list[tuple[str, str]]
) -> str:
    """Align example file references when the statement has exactly the planned blocks.

    This changes only the two file-name attributes and preserves every explanation.
    A missing or extra block remains visible to the statement validator instead of
    being silently fabricated or discarded.
    """

    matches = list(EXAMPLES.finditer(markdown))
    if len(matches) != len(pairs):
        return markdown
    index = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal index
        input_name, output_name = pairs[index]
        index += 1
        block = re.sub(r'input_file="[^"\n]+"', f'input_file="{input_name}"', match.group(0), count=1)
        return re.sub(r'output_file="[^"\n]+"', f'output_file="{output_name}"', block, count=1)

    return EXAMPLES.sub(replace, markdown)


def ensure_example_blocks(
    markdown: str, samples: list[dict[str, str]], language_code: str,
) -> str:
    """Guarantee one correctly named .example block for every planned sample.

    The language model is responsible for prose, but test-file references are
    fixed by the generated plan.  A missing, duplicate, or malformed set is
    repaired locally rather than triggering another paid model retry.
    """
    expected = [(sample["input_file"], sample["output_file"]) for sample in samples]
    actual = [(input_name, output_name) for input_name, output_name, _ in example_references(markdown)]
    if actual == expected:
        return markdown

    heading = "Example" if language_code.lower() == "en" else "Przykład"
    section = re.compile(
        rf"(^## {re.escape(heading)}[ \t]*\n)(.*?)(?=^## |\Z)", re.M | re.S
    )
    if not section.search(markdown):
        # A missing required section is a substantive statement error and must
        # remain visible for review instead of being invented silently.
        return markdown

    fallback = "Example" if language_code.lower() == "en" else "Przykład"
    blocks = "\n\n".join(
        "``` {{.example input_file=\"{input_file}\" output_file=\"{output_file}\"}}\n{description}\n```".format(
            input_file=sample["input_file"],
            output_file=sample["output_file"],
            description=str(sample.get("description", "")).strip() or f"{fallback} {index}.",
        )
        for index, sample in enumerate(samples, 1)
    )
    without_examples = EXAMPLES.sub("", markdown)

    def rebuild(match: re.Match[str]) -> str:
        # Wrong block counts commonly arrive with a second hand-written
        # Input/Output rendering.  Replacing this mechanical section atomically
        # prevents duplicate, contradictory examples in the preview.
        return match[1] + blocks + "\n"

    return section.sub(rebuild, without_examples, count=1)


def fill_empty_example_descriptions(markdown: str, descriptions: list[str]) -> str:
    """Fill empty .example blocks with descriptions from matching tests."""

    index = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal index
        body = match[2].strip()
        description = descriptions[index].strip() if index < len(descriptions) else ""
        index += 1
        if body or not description:
            return match.group(0)
        opening_end = match.group(0).find("\n") + 1
        opening = match.group(0)[:opening_end]
        return f"{opening}{description}\n```"

    return EXAMPLES.sub(replace, markdown)


def subtask_table(config: ProjectConfig) -> str:
    english = config.language_code.lower() == "en"
    rows = [["**Subtask**", "**Additional conditions**", "**Points**"] if english else
            ["**Podzadanie**", "**Dodatkowe warunki**", "**Punkty**"]]
    for item in config.subtasks:
        rows.append([str(item.index), " ".join(item.constraints.split()) or ("none" if english else "brak"), str(item.points)])
    widths = [max(len(row[i]) for row in rows) + 2 for i in range(3)]
    border = "+" + "+".join("-" * width for width in widths) + "+"
    header = "+" + "+".join(":" + "=" * (width - 2) + ":" for width in widths) + "+"
    result = [border]
    for index, row in enumerate(rows):
        result.append("|" + "|".join(" " + cell.ljust(width - 2) + " " for cell, width in zip(row, widths)) + "|")
        result.append(header if index == 0 else border)
    return "\n".join(result)


def format_subtasks(markdown: str, config: ProjectConfig) -> str:
    def replace(match):
        body = match[2]
        # Preserve partial-credit rules surrounding the table.
        table = re.compile(r'^(?:\+[-=+:]+\+[ \t]*\n)(?:(?:\|.*\||\+[-=+:]+\+)[ \t]*(?:\n|$))+', re.M)
        if table.search(body):
            body = table.sub(lambda _: subtask_table(config) + '\n', body, count=1)
        else:
            pipe_table = re.compile(r'^\|[^\n]+\|[ \t]*\n\|[ :|\-]+\|[ \t]*\n(?:\|[^\n]+\|[ \t]*(?:\n|$))*', re.M)
            if pipe_table.search(body):
                body = pipe_table.sub(lambda _: subtask_table(config) + '\n', body, count=1)
            else:
                body = subtask_table(config) + '\n\n' + body.strip()
        return match[1] + '\n' + body.strip() + '\n\n'
    headings = r"(?:Subtasks|Podzadania)"
    return re.sub(rf'(^## {headings}[ \t]*\n)(.*?)(?=^## |\Z)', replace, markdown, flags=re.M | re.S)
