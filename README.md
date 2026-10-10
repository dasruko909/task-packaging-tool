# Task Packaging Tool

A step-by-step tool that turns a task statement into a checked Solve 4 package. It can start from text, a PDF, tests, or an existing package, and saves progress so closing the terminal does not discard work. Polish is the default statement language; enter `en` during setup to generate an English statement. Editorials are generated in English.

## First launch on Linux

Python 3.10 or newer is required. On Ubuntu or Debian, install the requirements, clone the repository, and start setup with one command. Internet access is needed for the system packages, GitHub, and public Python packages:

```bash
sudo apt update && sudo apt install -y git python3 python3-venv g++ bubblewrap && git clone https://github.com/dasruko909/task-packaging-tool.git && cd task-packaging-tool && mkdir -p vendor/wheels && ./run.sh --setup
```

This creates a local `.venv`, installs the public Python requirements, and prompts for the OpenAI API key and Solve server token. The API key is used to generate or revise materials; the Solve token is used for server actions such as download, upload, preview, or rejudge. Credentials are saved privately outside task packages. Basic local use does not require Solve server access; to skip configuring it, provide `OPENAI_API_KEY` through your environment and run `./run.sh`. If you already have a checkout, update it with `git pull --ff-only` instead of cloning. Other Linux distributions should install the equivalent packages with their package manager.

The app can run without the private Solve wheels, but native Solve validation, compilation/grading, ZIP export, and server features need them. These wheels are not publicly distributed or included in this repository. If you are authorized to use them, place both files below in `vendor/wheels/`, then run `./run.sh`; the launcher installs them in `.venv`. Do not commit or redistribute the private third-party files.

```text
libsolve-1.0.11-py3-none-any.whl
solve_cli-1.0.14-py3-none-any.whl
```

Run `./run.sh --doctor` to check Python, compilers, Bubblewrap isolation, and Solve availability, then use `./run.sh` to start the app. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party notices.

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

The default model remains `gpt-6-astra`. Set `OPENAI_MODEL` to `gpt-6.1-sol` or `gpt-6-luna` to use an alternate model, without changing the terminal prompts. Other model IDs (including snapshot IDs) are rejected before generation; prices are never guessed. Requests use the public OpenAI endpoint and Standard processing (`service_tier=default`); other `OPENAI_BASE_URL` endpoints are rejected because their prices are not configured.

There is **no project monetary cap by default**. The program estimates cost using the selected model before work starts and records actual usage after each response. To enforce a cap on accumulated project spending, set a finite positive USD amount:

```bash
PACKER_BUDGET_USD=10 OPENAI_MODEL=gpt-6.1-sol ./run.sh --project bikes
```

To remove a previously exported cap, run `unset PACKER_BUDGET_USD`, or use `PACKER_BUDGET_USD= ./run.sh --project bikes`. Zero, negative, NaN, infinity, and malformed amounts are rejected. Raising or removing a cap does not reset spending. Resuming, switching models, and restoring earlier content retain accumulated actual cost and tokens. This application cap concerns paid OpenAI API requests; it is independent of your Codex allowance.

Before each call, an explicit cap must cover the request's conservative maximum cost as well as existing spending and unconfirmed charges. Text input is bounded by UTF-8 bytes (including JSON schemas) plus framing allowance. PDFs/images and other attachments use the model's entire remaining context capacity because extracted text and image tokens cannot be bounded reliably from file size. Output is bounded by the requested limit, including reasoning tokens. The bound includes possible cache writes and long-context pricing, so a small cap may refuse a document call even when its eventual charge would be smaller. A refusal saves progress; increase or remove the cap and resume. Automatic API retries are disabled to keep each preflight valid.

Empty and failed responses retain any reported usage. If an attempted request returns no usage (including a connection interruption), its maximum possible charge is saved separately as `unconfirmed_cost_usd`, without inventing actual tokens or cost. These reservations also count against future caps and appear in reports. Reports use `null` for the budget and percentage when no cap is set. Historical actual costs are retained, rather than repricing aggregate tokens at the current model's rate.

Standard rates verified on 2026-10-10, in USD per million tokens:

| Model | Input | Cached input | Cache writes | Output |
|---|---:|---:|---:|---:|
| [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) | 10.00 | 1.00 | 12.50 | 50.00 |
| [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol) | 2.00 | 0.10 | 2.50 | 10.00 |
| [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | 0.10 | 0.01 | 0.125 | 0.50 |

All three support Responses, text/image input, structured output, a 1,050,000-token context and up to 128,000 output tokens. Above 272,000 input tokens **in one request**, input/cache rates double and output rates multiply by 1.5 for that whole request. The initial project estimate assumes 6,000 input tokens per call; it is an estimate, not the cap preflight. See the [Responses API reference](https://developers.openai.com/api/reference/python/resources/responses/methods/create) and [PDF input details](https://developers.openai.com/api/docs/guides/file-inputs).

Generation and review requests send relevant task materials to OpenAI, including statement text, PDFs, selected images, code, feedback, and execution diagnostics. Do not supply confidential material without authorization. Task code runs locally without network access inside `bubblewrap`.

Imported Python programs can use helpers declared in `additional_files_names`,
including nested paths relative to their solution, generator or checker directory.
Local Runner checks, native Solve preparation/grading and the exported
`./check_with_solve.sh --reproduce` use only the entry point and those declared files
in a private staging tree. Helper files and declarations remain in the ZIP and
verification hashes. Unmodified libsolve 1.0.11 still rejects Python additional
files; this project-owned adapter supports local execution, not remote jail/server
execution. No private Solve library source is modified or included in the adapter.
