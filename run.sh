#!/bin/sh
# Local menu service. Started by launchd (see `just install-service`) or by hand.
set -e
cd "$(dirname "$0")"

# launchd hands processes a minimal PATH. uv lives in Homebrew, and the OCR step
# shells out to `claude` in ~/.local/bin - put both back.
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

exec uv run --frozen uvicorn app.server:app \
    --host 127.0.0.1 \
    --port "${KITCHEN_PORT:-8787}" \
    --log-level info
