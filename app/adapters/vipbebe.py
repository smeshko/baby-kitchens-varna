"""ВИП Бебе - меню за деца алергични към глутен (menu.php?id=7).

The menu exists only as a PNG, so this is the one adapter that needs OCR. The
image is regenerated weekly and served with a stable ETag, so the vision call
runs about once a week and its result is cached against that ETag.

Coverage limit: the site publishes the current week plus the *previous* one
(verified - /prev/ held 03-09.08 while the live image held 10-16.08). There is
never a future week here, so this column is legitimately empty for "next week".
"""
from __future__ import annotations

import datetime as dt
import re

import httpx

from .. import allergens as alg
from .. import cache, ocr
from ..models import DayMenu, MenuItem
from .base import Adapter

PAGE = "https://vipbebe.com/menu.php?id=7"
BASE = "https://vipbebe.com/"

_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})")


class VipBebeAdapter(Adapter):
    id = "vipbebe"
    label = "ВИП Бебе — без глутен"
    coverage_note = "Публикува само текущата седмица"

    async def _image_url(self, http: httpx.AsyncClient) -> str:
        r = await http.get(PAGE)
        r.raise_for_status()
        srcs = re.findall(r'src="(menu_site/[A-Za-z0-9_/-]+\.png)"', r.text)
        current = [s for s in srcs if "/prev/" not in s]
        if not current:
            raise RuntimeError("no current menu image on the page")
        return BASE + current[0]

    async def probe(self, http: httpx.AsyncClient) -> str:
        url = await self._image_url(http)
        r = await http.head(url)
        r.raise_for_status()
        tag = r.headers.get("etag") or r.headers.get("last-modified") or ""
        return re.sub(r'[^A-Za-z0-9]', "", tag)[:32] or url

    async def load(self, http: httpx.AsyncClient, start: dt.date, end: dt.date) -> list[DayMenu]:
        url = await self._image_url(http)
        key = await self.probe(http)

        cached = cache.read(f"vipbebe-ocr-{key}")
        if cached is not None:
            raw_days = cached["payload"]["days"]
        else:
            if not ocr.available():
                # Degrade, never fail: the PNG itself is perfectly readable, so the
                # UI shows it inline and the text/allergen comparison sits this one out.
                return [DayMenu(
                    kitchen="vipbebe", date=start, source_url=PAGE, image_url=url,
                    note=f"Няма {ocr.BACKEND} CLI — менюто се показва като изображение",
                )]
            blob = await http.get(url)
            blob.raise_for_status()
            raw_days = await ocr.read_menu_image(blob.content)
            cache.write(f"vipbebe-ocr-{key}", key, {"days": raw_days})

        today = dt.date.today()
        days: list[DayMenu] = []
        for raw in raw_days:
            m = _DATE.search(str(raw.get("date", "")))
            if not m:
                continue
            day, month = int(m.group(1)), int(m.group(2))
            year = today.year + (1 if month < today.month - 6 else 0)
            try:
                date = dt.date(year, month, day)
            except ValueError:
                continue
            if not (start <= date <= end):
                continue

            items: list[MenuItem] = []
            for it in raw.get("items", []):
                ingredients = [str(x).strip() for x in it.get("ingredients", []) if str(x).strip()]
                marked = [str(x).strip() for x in it.get("bold_ingredients", []) if str(x).strip()]
                tokens, printed = alg.classify_many(marked)
                extra, _ = alg.classify_many(ingredients)
                items.append(MenuItem(
                    position=it.get("position"),
                    name=str(it.get("name", "")).strip(),
                    ingredients=ingredients,
                    allergens=sorted(set(tokens) | set(extra)),
                    allergen_source_terms=printed,
                    portion="200 гр.",
                ))
            days.append(DayMenu(
                kitchen="vipbebe", date=date, items=[i for i in items if i.name],
                source_url=PAGE, image_url=url,
            ))
        return sorted(days, key=lambda d: d.date)
