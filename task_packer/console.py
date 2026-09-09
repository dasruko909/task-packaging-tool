"""A clear English terminal interface."""

from __future__ import annotations

from collections.abc import Callable


END_MARKER = "<<<END>>>"


def project_command(codename: str, *arguments: str) -> str:
    """A complete shell command that can be copied safely."""
    import shlex
    return shlex.join(['./run.sh', '--project', codename, *arguments])


class SavedExit(Exception):
    """A controlled exit after the state has been saved."""


class ProjectDeleted(Exception):
    """A controlled exit after moving a project to the packer's trash."""

    def __init__(self, archive: str):
        super().__init__(archive)
        self.archive = archive


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def ask_int(
    prompt: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    validator: Callable[[int], bool] | None = None,
) -> int:
    while True:
        raw = ask(prompt, str(default) if default is not None else "")
        try:
            value = int(raw)
            if minimum is not None and value < minimum:
                raise ValueError
            if validator is not None and not validator(value):
                raise ValueError
            return value
        except ValueError:
            print("Enter a valid integer that meets the stated conditions.")


def ask_multiline(
    prompt: str,
    *,
    initial: str = "",
    on_change: Callable[[str], None] | None = None,
) -> str:
    """Save a draft as it is entered and use it immediately after END."""

    print(f"{prompt}\nFinish with {END_MARKER} on its own line.")
    print("The text is saved continuously and will be used immediately when you finish.")
    lines = initial.splitlines() if initial else []
    if lines:
        print(f"Restored {len(lines)} saved lines. Continue on the next line.")
    while True:
        try:
            line = input()
        except (KeyboardInterrupt, EOFError):
            raise SavedExit
        if line.strip() != END_MARKER:
            lines.append(line)
            if on_change is not None:
                on_change("\n".join(lines))
            continue
        value = "\n".join(lines).strip()
        if on_change is not None:
            on_change(value)
        print("Text saved. Continuing…")
        return value


def _edit_multiline(value: str, *, on_change: Callable[[str], None] | None = None) -> str:
    """An editor for the whole draft, saving after every change."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("enter")
    def finish_or_newline(event):
        buffer = event.current_buffer
        if buffer.document.current_line.strip() == END_MARKER:
            lines = buffer.text.split("\n")
            del lines[buffer.document.cursor_position_row]
            result = "\n".join(lines)
            if on_change is not None:
                on_change(result)
            event.app.exit(result=result)
        else:
            buffer.insert_text("\n")

    session = PromptSession(multiline=True, key_bindings=bindings)
    if on_change is not None:
        session.default_buffer.on_text_changed += lambda buffer: on_change(buffer.text)
    print(f"Edit with the arrow keys and Backspace. Finish with {END_MARKER} on its own line.")
    try:
        return session.prompt("> ", default=value)
    except (KeyboardInterrupt, EOFError):
        if on_change is not None:
            on_change(session.default_buffer.text)
        raise SavedExit


def choose(prompt: str, options: dict[str, str], default: str) -> str:
    while True:
        print(f"\n{prompt}")
        for key, label in options.items():
            default_note = " (default)" if key == default else ""
            print(f"  {key} — {label}{default_note}")
        answer = input(f"Choice [{default}]: ").strip().lower() or default
        if answer in options:
            return answer
        print(
            "That option is not recognized. Enter one of: "
            + ", ".join(options)
            + "."
        )


def ask_yes_no(prompt: str, *, default: bool = False) -> bool:
    """Asks a yes/no question without making the user guess abbreviations."""

    default_key = "y" if default else "n"
    return choose(prompt, {"y": "yes", "n": "no"}, default_key) == "y"


def heading(title: str, subtitle: str = "") -> None:
    """Prints a calm, readable heading for the next form section."""

    width = max(54, len(title) + 4)
    print(f"\n{'═' * width}\n  {title}")
    if subtitle:
        print(f"  {subtitle}")
    print("═" * width)


def print_subtask_summary(rows: list[tuple[int, str, int, str]]) -> None:
    """Shows a concise subtask summary without external libraries."""

    print("\nSubtask summary:")
    for index, name, points, constraints in rows:
        compact = " ".join(constraints.split())
        if len(compact) > 70:
            compact = compact[:67] + "…"
        print(f"  {index:>2}. {points:>3} pts | {name or '(unnamed)'}")
        print(f"      {compact or '(no additional constraints)'}")
