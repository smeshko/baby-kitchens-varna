"""Fans out to the adapters, isolates their failures, merges by date."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from . import cache
from .adapters.base import Adapter, client
from .adapters.furisto import FuristoAdapter
from .adapters.opkdh import OpkdhAdapter
from .adapters.vipbebe import VipBebeAdapter
from .models import DayMenu, MenuResponse, SourceResult

log = logging.getLogger("kitchen")

ADAPTERS: list[Adapter] = [FuristoAdapter(), VipBebeAdapter(), OpkdhAdapter()]

_lock = asyncio.Lock()


def week_window(today: dt.date | None = None, weeks: int = 2) -> tuple[dt.date, dt.date]:
    """Monday of the current week through Sunday of the (weeks-1)th week ahead."""
    today = today or dt.date.today()
    start = today - dt.timedelta(days=today.weekday())
    return start, start + dt.timedelta(days=7 * weeks - 1)


def _restore(record: dict) -> tuple[list[DayMenu], str]:
    payload = record.get("payload", {})
    days = [DayMenu.model_validate(d) for d in payload.get("days", [])]
    return days, record.get("stored_at", "")


async def _refresh_one(ad: Adapter, start: dt.date, end: dt.date, force: bool) -> SourceResult:
    name = f"{ad.id}-{start.isoformat()}"
    record = cache.read(name)
    now = dt.datetime.now(dt.timezone.utc)

    async with client() as http:
        try:
            key = await ad.probe(http)
        except Exception as exc:                              # noqa: BLE001
            log.warning("%s probe failed: %s", ad.id, exc)
            key = None

        # Cheap path: upstream is unchanged, so skip the expensive load entirely.
        if not force and record is not None and key is not None and record.get("change_key") == key:
            days, stored = _restore(record)
            return SourceResult(
                kitchen=ad.id, label=ad.label, ok=True, days=days, change_key=key,
                fetched_at=dt.datetime.fromisoformat(stored) if stored else None,
                coverage_note=ad.coverage_note,
            )

        try:
            days = await ad.load(http, start, end)
            cache.write(name, key, {"days": [d.model_dump(mode="json") for d in days]})
            return SourceResult(
                kitchen=ad.id, label=ad.label, ok=True, days=days, change_key=key,
                fetched_at=now, coverage_note=ad.coverage_note,
            )
        except Exception as exc:                              # noqa: BLE001
            log.exception("%s load failed", ad.id)
            # Stale-if-error: last good data beats an empty column.
            if record is not None:
                days, stored = _restore(record)
                return SourceResult(
                    kitchen=ad.id, label=ad.label, ok=True, days=days, stale=True,
                    error=f"{type(exc).__name__}: {exc}",
                    fetched_at=dt.datetime.fromisoformat(stored) if stored else None,
                    coverage_note=ad.coverage_note,
                )
            return SourceResult(
                kitchen=ad.id, label=ad.label, ok=False,
                error=f"{type(exc).__name__}: {exc}", coverage_note=ad.coverage_note,
            )


def read_cached(weeks: int = 2) -> MenuResponse | None:
    """Whatever is already on disk, with no network at all.

    Even the 'nothing changed' path costs a probe per source (~7s, dominated by
    Furisto's 262KB listing page), which is too slow to sit in front of a page
    load. So the page is served from cache and revalidated behind the request.
    """
    start, end = week_window(weeks=weeks)
    sources: list[SourceResult] = []
    for ad in ADAPTERS:
        record = cache.read(f"{ad.id}-{start.isoformat()}")
        if record is None:
            return None
        days, stored = _restore(record)
        sources.append(SourceResult(
            kitchen=ad.id, label=ad.label, ok=True, days=days,
            change_key=record.get("change_key"),
            fetched_at=dt.datetime.fromisoformat(stored) if stored else None,
            coverage_note=ad.coverage_note,
        ))
    return MenuResponse(
        generated_at=dt.datetime.now(dt.timezone.utc),
        week_start=start, week_end=end, sources=sources,
    )


async def collect(weeks: int = 2, force: bool = False) -> MenuResponse:
    start, end = week_window(weeks=weeks)
    async with _lock:                      # one refresh at a time; this is a single-user app
        results = await asyncio.gather(*(_refresh_one(a, start, end, force) for a in ADAPTERS))
    return MenuResponse(
        generated_at=dt.datetime.now(dt.timezone.utc),
        week_start=start, week_end=end,
        sources=list(results),
    )
