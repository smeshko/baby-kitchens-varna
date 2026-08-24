#!/bin/sh
# Local menu service. Started by launchd (see `just install-service`) or by hand.
set -e
cd "$(dirname "$0")"

# launchd hands processes a minimal PATH. uv lives in Homebrew, and the OCR step
# shells out to `claude` in ~/.local/bin - put both back.
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

# Reload is on by default: this is the only instance of the service, so edits
# under app/ should reach it without a manual restart. Scoped to app/ on purpose
# - the default watch root is the whole project, and .venv alone is thousands of
# files. Set KITCHEN_RELOAD=0 to pin the process (a reload drops the in-memory
# cache and re-warms, ~20s of upstream probes).
if [ "${KITCHEN_RELOAD:-1}" = "0" ]; then
    set --
else
    set -- --reload --reload-dir app
fi

exec uv run --frozen uvicorn app.server:app \
    --host 127.0.0.1 \
    --port "${KITCHEN_PORT:-8787}" \
    --log-level info "$@"
