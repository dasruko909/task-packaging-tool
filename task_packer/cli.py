"""Command-line arguments, project creation, and workflow resumption."""

from __future__ import annotations

import argparse
import os
import sys

from .console import ProjectDeleted, SavedExit, ask_int
from .costs import conservative_project_estimate
from .models import WorkflowState
from .onboarding import gather_config
from .openai_client import OpenAIClient
from .storage import StateStore
from .workflow import Workflow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A resumable Solve 4 package generator powered by OpenAI GPT."
    )
    parser.add_argument("--project", help="project code to resume")
    parser.add_argument("--new", action="store_true", help="always create a new project")
    parser.add_argument("--status", action="store_true", help="show saved projects and exit")
    parser.add_argument("--task-type", choices=["auto", "standard", "multiple", "interactive"],
                        help="change the task type and rebuild dependent materials")
    parser.add_argument("--verify", action="store_true", help="re-check an already completed package")
    parser.add_argument('--revise', help='open a stage for revision, e.g. rules, test_plan, solution:1, verification_materials')
    parser.add_argument('--edit', action='store_true', help='menu for revisions, undo, and custom brute force programs')
    parser.add_argument('--delete', action='store_true', help='move the specified project to recoverable trash')
    parser.add_argument('--undo', action='store_true', help='undo the last approval')
    parser.add_argument('--history', action='store_true', help='show saved project versions')
    parser.add_argument('--restore', metavar='ID', help='restore a --history version and preserve the current one in history')
    parser.add_argument('--feedback', help='feedback for --revise statement/solutions/rules')
    parser.add_argument('--audit-statement', action='store_true', help='check that the statement matches the source and automatically fix discrepancies (API)')
    parser.add_argument("--doctor", action="store_true", help="check the Solve installation and compilers")
    parser.add_argument("--setup", action="store_true",
                        help="configure a private Solve connection once")
    parser.add_argument("--download", metavar="CODE",
                        help="download an existing Solve task and open it as an editable project")
    parser.add_argument("--solve", choices=["check", "zip", "preview", "upload", "rejudge", "report", "connection"],
                        help="manage a package through Solve without OpenAI calls")
    return parser


def _print_projects(projects: list[tuple[str, WorkflowState]]) -> None:
    if not projects:
        print("No saved projects.")
        return
    print("Saved projects:")
    for index, (codename, state) in enumerate(projects, 1):
        if state.finished and state.config and (
            not state.config.specification or not any(
                (state.config.package_dir / 'validators' / f'input_validator{suffix}').is_file()
                for suffix in ('.cpp', '.py')
            )
            or not (state.config.package_dir / 'verification/mutants.json').is_file()
        ):
            print(f"  {index}. {codename} — requires verification upgrades from a newer packer version")
            continue
        if state.finished:
            status = "ready"
            if state.config:
                from .freshness import changed_files
                if changed_files(state.config):
                    status = 'requires rechecking'
        elif state.config is None:
            status = "configuration in progress"
        else:
            status = f"in progress ({len(state.completed)} steps)"
        print(f"  {index}. {codename} — {status}")


def _migrate_legacy_completion(state: WorkflowState, store: StateStore) -> None:
    """Reopen legacy finished projects only after their exclusive lock is held."""
    if state.finished and state.config and (
        not state.config.specification
        or not any(
            (state.config.package_dir / 'validators' / f'input_validator{suffix}').is_file()
            for suffix in ('.cpp', '.py')
        )
        or not (state.config.package_dir / 'verification/mutants.json').is_file()
    ):
        state.finished = False
        store.save(state)


def _select_state(args: argparse.Namespace) -> tuple[WorkflowState, StateStore]:
    projects = StateStore.available()
    if args.project:
        store = StateStore(args.project)
        if not store.path.exists():
            raise RuntimeError(f"No saved project exists: {args.project}")
        store.acquire_lock()
        if not store.path.exists():
            raise RuntimeError(f"The project disappeared while opening it: {args.project}")
        state = store.load()
        _migrate_legacy_completion(state, store)
        if state.config is None:
            gather_config(state, store)
        return state, store

    unfinished = projects
    if unfinished and not args.new:
        _print_projects(unfinished)
        selection = ask_int(
            "Choose a project number to resume, or enter 0 to create a new one",
            default=1,
            minimum=0,
            validator=lambda value: value <= len(unfinished),
        )
        if selection:
            codename, _ = unfinished[selection - 1]
            store = StateStore(codename)
            store.acquire_lock()
            state = store.load()
            _migrate_legacy_completion(state, store)
            if state.config is None:
                gather_config(state, store)
            return state, store

    state = WorkflowState()
    _, store = gather_config(state)
    return state, store


