"""Local menu-comparison service.

Serves a static page that fetches /api/menus. Runs a slow background warm-up so
that opening the page is instant rather than waiting on a cold fan-out.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import ocr
from .aggregator import collect, read_cached

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kitchen")

STATIC = Path(__file__).parent / "static"
WARM_INTERVAL = int(os.environ.get("KITCHEN_WARM_SECONDS", 1800))


async def _warm_loop() -> None:
    while True:
        try:
            res = await collect()
            log.info("warmed: %s", ", ".join(
                f"{s.kitchen}={len(s.days)}d{'(stale)' if s.stale else ''}" for s in res.sources))
        except Exception:                                    # noqa: BLE001
            log.exception("warm-up failed")
        await asyncio.sleep(WARM_INTERVAL)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_warm_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="Детски кухни — Варна", lifespan=lifespan)


_bg: set[asyncio.Task] = set()


def _revalidate(weeks: int) -> None:
    """Fire-and-forget refresh behind an already-served response."""
    if any(not t.done() for t in _bg):
        return
    task = asyncio.create_task(collect(weeks=weeks))
    _bg.add(task)
    task.add_done_callback(_bg.discard)


@app.get("/api/menus")
async def api_menus(weeks: int = Query(2, ge=1, le=4), force: bool = False) -> JSONResponse:
    if not force:
        cached = read_cached(weeks=weeks)
        if cached is not None:
            _revalidate(weeks)
            return JSONResponse(cached.model_dump(mode="json"))
    res = await collect(weeks=weeks, force=force)
    return JSONResponse(res.model_dump(mode="json"))


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "today": dt.date.today().isoformat(),
        "ocr_backend": ocr.BACKEND,
        "ocr_available": ocr.available(),
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
