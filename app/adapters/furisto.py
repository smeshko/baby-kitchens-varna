"""Фуристо Варна - "БЕЗ" menu (без млечни и глутен), 200 гр.

Odoo shop. One product per day; the date lives in the product title ("14.08").
Dishes are only on the detail page, so a load costs 1 + N requests - run
concurrently, bounded, because the whole point of the two-phase adapter is that
this only happens when the listing actually changed.

Allergens are printed in ALL CAPS inside the ingredient list ("ЦЕЛИНА", "ЯЙЦА").
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import re

import httpx
from bs4 import BeautifulSoup

from .. import allergens as alg
from ..models import DayMenu, MenuItem
from .base import Adapter

LISTING = "https://furistovarna.com/bg/shop/category/furisto-bezz-200gr-8"
BASE = "https://furistovarna.com"

_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})")
_DISH_START = re.compile(r"^\s*([123])\s*[.．]\s*(.*)$")
# A run of 3+ Cyrillic capitals is the allergen marking.
_CAPS = re.compile(r"[А-ЯЁЬЪЮЯ]{3,}(?:[\s-]+[А-ЯЁЬЪЮЯ]{3,})*")
_CONCURRENCY = 6


def _resolve_year(day: int, month: int, today: dt.date) -> int:
    """Titles carry no year; assume the nearest upcoming occurrence."""
    year = today.year
    if month < today.month - 6:
        year += 1
    elif month > today.month + 6:
        year -= 1
    try:
        dt.date(year, month, day)
    except ValueError:
        return year
    return year


def _price(html: str) -> str | None:
    """The product's own price.

    The page also renders prev/next product teasers that contain
    .oe_currency_value, so anchoring on the first match picks a neighbouring
    product's price. The real one is the itemprop="price" / .oe_price pair.
    """
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("h3 .oe_price .oe_currency_value")
    if node is None:
        meta = soup.select_one('[itemprop="price"]')
        if meta is not None and meta.get("content"):
            return f"{meta['content']} €"
        return None
    return f"{re.sub(r'\s+', '', node.get_text())} €"


def parse_description(html: str) -> list[MenuItem]:
    """Description is 'N. Name<br>ingredients<br>...' with blank lines between dishes."""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find(id="product_full_description")
    if node is None:
        return []
    for br in node.find_all("br"):
        br.replace_with("\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in node.get_text().split("\n")]

    items: list[MenuItem] = []
    name: str | None = None
    pos = 0
    buf: list[str] = []

    def flush() -> None:
        nonlocal name, buf
        if name is None:
            return
        ingredient_text = " ".join(buf).strip(" .")
        ingredients = [p.strip(" .") for p in re.split(r"[,;]", ingredient_text) if p.strip(" .")]
        marked = [m.group(0) for m in _CAPS.finditer(ingredient_text)]
        tokens, printed = alg.classify_many(marked)
        extra, _ = alg.classify_many(ingredients)   # catch anything left un-capitalised
        items.append(MenuItem(
            position=pos or None,
            name=name,
            ingredients=ingredients,
            allergens=sorted(set(tokens) | set(extra)),
            allergen_source_terms=printed,
        ))
        name, buf = None, []

    for ln in lines:
        if not ln:
            continue
        m = _DISH_START.match(ln)
        if m:
            flush()
            pos = int(m.group(1))
            name = m.group(2).strip() or None
            continue
        if name is None:
            continue
        buf.append(ln)
    flush()
    return [i for i in items if i.name]


class FuristoAdapter(Adapter):
    id = "furisto"
    label = "Фуристо — без млечни и глутен (200 гр.)"

    async def _cards(self, http: httpx.AsyncClient) -> list[dict]:
        r = await http.get(LISTING)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        anchors = soup.select("h6.o_wsale_products_item_title a[href]")
        out: list[dict] = []
        for h in anchors:
            title = re.sub(r"\s+", " ", h.get_text()).strip()
            m = _DATE.search(title)
            if not m:
                continue
            href = h["href"].split("?")[0]
            out.append({
                "title": title,
                "url": BASE + href if href.startswith("/") else href,
                "day": int(m.group(1)),
                "month": int(m.group(2)),
                "extra": "Екстра" in title,
            })
        # An unparseable listing must fail loudly. Returning [] here reads as "the
        # shop has no menus", which the aggregator caches as a success - and since
        # the probe hash of an empty parse is itself stable, the empty result then
        # sticks forever. A bot challenge or markup change lands exactly here.
        if not out:
            raise RuntimeError(
                f"listing yielded no dated products ({len(anchors)} product anchors in "
                f"{len(r.text)} bytes) - markup changed, or the request was challenged")
        return out

    async def probe(self, http: httpx.AsyncClient) -> str:
        cards = await self._cards(http)
        blob = "|".join(sorted(c["url"] + c["title"] for c in cards))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    async def load(self, http: httpx.AsyncClient, start: dt.date, end: dt.date) -> list[DayMenu]:
        today = dt.date.today()
        cards = []
        for c in await self._cards(http):
            year = _resolve_year(c["day"], c["month"], today)
            try:
                c["date"] = dt.date(year, c["month"], c["day"])
            except ValueError:
                continue
            if start <= c["date"] <= end:
                cards.append(c)

        sem = asyncio.Semaphore(_CONCURRENCY)

        async def fetch(card: dict) -> dict:
            last: Exception | None = None
            for attempt in range(3):
                try:
                    async with sem:
                        r = await http.get(card["url"])
                        r.raise_for_status()
                    card["items"] = parse_description(r.text)
                    card["price"] = _price(r.text)
                    return card
                except Exception as exc:                       # noqa: BLE001
                    last = exc
                    await asyncio.sleep(0.4 * (attempt + 1))
            card["items"] = []
            card["price"] = None
            card["error"] = f"{type(last).__name__}: {last}"
            return card

        done = await asyncio.gather(*(fetch(c) for c in cards))
        by_date: dict[dt.date, DayMenu] = {}
        extras: dict[dt.date, dict] = {}

        for c in done:
            if c["extra"]:
                extras[c["date"]] = c
                continue
            day = by_date.setdefault(c["date"], DayMenu(
                kitchen="furisto", date=c["date"], source_url=c["url"]))
            day.items = c["items"]
            for it in day.items:
                it.portion = "200 гр."
            if c.get("error"):
                day.note = "Менюто не се зареди — виж сайта"
            elif c.get("price"):
                day.note = c["price"]

        # The "Екстра" variant is a separate, pricier menu for the same date. Kept as
        # a note so the day-by-day comparison stays one menu per kitchen per day.
        for date, c in extras.items():
            day = by_date.setdefault(date, DayMenu(
                kitchen="furisto", date=date, source_url=c["url"]))
            tag = f"Екстра: {c['price']}" if c.get("price") else "Има вариант Екстра"
            day.note = f"{day.note} · {tag}" if day.note else tag

        return sorted(by_date.values(), key=lambda d: d.date)
