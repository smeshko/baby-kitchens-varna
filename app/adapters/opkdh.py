"""ОПКДХ Варна - II група (1-3 години), обедно хранене.

Weekly PDFs with a real text layer, but a table layout the kitchen rewrites
without warning. It shipped one row per dish (Ден | Ястия | Количество | Състав)
with allergens in italics until 2026-08-17, then one row per *day* with
супа/основно/десерт as columns, no italics at all, allergens in ALL CAPS plus an
explicit "Алергени:" line. The structural parser read the new file as zero days
and cached that silently.

So the layout is not parsed here any more: the page is serialized to delimited
text and handed to the LLM (`app/ocr.py`), which is told about all three marking
conventions and returns the same day/item JSON the other sources produce. What
stays structural is cheap and independently checkable: which PDFs are linked and
which week each filename covers.

Cost is bounded by content hash - a given PDF is sent once, ever, and the result
is cached, so the weekly refresh is normally zero calls.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
import urllib.parse

import httpx
import pdfplumber

from .. import allergens as alg
from .. import cache, ocr
from ..models import DayMenu, MenuItem
from .base import Adapter

PAGE = ("https://opkdhvarna.com/новина/седмично-меню-ii-група--от-1-3-години-обедно-хранене")

# Filenames are hand-typed and inconsistent: "10.08.-16.08.2026Г." vs
# "17.08.- 23.08. 2026г." vs "24.08-30.08.2026_Меню_ДК_2гр_1.pdf" - so the
# separator and spacing must both be lenient.
_RANGE = re.compile(
    r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*-\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*(\d{2,4})")

# A day cell carries a full date in the new layout ("24.8.2026"); the older one
# numbered days only by position, so the year may have to come from the filename.
_CELL_DATE = re.compile(r"(\d{1,2})\s*\.\s*(\d{1,2})(?:\s*\.\s*(\d{2,4}))?")


def _week_start(filename: str) -> dt.date | None:
    m = _RANGE.search(filename)
    if not m:
        return None
    d, mo, _, _, y = m.groups()
    year = int(y) if len(y) == 4 else 2000 + int(y)
    try:
        return dt.date(year, int(mo), int(d))
    except ValueError:
        return None


def _cell_runs(page, bbox) -> list[tuple[str, bool]]:
    """Text runs inside a cell bbox, each flagged italic (= allergen marking)."""
    x0, top, x1, bottom = bbox
    chars = [c for c in page.chars
             if x0 - 0.5 <= c["x0"] and c["x1"] <= x1 + 0.5
             and top - 0.5 <= c["top"] and c["bottom"] <= bottom + 0.5]
    chars.sort(key=lambda c: (round(c["top"], 1), c["x0"]))
    runs: list[list] = []
    for c in chars:
        italic = "Italic" in c["fontname"]
        if runs and runs[-1][1] == italic:
            prev = runs[-1]
            # New visual line inside the same cell -> join with a space.
            gap = c["x0"] - prev[2]
            newline = c["top"] - prev[3] > 3
            if newline and not prev[0].endswith(" "):
                prev[0] += " "
            elif gap > 1.2 and not prev[0].endswith(" "):
                prev[0] += " "
            prev[0] += c["text"]
            prev[2], prev[3] = c["x1"], c["top"]
        else:
            runs.append([c["text"], italic, c["x1"], c["top"]])
    return [(re.sub(r"\s+", " ", r[0]).strip(), r[1]) for r in runs if r[0].strip()]


def pdf_text(data: bytes) -> str:
    """Serialize page 1 as "cell | cell" rows, keeping any italic marking.

    Italics were the allergen marking until the August 2026 rewrite dropped them
    for ALL CAPS and an "Алергени:" line. Wrapping italic runs in asterisks costs
    nothing when there are none, and means one prompt covers both files.

    Cell delimiters rather than pdfplumber's layout text: with three menu columns
    the layout mode interleaves them into unreadable lines.
    """
    rows: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        page = pdf.pages[0]
        for table in page.find_tables():
            for row in table.rows:
                cells = []
                for bbox in row.cells:
                    # A vertically merged cell is None on its continuation rows.
                    runs = _cell_runs(page, bbox) if bbox else []
                    cells.append(" ".join(f"*{t}*" if ital else t for t, ital in runs))
                if any(c.strip() for c in cells):
                    rows.append(" | ".join(cells))
        if not rows:
            # No table at all is not automatically fatal - the kitchen could move
            # to a plain-text layout - but an empty text layer means a scan, and
            # this adapter has no vision path.
            text = page.extract_text() or ""
            if not text.strip():
                raise ValueError("PDF page 1 has no table and no text layer")
            rows.append(text)
    return "\n".join(rows)


def _parse_date(text: str, week_start: dt.date) -> dt.date | None:
    m = _CELL_DATE.search(text)
    if not m:
        return None
    d, mo, y = m.groups()
    year = week_start.year if y is None else (int(y) if len(y) == 4 else 2000 + int(y))
    try:
        return dt.date(year, int(mo), int(d))
    except ValueError:
        return None


def _portion(value) -> str | None:
    """"Грамаж:150" reaches us as "150"; the other kitchens print a unit."""
    text = str(value or "").strip()
    if not text:
        return None
    return f"{text} гр." if text.isdigit() else text


def to_days(raw_days: list[dict], week_start: dt.date, url: str) -> list[DayMenu]:
    days: list[DayMenu] = []
    for raw in raw_days:
        date = _parse_date(str(raw.get("date", "")), week_start)
        if date is None:
            continue
        items: list[MenuItem] = []
        for it in raw.get("items", []):
            ingredients = [str(x).strip() for x in it.get("ingredients", []) if str(x).strip()]
            marked = [str(x).strip() for x in it.get("marked_ingredients", []) if str(x).strip()]
            tokens, printed = alg.classify_many(marked)
            # Secondary sweep: catch anything the source forgot to mark.
            extra, _ = alg.classify_many(ingredients)
            position = it.get("position")
            items.append(MenuItem(
                position=int(position) if str(position).isdigit() else None,
                name=re.sub(r"\s+", " ", str(it.get("name", ""))).strip(),
                ingredients=ingredients,
                allergens=sorted(set(tokens) | set(extra)),
                allergen_source_terms=printed,
                portion=_portion(it.get("portion")),
            ))
        days.append(DayMenu(
            kitchen="opkdh",
            date=date,
            items=[i for i in items if i.name],
            note=str(raw.get("note") or "").strip() or None,
            source_url=url,
        ))
    return days


class OpkdhAdapter(Adapter):
    id = "opkdh"
    label = "ОПКДХ Варна — II група (1–3 г.)"

    async def _pdf_links(self, http: httpx.AsyncClient) -> list[tuple[dt.date, str]]:
        r = await http.get(PAGE)
        r.raise_for_status()
        out: list[tuple[dt.date, str]] = []
        hrefs = re.findall(r'href="([^"]*\.pdf[^"]*)"', r.text, re.I)
        for href in hrefs:
            url = href if href.startswith("http") else urllib.parse.urljoin(PAGE, href)
            name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
            ws = _week_start(name)
            if ws:
                out.append((ws, url))
        # Same reasoning as Furisto: a page with no parseable PDF links is a
        # failure, not an empty menu. Silently caching [] hides the source.
        if not out:
            raise RuntimeError(
                f"no dated menu PDFs found ({len(hrefs)} pdf links in {len(r.text)} bytes)")
        return sorted(set(out))

    async def probe(self, http: httpx.AsyncClient) -> str:
        links = await self._pdf_links(http)
        return "|".join(f"{d.isoformat()}:{u.rsplit('/', 1)[-1]}" for d, u in links)

    async def load(self, http: httpx.AsyncClient, start: dt.date, end: dt.date) -> list[DayMenu]:
        days: list[DayMenu] = []
        for ws, url in await self._pdf_links(http):
            if ws + dt.timedelta(days=6) < start or ws > end:
                continue          # drops the stale 2025 PDF still linked on the page
            r = await http.get(url)
            r.raise_for_status()

            # Keyed on content, not filename: the kitchen re-uploads under new
            # names, and a re-upload of identical bytes must not pay for a call.
            key = hashlib.sha1(r.content).hexdigest()[:16]
            cached = cache.read(f"opkdh-parse-{key}")
            if cached is not None:
                raw_days = cached["payload"]["days"]
            else:
                if not ocr.available():
                    # Degrade, never fail: the PDF is linked in the UI and stays
                    # readable by a human.
                    days.append(DayMenu(
                        kitchen="opkdh", date=max(ws, start), source_url=url,
                        note=f"Няма {ocr.BACKEND} CLI — менюто се чете само от PDF",
                    ))
                    continue
                raw_days = await ocr.read_menu_text(pdf_text(r.content), ws)
                cache.write(f"opkdh-parse-{key}", key, {"days": raw_days})

            days.extend(to_days(raw_days, ws, url))
        return sorted((d for d in days if start <= d.date <= end), key=lambda d: d.date)
