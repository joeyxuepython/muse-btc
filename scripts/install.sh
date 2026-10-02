#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.cache/uv}"
uv sync --frozen
.venv/bin/python -c 'from muse_btc.config import Settings; from muse_btc.storage import Store; Store(Settings().database_path)'
