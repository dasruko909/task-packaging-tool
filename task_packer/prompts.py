"""English prompt templates for the Solve 4 task packer."""

from __future__ import annotations

import json

from .models import ProjectConfig, Subtask
from .statement_format import imported_examples, sample_pairs, subtask_table


WRITING_RULES_PL = """
Napisz dopracowaną treść zadania w języku wskazanym przez ustawienie language_code,
w Markdownzie Solve 4.
Zachowaj sens matematyczny, ograniczenia i poprawne przykłady źródłowe.
Użyj krótkiej, własnej fabuły, a następnie precyzyjnie wyjaśnij każdą zmienną,
operację, wejście, wyjście i warunek poprawności. Dodawaj przykłady tylko wtedy,
gdy potrzeba ich dwóch lub trzech, oraz wyjaśnij każdy wynik na podstawie jego
konkretnych danych. Filtry przykładów używają wyłącznie nazw input_file i
output_file; pliki wejścia i wyjścia przykładów mogą mieć najwyżej 2048 bajtów
UTF-8 razem z końcowym znakiem nowej linii. Użyj wymaganych sekcji, pustych
wierszy między blokami i nie twórz HTML-a. Nie dodawaj tytułu ani metadanych,
ponieważ Solve czyta je z config.json. Zwróć gotowy do publikacji Markdown w wybranym języku.
""".strip()

WRITING_RULES_EN = """
Write a polished task statement in the language selected by language_code, using
Solve 4 Markdown. Preserve the mathematical meaning, limits, and correct source
examples. Use a short original story, then explain every variable, operation,
input, output, and validity condition precisely. Add examples only when two or
three are needed, and explain every result using its concrete data. Example filters
must use only input_file and output_file names; example input and output files may
contain at most 2048 UTF-8 bytes including the final newline. Use the required
sections, blank lines between blocks, and no HTML. Do not add a title or metadata
because Solve reads those from config.json. Return publishable Markdown in the
selected language.
""".strip()


def subtasks_text(config: ProjectConfig) -> str:
    return "\n".join(
        f"{item.index}. {item.name}; {item.points} pts; constraints: {item.constraints}"
        for item in config.subtasks
    )


def subtask_detection_prompt(original_statement: str, imported_groups: list[dict] | None = None) -> tuple[str, str]:
    system = """Extract real subtasks from the original statement. Return only JSON with
has_explicit_subtasks, subtasks (index, name, points, constraints), and reason.
Do not invent variants. If no subtasks or partial scoring exist, return one subtask:
index 1, name Full constraints, points 100, constraints No additional constraints.
Preserve stated conditions and scoring; use concise English names."""
    metadata = ""
    if imported_groups:
        metadata = "\n\nManifest groups for reference only:\n" + json.dumps(imported_groups, ensure_ascii=False)
    return system, f"Original statement:\n{original_statement}{metadata}"


def image_placements_text(config: ProjectConfig) -> str:
    if not config.image_files:
        return "no separate images"
    if not config.image_placements:
        return ", ".join(config.image_files)
    return "\n".join(f"- {name}: {config.image_placements.get(name, 'no placement specified')}" for name in config.image_files)


def planned_sample_tests(config: ProjectConfig) -> list[dict[str, str]]:
    plan = config.test_plan if isinstance(config.test_plan, dict) else {}
    return plan.get("tests") if isinstance(plan.get("tests"), list) else []


def test_blueprint_prompt(config: ProjectConfig) -> tuple[str, str]:
    system = """Plan public samples and the full Solve 4 test suite. Return only JSON with
tests (input, output, description) and subtasks (index, generator_runs, corner_tests).
Preserve valid source samples; add samples only until there are two or three. The
application calculates the total itself: do not return total_tests. Each subtask needs
at least one generator run and no more than 26 generated plus corner tests. Samples
must be representative and each input or output must be at most 2048 UTF-8 bytes
including its final newline. Do not invent uncertain task rules."""
    user = f"Original statement:\n{config.original_statement}\n\nUser editorial idea: {config.statement_idea.strip() or 'none'}\nTarget subtasks:\n{subtasks_text(config)}\nImages: {image_placements_text(config)}\nTask type: {config.task_type}\nJudge specification: {config.judge_notes or 'not applicable'}"
    return system, user


