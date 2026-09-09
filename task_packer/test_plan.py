"""Canonical data contract for generated test plans."""

from __future__ import annotations

from typing import Any

from .statement_format import MAX_TESTS_PER_SUBTASK


def response_schema(subtask_count: int) -> dict[str, Any]:
    """Return the strict OpenAI response schema for a test-plan draft.

    The application derives ``total_tests`` after validation, preventing an
    otherwise duplicated value from getting out of sync with the actual plan.
    """

    sample = {
        "type": "object",
        "properties": {
            "input": {"type": "string"},
            "output": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["input", "output", "description"],
        "additionalProperties": False,
    }
    subtask = {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "generator_runs": {"type": "integer", "minimum": 1},
            "corner_tests": {"type": "integer", "minimum": 0},
        },
        "required": ["index", "generator_runs", "corner_tests"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "tests": {"type": "array", "minItems": 2, "maxItems": MAX_TESTS_PER_SUBTASK, "items": sample},
            "subtasks": {"type": "array", "minItems": subtask_count, "maxItems": subtask_count, "items": subtask},
        },
        "required": ["tests", "subtasks"],
        "additionalProperties": False,
    }


def counts_for(plan: dict[str, Any], subtask_index: int) -> tuple[int, int] | None:
    """Read validated counts without coercing malformed persisted data."""

    subtasks = plan.get("subtasks")
    if not isinstance(subtasks, list):
        return None
    for item in subtasks:
        if not isinstance(item, dict) or item.get("index") != subtask_index:
            continue
        generator_runs = item.get("generator_runs")
        corner_tests = item.get("corner_tests")
        if (type(generator_runs) is int and type(corner_tests) is int
                and generator_runs >= 0 and corner_tests >= 0):
            return generator_runs, corner_tests
        return None
    return None


def total_tests(plan: dict[str, Any]) -> int | None:
    """Calculate a plan total only when each stored subtask is well formed."""

    tests = plan.get("tests")
    subtasks = plan.get("subtasks")
    if not isinstance(tests, list) or not isinstance(subtasks, list):
        return None
    total = len(tests)
    for item in subtasks:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            return None
        counts = counts_for({"subtasks": [item]}, item["index"])
        if counts is None:
            return None
        total += sum(counts)
    return total