"""Manifest program selection and isolated compilation dependencies."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from task_packer.execution import ExecutionError, Runner
from task_packer.registry import programs


class ProgramDependencyTests(unittest.TestCase):
    def test_nested_cpp_entry_finds_declared_header_from_staged_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / 'solutions'
            source = source_root / 'entries/main.cpp'
            header = source_root / 'shared.hpp'
            source.parent.mkdir(parents=True)
            (root / 'public').mkdir()
            (root / 'work').mkdir()
            source.write_text(
                '#include "shared.hpp"\n#include <iostream>\n'
                'int main(){std::cout << shared_value;}\n',
                encoding='utf-8',
            )
            header.write_text('inline constexpr int shared_value = 42;\n', encoding='utf-8')

            runner = Runner(root / 'work', root / 'public')
            runner.compile(
                source, dependencies=(header,), root=source_root,
                language='cpp17', label="registered solution 'entries/main.cpp'",
            )

            self.assertEqual(runner.program(source), '42')

    def test_cpp17_program_gets_nested_and_declared_files_under_bubblewrap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / 'solutions'
            public = root / 'public'
            work = root / 'work'
            for directory in (
                source_root / 'entries', source_root / 'nested', source_root / 'shared',
                source_root / 'declared', source_root / 'assets', public, work,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            source = source_root / 'entries/main.cpp'
            source.write_text(
                '#define BONUS_HEADER "../declared/bonus.hpp"\n'
                '#include BONUS_HEADER\n'
                '#include "../nested/answer.hpp"\n'
                '#include <fstream>\n'
                '#include <iostream>\n'
                'int main(){std::ifstream in("assets/value.txt"); int x; in >> x; '
                'std::cout << answer() + bonus + x;}\n',
                encoding='utf-8',
            )
            (source_root / 'nested/answer.hpp').write_text(
                '#include "../shared/base.hpp"\ninline int answer(){return base;}\n',
                encoding='utf-8',
            )
            (source_root / 'shared/base.hpp').write_text(
                'inline constexpr int base = 30;\n', encoding='utf-8'
            )
            bonus = source_root / 'declared/bonus.hpp'
            bonus.write_text('inline constexpr int bonus = 7;\n', encoding='utf-8')
            data = source_root / 'assets/value.txt'
            data.write_text('5\n', encoding='utf-8')

            runner = Runner(work, public)
            runner.compile(
                source, dependencies=(bonus, data), root=source_root,
                language='cpp17', label="registered solution 'main.cpp'",
            )

            self.assertEqual(runner.program(source), '42')
            self.assertEqual(len(runner.commands), 1)

    def test_imported_c_and_python_programs_remain_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / 'generators'
            public = root / 'public'
            work = root / 'work'
            source_root.mkdir()
            public.mkdir()
            work.mkdir()
            c_source = source_root / 'generator.c'
            c_source.write_text('#include <stdio.h>\nint main(){printf("c");}\n', encoding='utf-8')
            py_source = source_root / 'generator.py'
            (source_root / 'lib').mkdir()
            py_helper = source_root / 'lib/helper.py'
            py_helper.write_text('message = "python"\n', encoding='utf-8')
            py_source.write_text(
                'from lib.helper import message\nprint(message, end="")\n', encoding='utf-8'
            )

            runner = Runner(work, public)
            runner.compile(c_source, language='c', label="registered generator 'generator.c'")
            runner.compile(
                py_source, dependencies=(py_helper,), root=source_root,
                language='python3', label="registered generator 'generator.py'",
            )

            self.assertEqual(runner.program(c_source), 'c')
            self.assertEqual(runner.program(py_source), 'python')

    def test_missing_declared_dependency_names_the_program(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / 'solutions'
            source_root.mkdir()
            (root / 'public').mkdir()
            (root / 'work').mkdir()
            source = source_root / 'model.cpp'
            source.write_text('int main(){}\n', encoding='utf-8')

            with self.assertRaisesRegex(
                ExecutionError, "Missing dependency for registered solution 'model.cpp'"
            ):
                Runner(root / 'work', root / 'public').compile(
                    source, dependencies=(source_root / 'missing/data.txt',),
                    root=source_root, language='cpp17',
                    label="registered solution 'model.cpp'",
                )

    def test_registered_unsupported_language_names_the_program(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / 'solutions'
            source_root.mkdir()
            (root / 'public').mkdir()
            (root / 'work').mkdir()
            source = source_root / 'model.java'
            source.write_text('class Model {}\n', encoding='utf-8')

            with self.assertRaisesRegex(
                ExecutionError, "Unsupported language 'java' for registered solution 'model.java'"
            ):
                Runner(root / 'work', root / 'public').compile(
                    source, root=source_root, language='java',
                    label="registered solution 'model.java'",
                )

    def test_non_string_registered_language_names_the_program(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / 'solutions'
            source_root.mkdir()
            (root / 'public').mkdir()
            (root / 'work').mkdir()
            source = source_root / 'model.cpp'
            source.write_text('int main(){}\n', encoding='utf-8')

            with self.assertRaisesRegex(
                ExecutionError, "Unsupported language 17 for registered solution 'model.cpp'"
            ):
                Runner(root / 'work', root / 'public').compile(
                    source, root=source_root, language=17,
                    label="registered solution 'model.cpp'",
                )

    def test_manifest_roles_select_only_registered_entry_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = {
                'checker': {'name': 'chosen.cpp'},
                'solutions': [{'name': 'model.py', 'prog_lang': 'python3'}],
                'generators': [{'name': 'gen.c', 'prog_lang': 'c'}],
            }

            selected = programs(root, manifest)

            self.assertEqual(
                [(item.role, item.name) for item in selected],
                [('checker', 'chosen.cpp'), ('solution', 'model.py'), ('generator', 'gen.c')],
            )


if __name__ == '__main__':
    unittest.main()
