#!/usr/bin/env bash
# Start the AiAS Memory service (the NEDB connector) on 127.0.0.1:8000.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
export NEDB_PYTHON_PATH="${NEDB_PYTHON_PATH:-$HOME/workspace/nedb-portal-load/python}"
exec venv/bin/python -m aias_memory.server
