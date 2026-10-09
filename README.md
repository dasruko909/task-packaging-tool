# Task Packaging Tool

A step-by-step tool that turns a task statement into a checked Solve 4 package. It can start from text, a PDF, tests, or an existing package, and saves progress so closing the terminal does not discard work. Polish is the default statement language; enter `en` during setup to generate an English statement. Editorials are generated in English.

## Solve 4 wheels (not included)

Wrocław students: create `vendor/wheels/` if it is missing, then place the two
private Solve 4 wheel files there before running the Solve-dependent features:

```text
vendor/wheels/libsolve-1.0.11-py3-none-any.whl
vendor/wheels/solve_cli-1.0.14-py3-none-any.whl
```

These files are intentionally not included in this repository because the author does not have permission to redistribute them. Obtain them from the authorised course or Solve 4 distribution and do not commit or share them through this repository. After copying them, run `./run.sh` again so the private packages can be installed.

Without the wheels, the basic project and package-generation workflow still works, but some functions will not: native Solve validation, compilation and grading, standard-checker installation, downloading tasks, and server-side Solve actions such as upload, preview and rejudge.

The distributions are third-party components and remain the property of their copyright holders. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

```bash
./run.sh --setup
./run.sh
```

Follow the terminal prompts and choose the letter next to an action. Press Enter to choose the default.

## Requirements

- Linux or WSL
- Python 3.10 or newer
- `g++`
- `bubblewrap` (`bwrap`) for safe local isolation
- an OpenAI API key for generation
- a Solve token only for upload or server-side PDF/HTML preview

The first run creates `.venv` and installs dependencies. Check the environment with:

```bash
./run.sh --doctor
```

On Ubuntu or WSL, install missing system dependencies with:

```bash
sudo apt install bubblewrap g++ python3-venv
```

## One-time setup

Run:

```bash
./run.sh --setup
```

The program asks for the Solve URL (usually `https://solve.edu.pl`), a private Solve API token, and an OpenAI API key. Credentials are stored separately from task packages. You can instead set `OPENAI_API_KEY`. Do not put credentials in task materials, prompts, or feedback; review reports and exports before sharing them.

## Create a task

Run `./run.sh`, choose a short project code such as `bikes`, and complete the eight stages. Task code—solutions, generators, validators, checkers, interactors, and verification tools—is written in C++17.

For a long text, paste it and finish with this separate line:

```text
<<<END>>>
```

The program then lets you use, edit, extend, replace, or save the draft. You can safely exit with `z` or Ctrl+C and resume later.

## Source materials

For a project code `bikes`, put source materials in `input/bikes/`.

Project codes use 1–20 ASCII lowercase letters, digits, `-` or `_`. Statement languages are `pl` and `en` (case and surrounding spaces are normalized). Creating a project refuses any existing state, output directory, ZIP or ZIP checksum, including conflicts detected after taking the project lock. Prefilled input remains valid.

Package file references must stay inside their assigned directories; relative subdirectories for programs and dependencies are supported. Symlinks in project paths, package contents and locks are rejected. Explicitly chosen source documents and files for manual import may remain outside the project. These checks do not make imports/exports transactional or protect against another process replacing filesystem components during an operation.


- PDF statement: `input/bikes/statement.pdf`
- Images: `input/bikes/images/`
- Existing package: `input/bikes/package/`

To download an existing Solve task directly:

```bash
./run.sh --download bikes
```

An imported package needs `config.json` directly in `package/`. For example:

```text
input/bikes/package/config.json
input/bikes/package/tests/in/1a.in
input/bikes/package/tests/out/1a.out
```

Do not add an extra directory level. Imported test names and data are retained.

## Output

The package is written to `output/bikes/`. Its main files are:

- `description/{language}.md` — task statement (`pl` by default, or the selected language)
- `editorial/en.md` — English editorial
- `solutions/`, `generators/`, `tests/` — task materials
- `config.json` — Solve manifest
- `verification/report.md` — verification report

Markdown is the source of truth. The packer does not manually create HTML. PDF and HTML can only be generated through an explicit Solve CLI action.

New tests use names such as `0a.in`, `0a.out`, `1a.in`, and `1a.out`. Group 0 always contains public samples; each group supports up to 26 tests.

## Resume, revise, and delete

```bash
./run.sh                         # choose or create a project
./run.sh --status                # list saved projects
./run.sh --project bikes         # resume a project
./run.sh --project bikes --edit  # revision menu
./run.sh --project bikes --undo  # undo last approval
./run.sh --project bikes --history
./run.sh --project bikes --verify
```

The revision menu can reopen a stage, change rules and limits, alter subtasks, restore a saved version, add a custom brute-force program, or delete a project. Deletion requires the exact project code and moves its files to recoverable `.packer-trash/`.

## Solve commands

After completion, use:

```bash
./run.sh --project bikes --solve check
./run.sh --project bikes --solve zip
./run.sh --project bikes --solve report
./run.sh --project bikes --solve preview
./run.sh --project bikes --solve upload
./run.sh --project bikes --solve rejudge
```

`check` validates, compiles, and grades through the supplied Solve libraries. `zip` creates `output/bikes.zip` with a SHA-256 hash. `preview` requests PDF/HTML from Solve. `upload` and `rejudge` always show the target server and request confirmation before network activity.

## Cost and privacy

The default model is `gpt-6-astra`. The program estimates cost before work starts, reports it after each call, and enforces a default $5.00 per-project cap. For a request with an attached source document, the preflight check reserves $1.00 for document input. Set `OPENAI_MODEL` to choose another model.

Generation and review requests send relevant task materials to OpenAI, including statement text, PDFs, selected images, code, feedback, and execution diagnostics. Do not supply confidential material without authorization. Task code runs locally without network access inside `bubblewrap`.