def statement_prompt(config: ProjectConfig) -> tuple[str, str]:
    english = config.language_code.lower() == "en"
    planned = planned_sample_tests(config)
    samples = sample_pairs(config, len(planned)) if planned else sample_pairs(config)
    images = image_placements_text(config)
    writing_rules = WRITING_RULES_EN if english else WRITING_RULES_PL
    sections = (
        "Use exactly: ## Input, ## Output, ## Constraints, ## Subtasks, ## Example."
        if english else
        "Użyj dokładnie: treść, ## Wejście, ## Wyjście, ## Ograniczenia, ## Podzadania, ## Przykład. Zachowaj polskie znaki."
    )
    tests_rule = (
        "tests contains every agreed example with input, output, and description."
        if english else
        "tests zawiera każdy uzgodniony przykład z input, output i description."
    )
    if config.task_type == "interactive":
        sections = (
            "State that the task is interactive. Use: ## Interaction, ## Constraints, "
            "## Subtasks, ## Sample interaction, ## Local testing tools. State whether "
            "the interactor is adaptive."
            if english else
            "Napisz, że zadanie jest interaktywne. Użyj dokładnie: treść, ## Interakcja, "
            "## Ograniczenia, ## Podzadania, ## Przykładowa interakcja, "
            "## Narzędzia do testowania lokalnego. Podaj, czy interaktorka jest adaptacyjna."
        )
        tests_rule = (
            "tests contains one to three interactor control inputs; output may be omitted."
            if english else
            "tests zawiera od jednego do trzech wejść kontrolnych interaktorki; output może być pominięte."
        )
    examples: list[dict[str, str]] = []
    for (input_file, output_file), test in zip(samples, planned or [{} for _ in samples]):
        item: dict[str, str] = {"input_file": input_file, "output_file": output_file}
        if isinstance(test, dict):
            item.update({key: value for key, value in test.items() if key in {"input", "output", "description"} and isinstance(value, str)})
        examples.append(item)
    sample_rule = (
        "Use exactly the planned examples; do not create new inputs or outputs."
        if english else
        "Użyj dokładnie zaplanowanych przykładów; nie twórz nowych wejść ani wyjść."
    )
    if config.existing_tests and config.task_type != "interactive":
        examples = imported_examples(config)
        tests_rule = (
            "tests must be an empty array because the example data is already in the files."
            if english else
            "tests musi być pustą tablicą, ponieważ dane przykładów są już w plikach."
        )
        sample_rule = (
            "Use the supplied examples in order. tests/in and tests/out are the source of truth."
            if english else
            "Użyj dostarczonych przykładów w kolejności. tests/in i tests/out są źródłem prawdy."
        )
    output_instruction = (
        "Return only JSON with statement_markdown and tests."
        if english else
        "Zwróć wyłącznie JSON z statement_markdown i tests."
    )
    system = f"""{"You are an experienced competitive-programming editor." if english else "Jesteś doświadczonym redaktorem zadań algorytmicznych."}
{writing_rules}
{sections}
{"Write a natural editorial version in English based on the source and the user's idea." if english else "Napisz naturalną wersję redakcyjną po polsku na podstawie źródła i pomysłu użytkownika."}
{"Do not change the task meaning or add uncertain assumptions. Use a neutral, professional contest style." if english else "Nie zmieniaj sensu zadania ani nie dodawaj niepewnych założeń. Użyj neutralnego, profesjonalnego stylu konkursowego."} {output_instruction} {tests_rule}
{sample_rule}
{"Public examples:" if english else "Publiczne przykłady:"}
{json.dumps(examples, ensure_ascii=False, indent=2)}
{"Place this subtask table under ## Subtasks:" if english else "Umieść tę tabelę podzadań pod ## Podzadania:"}
{subtask_table(config)}
{"Image placement:" if english else "Położenie obrazów:"}
{images}"""
    user = (
        f"Original statement:\n{config.original_statement}\n\nUser idea: {config.statement_idea.strip() or 'none'}\n"
        f"Target subtasks:\n{subtasks_text(config)}\nTask type: {config.task_type}\n"
        f"Judging specification: {config.judge_notes or 'not applicable'}\nRules:\n"
        f"{json.dumps(config.specification, ensure_ascii=False)}\nLimits: {config.time_limit_ms} ms, {config.memory_limit_kb} KiB."
        if english else
        f"Oryginalna treść:\n{config.original_statement}\n\nPomysł użytkownika: {config.statement_idea.strip() or 'brak'}\n"
        f"Docelowe podzadania:\n{subtasks_text(config)}\nTyp zadania: {config.task_type}\n"
        f"Specyfikacja oceny: {config.judge_notes or 'nie dotyczy'}\nReguły:\n"
        f"{json.dumps(config.specification, ensure_ascii=False)}\nLimity: {config.time_limit_ms} ms, {config.memory_limit_kb} KiB."
    )
    return system, user


