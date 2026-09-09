#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
if ! XDG_CONFIG_HOME="$PWD/.packer-projects/runtime-config" .venv/bin/python -c 'import openai; import prompt_toolkit; import solve_cli; from libsolve.package import Package; from importlib.metadata import version; assert version("libsolve")=="1.0.11" and version("solve-cli")=="1.0.14"' >/dev/null 2>&1; then
    echo 'Setting up the application environment and Solve CLI…'
    install_args=(-r requirements.txt)
    if [[ -f vendor/wheels/libsolve-1.0.11-py3-none-any.whl && -f vendor/wheels/solve_cli-1.0.14-py3-none-any.whl ]]; then
        install_args+=(vendor/wheels/libsolve-1.0.11-py3-none-any.whl vendor/wheels/solve_cli-1.0.14-py3-none-any.whl)
    else
        echo 'Solve wheels are not present. Some Solve features will remain unavailable.'
    fi
    .venv/bin/python -m pip install "${install_args[@]}"
fi
exec .venv/bin/python packer.py "$@"
