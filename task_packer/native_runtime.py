"""Project-owned adapters for isolated local libsolve execution."""
from pathlib import Path
import tempfile
import shlex
import shutil
from contextvars import ContextVar

from .sandbox import isolated
from .registry import safe_file

LOCAL_WALL_TIME_FACTOR = 4


def local_wall_time_limit(time_limit: int | None) -> int | None:
    """Allow scheduler stalls locally without relaxing the measured CPU limit."""
    return time_limit * LOCAL_WALL_TIME_FACTOR if time_limit is not None else None


def install_local_wall_time_margin(checker_root: Path | None = None) -> None:
    """Separate libsolve's no-jail wall watchdog from its CPU-time grading limit."""
    from libsolve.execution.language_utils import LanguageUtils
    from libsolve.execution.config import PYTHON3_PATH, PYTHON_COMPILATION_SCRIPT
    from libsolve.package.program import Program

    if getattr(LanguageUtils, '_packer_wall_margin_installed', False):
        return
    original = LanguageUtils._run_command_no_jail.__func__
    permissions = ContextVar('native_program_permissions', default=None)
    active_program = ContextVar('native_active_program', default=None)
    python_utils = Program(Path('.'), 'main.py', 'python3').language_utils
    original_language = Program.language_utils.fget

    class PythonDependencies(python_utils):
        """Retain declared file layout while using Solve's Python compiler."""

        @classmethod
        def exec_path(cls, source_path, preparation_path):
            program = active_program.get()
            return safe_file(preparation_path, program.name).with_suffix('.pyc').resolve()

        @classmethod
        def prepare(cls, source_path, output_directory, **kwargs):
            if kwargs.get('jail_params') is not None:
                raise RuntimeError('Python dependencies are supported only by the isolated local adapter.')
            program = active_program.get()
            for name in dict.fromkeys([program.name, *program.additional_files_names]):
                source = safe_file(program.path, name)
                target = safe_file(output_directory, name)
                if not source.is_file():
                    raise ValueError(f'Missing declared Python file: {source}')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            # The upstream compiler handles the entry point; helpers remain as
            # source in this private tree, available only to this program.
            kwargs['additional_files'] = []
            return super().prepare(safe_file(output_directory, program.name), output_directory, **kwargs)

        @classmethod
        def get_command(cls, source_path, preparation_path, **kwargs):
            if kwargs.get('jail_params') is not None:
                raise RuntimeError('Python dependencies are supported only by the isolated local adapter.')
            cmd, jail = super().get_command(source_path, preparation_path, **kwargs)
            # Isolated Python excludes the supervisor's environment and site
            # packages. Add only the entry directory and declared file tree.
            bootstrap = ('import sys,runpy; from pathlib import Path; '
                         'entry=sys.argv[1]; root=sys.argv[2]; '
                         'sys.argv=[entry,*sys.argv[3:]]; '
                         'sys.path[:0]=[str(Path(entry).parent),root]; '
                         'runpy.run_path(entry,run_name="__main__")')
            cmd.params = [cmd.params[0], '-I', '-B', '-c', bootstrap,
                          *cmd.params[1:2], str(preparation_path), *cmd.params[2:]]
            return cmd, jail

    def language(program):
        if program.prog_lang_name == 'python3' and program.additional_files_names:
            return PythonDependencies
        return original_language(program)

    Program.language_utils = property(language)

    def scoped(method, compiling=False):
        def invoke(program, *args, **kwargs):
            if program.language_utils is PythonDependencies:
                names = [program.name, *program.additional_files_names]
                for name in names:
                    safe_file(program.path, name)
                bytecode = Path(program.name).with_suffix('.pyc')
                if any(Path(name) == bytecode for name in names):
                    raise ValueError(f'Declared Python file conflicts with entry bytecode: {bytecode}')
            readable = [program.source_path.resolve(), *program.additional_files]
            build = program.preparation_path
            writable = []
            if build is not None:
                (writable if compiling else readable).append(build.resolve())
            if not compiling and checker_root is not None and program.path.resolve() == checker_root.resolve():
                params = kwargs.get('params', [])
                params = shlex.split(params) if isinstance(params, str) else params
                readable.extend(Path(value).resolve() for value in params)
            cwd = build if program.language_utils is PythonDependencies and not compiling else None
            token = permissions.set((readable, writable, cwd))
            program_token = active_program.set(program)
            try:
                return method(program, *args, **kwargs)
            finally:
                active_program.reset(program_token)
                permissions.reset(token)
        return invoke

    def run_with_wall_margin(cls, cmd, time_limit=None):
        allowed = permissions.get()
        if allowed is None:
            raise RuntimeError('Native execution has no program isolation scope')
        readable, writable, cwd = allowed
        executable = Path(str(cmd.params[0]))
        if executable.is_file():
            # A virtualenv interpreter symlink is outside the nested sandbox;
            # execute its mounted target without exposing the virtualenv tree.
            cmd.params[0] = str(executable.resolve())
            readable = [*readable, executable.resolve()]
            if executable.resolve() == Path(PYTHON_COMPILATION_SCRIPT).resolve():
                # The compiler script's env shebang can select a different
                # system Python and produce bytecode the runtime cannot read.
                interpreter = Path(PYTHON3_PATH).resolve()
                cmd.params.insert(0, str(interpreter))
                readable.append(interpreter)
        with tempfile.TemporaryDirectory(prefix='native-invocation-') as temporary:
            # Redirections stay with the trusted supervisor. The program receives
            # only the resulting descriptors, never the surrounding output tree.
            cmd.params = isolated(
                [str(value) for value in cmd.params], work=Path(temporary),
                readable=readable, writable=writable, cwd=cwd,
            )
            return original(cls, cmd, time_limit=local_wall_time_limit(time_limit))

    Program.prepare = scoped(Program.prepare, compiling=True)
    Program.run = scoped(Program.run)
    Program.run_from_folder = scoped(Program.run_from_folder)
    Program.get_run_command = scoped(Program.get_run_command)
    LanguageUtils._run_command_no_jail = classmethod(run_with_wall_margin)
    LanguageUtils._packer_wall_margin_installed = True