def generator_prompt(config: ProjectConfig, subtask: Subtask, statement: str) -> tuple[str, str]:
    system = f"""Create a deterministic Solve 4 test generator in {config.generator_language}.
It receives an integer seed and profile small, random, or max; prints one valid test to
stdout; uses no files; and exits successfully. Return only JSON with code and an English
description. Escape newlines correctly in JSON."""
    return system, f"Statement:\n{statement}\n\nSubtask {subtask.index}: {subtask.name}\nConstraints: {subtask.constraints}. Points: {subtask.points}."


def corner_prompt(config: ProjectConfig, subtask: Subtask, statement: str, count: int | None = None) -> tuple[str, str]:
    rule = "an array of 2 to 5 objects" if count is None else f"an array of exactly {count} objects"
    system = f"""Create manual boundary tests. Return only JSON with tests: {rule}. Each test
has input and an English description. Include maximum values and cases that expose common heuristics,
greedy assumptions, indexing errors, overflow, and excessive complexity.
Do not provide output because the model solution will generate it."""
    return system, f"Statement:\n{statement}\n\nSubtask {subtask.index}: {subtask.name}\nConstraints: {subtask.constraints}."


def solution_prompt(config: ProjectConfig, subtask: Subtask, statement: str) -> tuple[str, str]:
    extra = " Follow the interactive protocol, flush each query, and handle termination correctly." if config.task_type == "interactive" else ""
    system = f"""Write a complete portable {config.solution_language} solution. Return only JSON
with code and an English description stating the idea, correctness argument, and complexity.
Read stdin, write stdout, use no network or files, and respect all limits.{extra}"""
    return system, f"Statement:\n{statement}\n\nSubtask {subtask.index}: {subtask.name}\nConstraints: {subtask.constraints}."


def critique_prompt(config: ProjectConfig, subtask: Subtask, statement: str, code: str) -> tuple[str, str]:
    system = """Review a competitive-programming solution. Return only JSON with correct,
summary in English, and blocking_issues. Check logic, overflow, complexity, I/O, boundary
cases, and interactive behavior. Report a blocking issue only for a concrete defect."""
    return system, f"Subtask {subtask.index}: {subtask.name}\nConstraints: {subtask.constraints}\n\nStatement:\n{statement}\n\nCode:\n{code}"


def editorial_prompt(config: ProjectConfig, statement: str, full_solution: str) -> tuple[str, str]:
    system = "Write an English contest editorial in Markdown. Explain the observation, algorithm, correctness proof, complexity, and implementation pitfalls. Return only publishable Markdown without HTML."
    return system, f"Statement:\n{statement}\n\nModel solution:\n{full_solution}"


