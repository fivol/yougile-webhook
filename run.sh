#!/bin/zsh
cd "$(dirname "$0")"
# load .env so HOST/PORT (and other settings) are available to the shell
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-9100}"
exec ./.venv/bin/uvicorn app:app --host "$HOST" --port "$PORT"
