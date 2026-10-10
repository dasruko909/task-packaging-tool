#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
base_probe='import openai; import prompt_toolkit'
solve_probe='import solve_cli; from libsolve.package import Package; from importlib.metadata import version; assert version("libsolve")=="1.0.11" and version("solve-cli")=="1.0.14"'
if ! .venv/bin/python -c "$base_probe" >/dev/null 2>&1; then
    echo 'Setting up the application environment…'
    if ! .venv/bin/python -m pip install -r requirements.txt; then
        echo 'Could not install application requirements. Check the error above and try again.' >&2
        exit 1
    fi
    if ! .venv/bin/python -c "$base_probe" >/dev/null 2>&1; then
        echo 'Application requirements were installed, but the base environment is still incomplete.' >&2
        exit 1
    fi
fi

if [[ -f vendor/wheels/libsolve-1.0.11-py3-none-any.whl && -f vendor/wheels/solve_cli-1.0.14-py3-none-any.whl ]]; then
    if ! .venv/bin/python -c "$solve_probe" >/dev/null 2>&1; then
        echo 'Installing the supplied Solve 4 wheels…'
        if ! .venv/bin/python -m pip install \
            vendor/wheels/libsolve-1.0.11-py3-none-any.whl \
            vendor/wheels/solve_cli-1.0.14-py3-none-any.whl; then
            echo 'Could not install the supplied Solve 4 wheels. Check the error above.' >&2
            exit 1
        fi
        if ! .venv/bin/python -c "$solve_probe" >/dev/null 2>&1; then
            echo 'The supplied wheels installed, but the required Solve versions are still unavailable.' >&2
            exit 1
        fi
    fi
elif ! .venv/bin/python -c "$solve_probe" >/dev/null 2>&1; then
    echo 'Solve 4 wheels are not present. Basic workflows are available; Solve features remain unavailable.' >&2
fi
exec .venv/bin/python packer.py "$@"
