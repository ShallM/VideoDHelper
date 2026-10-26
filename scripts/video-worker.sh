#!/bin/zsh
set -euo pipefail
PROJECT_ROOT="${0:A:h:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
exec "$PROJECT_ROOT/.venv/bin/python" -u "$PROJECT_ROOT/backend/worker.py" "$@"
