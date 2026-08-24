set shell := ["bash", "-uc"]

port     := env_var_or_default("KITCHEN_PORT", "8787")
label    := "com.ivo.kitchen"
plist    := env_var("HOME") / "Library/LaunchAgents" / label + ".plist"
# Shared with the other local services started from ~/Developer/start-services.sh.
logfile  := env_var("HOME") / "Developer/logs/kitchen.log"
cachedir := env_var("HOME") / ".cache/kitchen-menus"
# Long-lived token for the OCR shell-out; gitignored, see install-service.
tokenfile := ".ocr-token"

_default:
    @just --list --unsorted

# Install/refresh dependencies
sync:
    uv sync

# Run the server in the foreground
run: sync
    uv run uvicorn app.server:app --host 127.0.0.1 --port {{port}} --reload

# Open the app in a browser
open:
    open http://127.0.0.1:{{port}}

# Fetch every kitchen and print a summary (no server needed)
check: sync
    @uv run python -c "\
    import asyncio, json; from app.aggregator import collect; \
    r = asyncio.run(collect()); \
    print(f'{r.week_start} .. {r.week_end}'); \
    [print(f\"  {s.kitchen:9} ok={s.ok} stale={s.stale} days={len(s.days):2} items={sum(len(d.items) for d in s.days):3} {s.error or ''}\") for s in r.sources]"

# Dump the normalised menu JSON
dump: sync
    @uv run python -c "\
    import asyncio, json; from app.aggregator import collect; \
    print(json.dumps(asyncio.run(collect()).model_dump(mode='json'), ensure_ascii=False, indent=2, default=str))"

# Force a refresh, ignoring cached change-keys
refresh:
    curl -s "http://127.0.0.1:{{port}}/api/menus?force=true" -o /dev/null -w "%{http_code}\n"

# Drop all cached menus and OCR results
clean-cache:
    rm -rf "{{cachedir}}"
    @echo "cleared {{cachedir}}"

# Lint
lint:
    uvx ruff check app

# Format
fmt:
    uvx ruff format app

# Generate the LaunchAgent plist for this machine and load it
install-service:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "$(dirname "{{plist}}")"
    # OCR token: see the comment in the template. Absent is allowed - the service
    # still runs, OCR just falls back to the Keychain session and may expire.
    if [ -r "{{tokenfile}}" ]; then
        tok=$(tr -d '\r\n' < "{{tokenfile}}")
    else
        tok=""
    fi
    if [ -n "$tok" ]; then
        # The token lands inside an XML element, then inside a sed replacement, so
        # it needs both escapes and in that order: XML first (& < >), sed second
        # (\ & |, including the & of any &amp; the first pass just produced).
        esc=$(printf '%s' "$tok" \
            | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' \
            | sed -e 's/[\\&|]/\\&/g')
        token_edit=(-e "s|__OAUTH_TOKEN__|$esc|")
    else
        echo "note: no {{tokenfile}}; OCR will use the Keychain session (run 'just ocr-token')" >&2
        token_edit=(-e "/CLAUDE_CODE_OAUTH_TOKEN/,+1d")
    fi
    # The plist carries a credential. > onto an existing file keeps its old mode,
    # so remove it first and let the umask apply to a fresh 600 file.
    umask 077
    rm -f "{{plist}}"
    sed -e "s|__DIR__|$(pwd)|g" -e "s|__PORT__|{{port}}|g" -e "s|__LOG__|{{logfile}}|g" \
        "${token_edit[@]}" com.ivo.kitchen.plist.template > "{{plist}}"
    launchctl unload "{{plist}}" 2>/dev/null || true
    launchctl load "{{plist}}"
    echo "loaded {{label}} on port {{port}}"

# Store a long-lived OCR token for install-service (minting one if needed)
ocr-token:
    #!/usr/bin/env bash
    set -euo pipefail
    # Read silently: this token is a credential and must not land in shell history.
    printf 'Paste an existing token, or press Enter to mint a new one: ' >&2
    read -rs tok; echo >&2
    if [ -z "$tok" ]; then
        echo "Running 'claude setup-token' - a browser will open." >&2
        claude setup-token
        printf 'Paste the token it printed: ' >&2
        read -rs tok; echo >&2
    fi
    [ -n "$tok" ] || { echo "empty token, aborting" >&2; exit 1; }
    (umask 077; printf '%s\n' "$tok" > "{{tokenfile}}")
    echo "wrote {{tokenfile}} ($(wc -c < "{{tokenfile}}" | tr -d ' ') bytes) - now run 'just install-service'"

# Stop and remove the LaunchAgent
uninstall-service:
    -launchctl unload "{{plist}}" 2>/dev/null
    rm -f "{{plist}}"
    @echo "removed {{label}}"

# Restart the background service
restart: && install-service
    @echo "restarting {{label}}"

# Tail the service log
logs:
    tail -f "{{logfile}}"

# Is the service up?
status:
    @launchctl list | grep {{label}} || echo "not loaded"
    @curl -s -m 3 "http://127.0.0.1:{{port}}/api/health" || echo "not responding"
