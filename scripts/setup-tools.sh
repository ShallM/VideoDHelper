#!/bin/zsh
set -euo pipefail
PROJECT_ROOT="${0:A:h:h}"
cd "$PROJECT_ROOT"
uv venv .venv --python "$(command -v python3)" --allow-existing
uv pip install --python .venv/bin/python -r requirements.txt
