"""Model-response validation and code extraction from JSON."""

from __future__ import annotations

import json
from typing import Any


class ModelFormatError(ValueError):
    """The model response does not have the agreed format."""


_JSON_STRING_ESCAPES = {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}


def _repair_invalid_json_escapes(text: str) -> str:
    """Double lone backslashes inside JSON strings."""

    repaired: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            repaired.append(char)
            escaped = False
            continue
        if char == "\\":
            next_char = text[index + 1] if index + 1 < len(text) else ""
            if next_char in _JSON_STRING_ESCAPES:
                repaired.append(char)
                escaped = True
            else:
                repaired.append("\\")
                repaired.append("\\")
            continue
        repaired.append(char)
        if char == '"':
            in_string = False

    return "".join(repaired)


def parse_json_object(text: str) -> dict[str, Any]:
    """Read a JSON object while allowing an enclosing Markdown block."""

    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        candidate = "\n".join(lines)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        repaired = _repair_invalid_json_escapes(candidate)
        if repaired != candidate:
            try:
                value = json.loads(repaired)
            except json.JSONDecodeError as repaired_error:
                raise ModelFormatError(f"Invalid JSON: {repaired_error}") from repaired_error
        else:
            raise ModelFormatError(f"Invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ModelFormatError("Expected a JSON object.")
    return value


def require_string(data: dict[str, Any], key: str) -> str:
    if not isinstance(data, dict):
        raise ModelFormatError(f"Expected an object containing field {key!r}.")
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ModelFormatError(f"Field {key!r} must be non-empty text.")
    return value.strip()


def require_test_list(data: dict[str, Any], minimum: int = 1) -> list[dict[str, str]]:
    values = data.get("tests")
    if not isinstance(values, list) or len(values) < minimum:
        raise ModelFormatError(f"Field 'tests' must contain at least {minimum} test(s).")
    result: list[dict[str, str]] = []
    for index, value in enumerate(values, 1):
        if not isinstance(value, dict):
            raise ModelFormatError(f"Test {index} is not an object.")
        item = {
            "input": require_string(value, "input"),
            "description": require_string(value, "description"),
        }
        output = value.get("output")
        if output is not None:
            if not isinstance(output, str):
                raise ModelFormatError(f"Output of test {index} is not text.")
            item["output"] = output.strip()
        result.append(item)
    return result
