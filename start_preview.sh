#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -x ".venv/bin/python" ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install -r requirements.txt >/dev/null
exec .venv/bin/python canon_eos_preview.py "$@"
