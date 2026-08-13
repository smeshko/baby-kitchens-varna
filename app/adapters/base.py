"""The contract every kitchen implements.

Two phases on purpose: probe() is cheap and runs on every refresh, load() is
expensive and runs only when the change key moves. The three kitchens differ by
an order of magnitude in cost (OPKDH ~3 requests, Furisto ~21, VIP Bebe an LLM
vision call), so a single shared TTL would either overspend or serve stale data.
"""
from __future__ import annotations

import datetime as dt

import httpx

from ..models import DayMenu, KitchenId

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "bg,en;q=0.8"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


class Adapter:
    id: KitchenId
    label: str
    coverage_note: str | None = None

    async def probe(self, http: httpx.AsyncClient) -> str:
        """Cheap fingerprint of the upstream state. Changing it triggers load()."""
        raise NotImplementedError

    async def load(self, http: httpx.AsyncClient, start: dt.date, end: dt.date) -> list[DayMenu]:
        """Expensive full parse, restricted to [start, end]."""
        raise NotImplementedError
