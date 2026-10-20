#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3.14 -m venv .venv
fi
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
export OZON_ENABLE_TEST_TOOLS=1
printf '%s\n' 'Starting local API at http://127.0.0.1:8765 (leave this terminal open).'
python -m app.main