def checker_prompt(config: ProjectConfig, statement: str) -> tuple[str, str]:
    system = """Write a safe custom Solve 4 checker in C++17. It receives input, reference
output, and contestant output as argv[1], argv[2], and argv[3]. Always exit 0 and print
exactly a score from 0 to 100 plus a brief polite English explanation. Validate all data.
Return only JSON with code and description."""
    return system, f"Statement:\n{statement}\n\nScoring rules:\n{config.judge_notes}"


def interactor_prompt(config: ProjectConfig, statement: str) -> tuple[str, str]:
    system = """Write a Solve 4 interactor in C++17. Read contestant queries from stdin,
respond on stdout, flush after every response, validate messages, enforce query limits,
and avoid hangs. Return only JSON with code, public_header, local_tester, and description."""
    return system, f"Statement:\n{statement}\n\nProtocol:\n{config.judge_notes}"


def task_type_prompt(config: ProjectConfig, metadata: dict) -> tuple[str, str]:
    system = """Classify a task as standard, multiple, or interactive. Return only JSON with
task_type, English reason, missing_details, and judge_notes. Interactive means a runtime
dialogue. Multiple means several accepted outputs for one input. Do not infer facts absent
from supplied materials."""
    return system, f"Original statement:\n{config.original_statement}\n\nMetadata:\n{json.dumps(metadata, ensure_ascii=False)}\n\nAuthor notes:\n{config.judge_notes}"


def verification_prompt(config: ProjectConfig, statement: str) -> tuple[str, str]:
    system = """Create independent verification materials. Return only JSON conforming to the
given schema and write all descriptions in English. Use only the C++17 standard library;
do not use Boost or other third-party dependencies. Provide a C++17 input validator and
either null or a standalone C++17 reducer with int main(), stdin input, an optional step
argument, and a reduced candidate written to stdout. Standard and multiple tasks require an exhaustive brute program, a small
generator, and at least two compiling realistic wrong-solution mutants. Interactive tasks
require wrong, truncated, query-limit, and silent clients. Never invent unavailable rules."""
    return system, f"Task type: {config.task_type}\nOriginal statement:\n{config.original_statement}\nRules:\n{json.dumps(config.specification, ensure_ascii=False)}\nStatement:\n{statement}\nSubtasks:\n{subtasks_text(config)}\nJudge rules:\n{config.judge_notes}"


def verification_schema(config: ProjectConfig) -> dict[str, object]:
    """Return the strict response schema for verification materials."""
    text: dict[str, object] = {"type": "string"}
    nullable_text: dict[str, object] = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    mutant = {"type": "object", "properties": {"name": text, "code": text, "description": text}, "required": ["name", "code", "description"], "additionalProperties": False}
    common = {"description": text, "input_validator": text, "mutants": {"type": "array", "items": mutant}, "reducer": nullable_text}
    if config.task_type == "interactive":
        case = {"type": "object", "properties": {"input": text, "client": {"type": "string", "enum": ["wrong.cpp", "truncated.cpp", "query_limit.cpp", "silent.cpp"]}, "kind": {"type": "string", "enum": ["wrong", "truncated", "query_limit", "silent"]}, "score": {"type": "integer"}, "description": text}, "required": ["input", "client", "kind", "score", "description"], "additionalProperties": False}
        properties = {**common, "clients": {"type": "object", "properties": {"wrong": text, "truncated": text, "query_limit": text}, "required": ["wrong", "truncated", "query_limit"], "additionalProperties": False}, "cases": {"type": "array", "items": case}}
    else:
        case = {"type": "object", "properties": {"input": text, "reference": text, "candidate": text, "score": {"type": "integer"}, "kind": {"type": "string", "enum": ["valid", "wrong", "empty", "truncated", "extra", "malformed"]}, "description": text}, "required": ["input", "reference", "candidate", "score", "kind", "description"], "additionalProperties": False}
        properties = {**common, "brute": text, "small_generator": text, "cases": {"type": "array", "items": case}}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
