"""Resumable intake form for new and existing packages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .console import (
    ask,
    ask_int,
    ask_multiline,
    ask_yes_no,
    choose,
    heading,
    print_subtask_summary,
)
from .input_sources import (
    find_package_statement,
    images_drop_dir,
    input_dir,
    list_images,
    package_defaults,
    package_drop_dir,
    package_has_tests,
    pdf_drop_path,
    prepare_drop_zones,
    validate_package_drop,
    validate_pdf,
)
from .paths import project_code, language_code as normalize_language
from .storage import ensure_new_project, validate_state_identity
from .models import ProjectConfig, Subtask, WorkflowState
from .parsing import ModelFormatError, parse_json_object, require_string
from .storage import StateStore


def _detected_subtasks(data: dict[str, Any]) -> list[Subtask]:
    """Validate and normalize model-detected subtasks."""

    explicit = data.get("has_explicit_subtasks")
    if not isinstance(explicit, bool):
        raise ModelFormatError("has_explicit_subtasks must be a boolean.")
    require_string(data, "reason")
    raw = data.get("subtasks")
    if not isinstance(raw, list):
        raise ModelFormatError("The model must return a subtask list.")

    if not explicit:
        return [Subtask(1, "Full", 100, "No additional constraints")]
    if not raw:
        raise ModelFormatError("Explicit subtasks must contain at least one subtask.")

    result: list[Subtask] = []
    for expected_index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ModelFormatError(f"Subtask {expected_index} is not an object.")
        if item.get("index") != expected_index:
            raise ModelFormatError("Subtask indices must be consecutive numbers starting at 1.")
        points = item.get("points")
        if not isinstance(points, int) or isinstance(points, bool) or points < 0:
            raise ModelFormatError(f"Subtask {expected_index} has invalid points.")
        name = require_string(item, "name")
        if len(name.split()) > 10:
            raise ModelFormatError(
                f"Subtask {expected_index} name may contain at most 10 words."
            )
        result.append(
            Subtask(
                expected_index,
                name,
                points,
                require_string(item, "constraints"),
            )
        )
    if sum(item.points for item in result) != 100:
        raise ModelFormatError("Detected subtask points do not total 100.")
    return result


def _subtask_detection_schema() -> dict[str, Any]:
    """Return the exact response contract used by subtask detection."""

    subtask = {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "name": {"type": "string"},
            "points": {"type": "integer"},
            "constraints": {"type": "string"},
        },
        "required": ["index", "name", "points", "constraints"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "has_explicit_subtasks": {"type": "boolean"},
            "subtasks": {"type": "array", "items": subtask},
            "reason": {"type": "string"},
        },
        "required": ["has_explicit_subtasks", "subtasks", "reason"],
        "additionalProperties": False,
    }


def _detect_subtasks_with_ai(
    state: WorkflowState,
    store: StateStore,
    original_statement: str,
    attachments: list[Path],
    imported_groups: list[dict],
) -> list[Subtask]:
    """Calls the model during onboarding and persists its cost."""

    from .openai_client import OpenAIClient
    from .prompts import subtask_detection_prompt

    client = OpenAIClient(starting_usage=state.usage)
    system, user = subtask_detection_prompt(original_statement, imported_groups)
    print("AI is analyzing the statement and detecting subtasks…")
    try:
        response = client.generate_json(
            system,
            user,
            schema=_subtask_detection_schema(),
            schema_name="subtask_detection",
            max_tokens=3000,
            attachments=attachments,
        )
        return _detected_subtasks(parse_json_object(response))
    finally:
        state.usage = client.usage_dict()
        store.save(state)
        if client.events:
            from .revisions import snapshot
            snapshot(store, 'api-calls', json.dumps(client.events.pop(), ensure_ascii=False, indent=2))


def _valid_codename(value: str) -> bool:
    try:
        project_code(value)
        return True
    except ValueError:
        return False


def _config_default(data: dict[str, Any], key: str, fallback: Any) -> Any:
    value = data.get(key, fallback)
    return value if value is not None else fallback


def gather_config(
    state: WorkflowState | None = None,
    store: StateStore | None = None,
) -> tuple[ProjectConfig, StateStore]:
    """Collects configuration while saving every field and line immediately."""

    heading(
        "Stage 1/8 — project setup",
        "Nothing will be lost if you close the program.",
    )
    state = state or WorkflowState()
    setup = state.setup

    if store is not None:
        try:
            validate_state_identity(state, store.codename)
        except (ValueError, TypeError) as error:
            raise ValueError(f"{store.path}: {error}") from error
        if "codename" not in setup:
            setup["codename"] = store.codename
    if "language_code" in setup:
        try:
            setup["language_code"] = normalize_language(setup["language_code"])
        except ValueError as error:
            raise ValueError(f"{store.path if store else 'setup'}: {error}") from error

    if "codename" not in setup:
        while True:
            codename = ask(
                "Short task code (lowercase letters, digits, - or _)", "task"
            )
            if _valid_codename(codename):
                break
            print("The code must contain at most 20 characters and no spaces.")
        store = StateStore(codename)
        ensure_new_project(store)
        store.acquire_lock()
        ensure_new_project(store)
        setup["codename"] = codename
        store.save(state)
    else:
        codename = project_code(setup["codename"])
        store = store or StateStore(codename)
        store.acquire_lock()
        print(f"Resuming saved configuration for project {codename!r}.")

    prepare_drop_zones(codename)

    def save_value(key: str, value: Any) -> Any:
        setup[key] = value
        store.save(state)
        return value

    def saved_or_ask(key: str, prompt: str, default: str = "") -> str:
        if key in setup:
            return str(setup[key])
        return str(save_value(key, ask(prompt, default)))

    def saved_or_ask_int(
        key: str, prompt: str, *, default: int | None = None, minimum: int | None = None
    ) -> int:
        if key in setup:
            return int(setup[key])
        return int(save_value(key, ask_int(prompt, default=default, minimum=minimum)))

    heading("Existing materials")
    if "has_package" not in setup:
        save_value(
            "has_package",
            ask_yes_no("Do you already have a Solve 4 package or prepared tests?"),
        )
    has_package = bool(setup["has_package"])
    if has_package and "package_ready" not in setup:
        print(
            f"\n1. Open: {package_drop_dir(codename).resolve()}\n"
            "2. Copy the PACKAGE CONTENTS there. Do not add an extra parent directory.\n"
            "3. For tests only, use exactly: tests/in and tests/out.\n"
            f"4. Full instructions: {(input_dir(codename) / 'INSTRUCTIONS.txt').resolve()}\n"
        )
        ask("Press Enter when the files are in place")
        validate_package_drop(codename)
        save_value("package_ready", True)
        save_value("input_package", str(package_drop_dir(codename)))
        save_value("existing_tests", package_has_tests(codename))

    imported = package_defaults(codename) if has_package else {}
    if 'language_code' not in setup:
        while True:
            try:
                selected_language = normalize_language(ask("Statement language code", "pl"))
            except ValueError as error:
                print(error)
                continue
            save_value('language_code', selected_language)
            break
    language_code = normalize_language(setup['language_code'])

    # Preserve a manual selection from older in-progress projects.
    task_type = str(setup.get("task_type", "auto"))
    if task_type == "auto":
        print("The task type will be detected automatically from the supplied materials.")

    heading("Task statement")
    package_statement = (
        find_package_statement(codename, language_code) if has_package else None
    )
    if "statement_source" not in setup:
        if "original_statement" in setup:  # migracja rozpoczetego projektu v2
            save_value("statement_source", "paste")
        else:
            options = {"w": "paste text in the terminal", "p": "I only have a PDF"}
            if package_statement is not None:
                options["m"] = "use Markdown from the imported package"
            selected = choose(
                "Where should the statement come from?", options, "m" if package_statement else "w"
            )
            save_value(
                "statement_source",
                {"w": "paste", "p": "pdf", "m": "package"}[selected],
            )
    statement_source = str(setup["statement_source"])

    statement_pdf = ""
    if statement_source == "paste":
        if "original_statement" in setup:
            original_statement = str(setup["original_statement"])
        else:
            original_statement = ask_multiline(
                "Paste the original task statement.",
                initial=str(setup.get("original_statement_draft", "")),
                on_change=lambda text: save_value("original_statement_draft", text),
            )
            setup.pop("original_statement_draft", None)
            save_value("original_statement", original_statement)
    elif statement_source == "pdf":
        pdf_path = pdf_drop_path(codename)
        if "pdf_ready" not in setup:
            print(
                f"\n1. Name the file exactly: statement.pdf\n"
                f"2. Copy it here: {pdf_path.resolve()}\n"
                "3. The model reads text and page layout. Export any images needed in "
                "Markdown as separate files too.\n"
            )
            ask("Press Enter when the PDF is in place")
            validate_pdf(pdf_path)
            save_value("pdf_ready", True)
        statement_pdf = str(pdf_path)
        original_statement = "The source statement is in the attached PDF."
        save_value("original_statement", original_statement)
    else:
        package_statement = find_package_statement(codename, language_code)
        if package_statement is None:
            raise RuntimeError("The package does not contain description/*.md.")
        original_statement = package_statement.read_text(encoding="utf-8")
        save_value("original_statement", original_statement)

    statement_idea = saved_or_ask(
        "statement_idea",
        "Brief editorial idea (optional, one line)",
        str(_config_default(setup, "statement_idea", "")),
    )

    if "has_images" not in setup:
        save_value(
            "has_images",
            ask_yes_no(
                "Should the final Markdown include images? "
                "This also includes images visible only in the PDF."
            ),
        )
    if bool(setup["has_images"]) and "images_ready" not in setup:
        print(
            f"\n1. Copy PNG/JPG/JPEG/WEBP files here: {images_drop_dir(codename).resolve()}\n"
            "2. If they exist only in the PDF, export them as separate files first.\n"
            "3. Do not rename files referenced by the original statement.\n"
            "4. Return to the terminal after copying them.\n"
        )
        ask("Press Enter when the images are in place")
        images = list_images(codename)
        if not images:
            raise RuntimeError(f"No images found in {images_drop_dir(codename)}.")
        save_value("image_files", [path.name for path in images])
        save_value("images_ready", True)
    image_files = [str(name) for name in setup.get("image_files", [])]
    if bool(setup.get("has_images")) and "image_placements" not in setup:
        placements: dict[str, str] = {}
        print(
            "\nNow specify where each image should appear in the statement. "
            "Enter a short location description, for example, \"after the introduction, before Input\"."
        )
        for image_name in image_files:
            placements[image_name] = ask(
                f"Where should image {image_name} appear in the statement?",
                "determine from the statement and sample data",
            )
        save_value("image_placements", placements)

    heading("Metadata and limits")
    title_default = codename.capitalize()
    titles = imported.get("title")
    if isinstance(titles, dict):
        title_default = str(titles.get(language_code, title_default))
    title = saved_or_ask("title", "Task title", title_default)
    origin = saved_or_ask(
        "origin",
        "Exact source (contest, year, round, original title)",
        str(_config_default(imported, "origin", "")),
    )
    generator_language = str(setup.setdefault("generator_language", "cpp"))
    solution_language = str(setup.setdefault("solution_language", "cpp"))
    store.save(state)
    supported = {'cpp', 'c++', 'cpp17', 'cpp20'}
    if generator_language.lower() not in supported or solution_language.lower() not in supported:
        raise RuntimeError('Task artifacts must be written in C++. Choose cpp for generators and solutions.')
    limits = imported.get("limits") if isinstance(imported.get("limits"), dict) else {}
    time_limit_ms = saved_or_ask_int(
        "time_limit_ms",
        "Time limit in milliseconds",
        default=int(limits.get("time", 2000)),
        minimum=500,
    )
    memory_limit_mb = saved_or_ask_int(
        "memory_limit_mb",
        "Memory limit in MB",
        default=max(1, int(limits.get("memory", 262_144)) // 1024),
        minimum=1,
    )

    judge_notes = ""
    if task_type in {"multiple", "interactive"}:
        label = "checker" if task_type == "multiple" else "interactor and protocol"
        key = "judge_notes"
        if key in setup:
            judge_notes = str(setup[key])
        else:
            judge_notes = ask_multiline(
                f"Describe exactly how the {label} must work. "
                "This is a safety-critical specification.",
                initial=str(setup.get("judge_notes_draft", "")),
                on_change=lambda text: save_value("judge_notes_draft", text),
            )
            setup.pop("judge_notes_draft", None)
            save_value(key, judge_notes)

    heading("Subtasks")
    imported_groups = [g for g in imported.get('test_groups', []) if not g.get('is_sample', g.get('sample', False))]
    if "subtask_mode" not in setup:
        selected = choose(
            "How would you like to define subtasks?",
            {
                "w": "I will define my own subtasks",
                "a": "AI will detect subtasks from the statement",
            },
            "a",
        )
        save_value("subtask_mode", "manual" if selected == "w" else "ai")

    if setup["subtask_mode"] == "ai":
        if "subtasks" not in setup:
            attachments = [Path(statement_pdf)] if statement_pdf else []
            detected = _detect_subtasks_with_ai(
                state, store, original_statement, attachments, imported_groups
            )
            save_value(
                "subtasks",
                [
                    {
                        "name": item.name,
                        "points": item.points,
                        "constraints": item.constraints,
                    }
                    for item in detected
                ],
            )
            save_value("subtask_count", len(detected))
        count = int(setup["subtask_count"])
    else:
        heading("Custom subtasks", "The last one automatically receives the remaining points.")
        count = saved_or_ask_int("subtask_count", "How many subtasks are there?", default=len(imported_groups) or 1, minimum=1)
    if count < 1:
        raise RuntimeError('There must be at least one subtask.')
    if imported_groups and count != len(imported_groups):
        raise RuntimeError('The number of subtasks must match the scored groups in the imported package.')
    partial_subtasks = setup.setdefault("subtasks", [])
    if not isinstance(partial_subtasks, list):
        raise RuntimeError("The saved subtask list has an invalid format.")

    points_left = 100
    for index in range(1, count + 1):
        while len(partial_subtasks) < index:
            partial_subtasks.append({})
            store.save(state)
        item = partial_subtasks[index - 1]
        if not isinstance(item, dict):
            raise RuntimeError(f"Saved subtask {index} has an invalid format.")

        if setup["subtask_mode"] == "manual":
            heading(
                f"Subtask {index} of {count}",
                f"{points_left} points remain to allocate.",
            )
        if "name" not in item:
            item["name"] = ask(
                "Short name",
                "Full" if count == 1 else f"Subtask {index}",
            )
            store.save(state)
        if "constraints" not in item:
            def save_constraints(text: str, target: dict[str, Any] = item) -> None:
                target["constraints_draft"] = text
                store.save(state)

            constraints_default = "No additional constraints" if count == 1 and not imported_groups else ""
            item["constraints"] = ask_multiline(
                "Enter constraints. You can use multiple lines and LaTeX formulas.",
                initial=str(item.get("constraints_draft", constraints_default)),
                on_change=save_constraints,
            )
            item.pop("constraints_draft", None)
            store.save(state)
        if index == count:
            item.setdefault("points", points_left)
            store.save(state)
            print(f"This is the last subtask: {item['points']} points were assigned.")
        elif "points" not in item:
            item["points"] = ask_int(
                f"Points for subtask {index}",
                minimum=0,
                validator=lambda value, left=points_left: value <= left,
            )
            store.save(state)
        points_left -= int(item["points"])
        if points_left < 0:
            raise RuntimeError("Saved scoring exceeds 100 points.")

    subtasks = [
        Subtask(
            index=index,
            name=str(item["name"]),
            points=int(item["points"]),
            constraints=str(item["constraints"]),
        )
        for index, item in enumerate(partial_subtasks[:count], 1)
    ]
    if imported_groups:
        from types import SimpleNamespace
        from .registry import bind_subtasks
        bind_subtasks(SimpleNamespace(subtasks=subtasks), imported['test_groups'])
        print('Scoring and group assignments were retained from the imported Solve manifest.')
    print_subtask_summary(
        [(item.index, item.name, item.points, item.constraints) for item in subtasks]
    )

    config = ProjectConfig(
        codename=codename,
        title=title,
        origin=origin,
        language_code=language_code,
        original_statement=original_statement,
        subtasks=subtasks,
        statement_idea=statement_idea,
        generator_language=generator_language,
        solution_language=solution_language,
        time_limit_ms=time_limit_ms,
        memory_limit_kb=memory_limit_mb * 1024,
        task_type=task_type,
        statement_source=statement_source,
        statement_pdf=statement_pdf,
        input_package=str(setup.get("input_package", "")),
        image_files=image_files,
        image_placements={str(name): str(text) for name, text in setup.get("image_placements", {}).items()},
        existing_tests=bool(setup.get("existing_tests", False)),
        judge_notes=judge_notes,
    )
    state.config = config
    state.setup = {}
    store.save(state)
    return config, store
