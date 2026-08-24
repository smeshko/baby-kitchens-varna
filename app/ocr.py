"""Vision OCR for the one source that publishes its menu only as an image.

Runs through the local `claude -p` CLI so it bills against the Claude
subscription rather than an API key - nothing to configure, no secret to store.
Under launchd that CLI needs CLAUDE_CODE_OAUTH_TOKEN; see the README.

Runs at most once per week: VIP Bebe regenerates the PNG weekly and serves a
stable ETag, so the adapter only calls this when that ETag moves.

The image is tiled first. At 1414x2000 the long edge would be downscaled to
1568px (~78%) and the ingredient lines are already small; two overlapping halves
keep every tile at native resolution.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from PIL import Image

MAX_EDGE = 1500
OVERLAP = 0.10
TIMEOUT = 300

PROMPT = """Read the image at {path}

It is a weekly menu from a Bulgarian children's kitchen. Extract every day you
can see, and return ONLY valid JSON - no prose, no explanation:

{{"days":[{{"date":"DD.MM","items":[{{"position":1,"name":"...",
"ingredients":["..."],"bold_ingredients":["..."]}}]}}]}}

Rules:
- "date" is the day and month printed down the left edge, format "DD.MM".
- "position" is the number printed before the dish (1, 2 or 3).
- "ingredients" is the small-print list under the dish name, split per item.
- "bold_ingredients" contains ONLY the ingredients printed in BOLD type - these
  mark allergens. Empty list if none are bold.
- Transcribe the Bulgarian exactly as printed. Do not translate or rephrase.
- Do not invent days or ingredients. Skip any day clipped at the image edge.
"""


# Both CLIs work. claude -p is the default: on this workload it was ~3.5x faster
# and returned exactly the tile it was given, whereas codex explored the working
# directory and transcribed a different image that happened to be there - hence
# the one-tile-per-directory isolation below.
BACKEND = os.environ.get("KITCHEN_OCR_BACKEND", "claude").lower()


def _argv(backend: str, path: Path) -> tuple[list[str], str | None]:
    """Returns (argv, stdin_text)."""
    if backend == "codex":
        return (["codex", "exec", "--skip-git-repo-check", "-i", str(path), "-"],
                PROMPT.format(path=path))
    return (["claude", "-p", PROMPT.format(path=path), "--allowedTools", "Read"], None)


def available(backend: str | None = None) -> bool:
    return shutil.which("codex" if (backend or BACKEND) == "codex" else "claude") is not None


def _tiles(data: bytes) -> list[bytes]:
    im = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = im.size
    if max(w, h) <= MAX_EDGE:
        return [data]
    n = -(-h // int(MAX_EDGE * (1 - OVERLAP)))       # ceil
    step = h / n
    out: list[bytes] = []
    for i in range(n):
        top = max(0, int(i * step - step * OVERLAP))
        bottom = min(h, int((i + 1) * step + step * OVERLAP))
        buf = io.BytesIO()
        im.crop((0, top, w, bottom)).save(buf, format="PNG", optimize=True)
        out.append(buf.getvalue())
    return out


def _parse_json(text: str) -> dict:
    """Pull the menu object out of a CLI transcript.

    Cannot just slice first-'{' to last-'}': codex echoes the JSON twice (stream
    then final message) and both CLIs may wrap it in prose or fences. So scan for
    balanced top-level objects and keep the richest one that actually has days.
    """
    text = re.sub(r"```(?:json)?", "", text)
    best: dict | None = None
    for start in (m.start() for m in re.finditer(r"\{", text)):
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict) and obj.get("days"):
                        if best is None or len(obj["days"]) > len(best["days"]):
                            best = obj
                    break
    if best is None:
        raise ValueError(f"no menu JSON in reply: {text[:200]!r}")
    return best


async def _read_tile(path: Path, backend: str) -> dict:
    argv, stdin_text = _argv(backend, path)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin_text else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(path.parent),          # its own directory: nothing else is visible
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin_text.encode() if stdin_text else None), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{backend} timed out after {TIMEOUT}s") from None
    if proc.returncode != 0:
        # Both CLIs report auth/quota failures on stdout and exit non-zero with an
        # empty stderr, so a stderr-only message says nothing. Prefer whichever
        # stream actually spoke.
        detail = (err.decode().strip() or out.decode().strip() or "no output")
        raise RuntimeError(f"{backend} failed ({proc.returncode}): {detail[:300]}")
    return _parse_json(out.decode())


async def read_menu_image(data: bytes, backend: str | None = None) -> list[dict]:
    """Returns raw day dicts as described in PROMPT. Raises on failure."""
    backend = (backend or BACKEND).lower()
    by_date: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="kitchen-ocr-") as tmp:
        paths: list[Path] = []
        for i, tile in enumerate(_tiles(data)):
            # One tile per directory: these CLIs are agents, not OCR endpoints, and
            # will happily read a neighbouring file instead of the one requested.
            d = Path(tmp) / f"tile-{i}"
            d.mkdir()
            p = d / "menu.png"
            p.write_bytes(tile)
            paths.append(p)
        # Tiles are independent; read them concurrently.
        results = await asyncio.gather(*(_read_tile(p, backend) for p in paths),
                                       return_exceptions=True)

    failures = [r for r in results if isinstance(r, BaseException)]
    if failures and len(failures) == len(results):
        raise RuntimeError(str(failures[0]))

    for res in results:
        if isinstance(res, BaseException):
            continue
        for day in res.get("days", []):
            date = str(day.get("date", "")).strip()
            if not date:
                continue
            # Tiles overlap, so a day can appear twice - keep the fuller copy,
            # i.e. the one that was not clipped by a tile edge.
            prev = by_date.get(date)
            if prev is None or len(day.get("items", [])) > len(prev.get("items", [])):
                by_date[date] = day
    return list(by_date.values())
