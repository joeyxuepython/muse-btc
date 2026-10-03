#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pytest -q
node --check src/muse_btc/static/app.js
node --check src/muse_btc/static/intelligence.js
.venv/bin/muse --help > /dev/null
