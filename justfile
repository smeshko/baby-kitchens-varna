set shell := ["bash", "-uc"]

port     := env_var_or_default("KITCHEN_PORT", "8787")
label    := "com.ivo.kitchen"
plist    := env_var("HOME") / "Library/LaunchAgents" / label + ".plist"
logfile  := env_var("HOME") / "Library/Logs/kitchen.log"
cachedir := env_var("HOME") / ".cache/kitchen-menus"

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
    @mkdir -p "$(dirname "{{plist}}")"
    @sed -e "s|__DIR__|$(pwd)|g" -e "s|__PORT__|{{port}}|g" -e "s|__LOG__|{{logfile}}|g" \
        com.ivo.kitchen.plist.template > "{{plist}}"
    -launchctl unload "{{plist}}" 2>/dev/null
    launchctl load "{{plist}}"
    @echo "loaded {{label}} on port {{port}}"

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