def main() -> None:
    args = _parser().parse_args()
    if args.delete and not args.project:
        _parser().error("--delete also requires --project CODE")
    store = None
    if args.doctor:
        from .solve_menu import show_doctor
        if not show_doctor():
            raise SystemExit(1)
        return
    if args.status:
        _print_projects(StateStore.available())
        return
    try:
        if args.setup or args.solve == "connection":
            from .solve_menu import configure_connection
            configure_connection()
            if args.setup:
                from .openai_client import configure_api_key
                configure_api_key()
            return
        if args.download:
            if args.project or args.new:
                _parser().error("--download cannot be combined with --project or --new")
            from .solve_menu import download_for_editing
            download_for_editing(args.download)
            args.project = args.download
        state, store = _select_state(args)
        # Keep the handle alive until this command exits. Different project codes
        # remain independent and can be processed concurrently.
        project_lock = store.acquire_lock()
        from .revisions import cleanup, edit_menu, history, restore, revise, RestartWorkflow
        if args.delete:
            from .console import ask
            from .storage import delete_project
            print(
                f"Project {store.codename!r}, its input/ directory, output/, and ZIP "
                "will be moved to .packer-trash."
            )
            if ask(f"To delete it, enter the exact project code: {store.codename}") != store.codename:
                print("The code does not match. The project was not changed.")
                return
            raise ProjectDeleted(str(delete_project(store.codename)))
        cleanup(state, store)
        if args.history:
            import json
            for path in history(store):
                print(path.name + ': ' + json.loads((path / 'checkpoint.json').read_text())['reason'])
            return
        if args.restore:
            restore(state, store, args.restore)
        if args.edit and not edit_menu(state, store):
            return
        if args.undo:
            keys = [k for k in state.setup.get('approvals', state.completed) if k not in {'import', 'finalize'}]
            if not keys:
                raise RuntimeError('There are no approvals to undo.')
            revise(state, store, keys[-1])
        if args.revise:
            feedback = args.feedback or ''
            if args.revise == 'rules' and not feedback:
                from .console import ask_multiline
                def save_changes(value):
                    state.setup['rules_change'] = value
                    store.save(state)
                feedback = ask_multiline('Describe the new rules or variable constraints.',
                    initial=state.setup.get('rules_change', ''), on_change=save_changes)
                if not feedback.strip():
                    raise RuntimeError('No rule change was provided.')
            revise(state, store, args.revise, feedback)
            state.setup.pop('rules_change', None)
            store.save(state)
        if args.audit_statement:
            workflow = Workflow(state, store, OpenAIClient(starting_usage=state.usage))
            try:
                workflow.audit_statement()
            except RestartWorkflow:
                workflow.run()
            return
        if args.solve:
            from .solve_menu import action
            if state.config is None:
                raise RuntimeError("Configure the project first.")
            action(state.config, args.solve)
            return
        if args.task_type and state.config:
            if state.config.task_type != args.task_type:
                revise(state, store, 'task_type', reuse=False, updates={'task_type': args.task_type})
        if args.verify:
            if state.config is None or not state.is_done("editorial"):
                raise RuntimeError("Finish generating the package before using --verify.")
            from .solve_menu import check
            result = check(state.config)
            if result['ok'] is not True:
                raise RuntimeError('Package verification failed.')
            return
        if state.config is not None and not state.usage.get("input_tokens"):
            model = os.environ.get('OPENAI_MODEL', 'gpt-6-astra')
            input_tokens, output_tokens, estimate = conservative_project_estimate(
                len(state.config.subtasks), state.config.task_type, model
            )
            print(
                f"\nEstimated full-project cost for {model}: "
                f"${estimate:.3f} (assuming {input_tokens:,} input and "
                f"{output_tokens:,} output tokens). PDFs and revisions may change this."
            )
        while True:
            if not state.finished:
                Workflow(state, store, OpenAIClient(starting_usage=state.usage)).run()
            if not sys.stdin.isatty():
                break
            from .solve_menu import menu
            if not menu(state.config):
                break
            state = store.load()
    except ProjectDeleted as deleted:
        print(f"\nThe project was removed from the active list. Recoverable copy: {deleted.archive}")
    except (SavedExit, KeyboardInterrupt, EOFError):
        print("\nProgress is saved. Run the program again to return here.")
        if store is not None:
            from .console import project_command
            print(project_command(store.codename))
    except (RuntimeError, OSError, ValueError) as error:
        print(f"\nError: {error}", file=sys.stderr)
        if store is not None:
            from .console import project_command
            print('To resume the project after resolving the problem, run:\n' +
                  project_command(store.codename), file=sys.stderr)
        sys.exit(1)
