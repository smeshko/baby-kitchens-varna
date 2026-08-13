"""ОПКДХ Варна - II група (1-3 години), обедно хранене.

Weekly PDFs with a real text layer. Allergens are marked italic *and*
underlined; the PDF legend says so outright ("включените в ястията възможни
алергени са подчертани"). The two markings agree on 451/452 characters, so we
read italics (cheap, via fontname) and keep the underline rects as a cross-check.
"""
from __future__ import annotations

import datetime as dt
import io
import re
import urllib.parse

import httpx
import pdfplumber

from .. import allergens as alg
from ..models import DayMenu, MenuItem
from .base import Adapter

PAGE = ("https://opkdhvarna.com/новина/седмично-меню-ii-група--от-1-3-години-обедно-хранене")

# Filenames are hand-typed and inconsistent: "10.08.-16.08.2026Г." vs
# "17.08.- 23.08. 2026г." - so the separator and spacing must both be lenient.
_RANGE = re.compile(
    r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*-\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*(\d{2,4})")

_DISH = re.compile(r"^\s*([123])\s*[.．]\s*(.+)$", re.S)
_RECIPE = re.compile(r"\s*[–-]\s*(?:рец\.?|р\.)\s*\d+\s*$")


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


def _split_terms(text: str) -> list[str]:
    # Bulgarian decimals use a comma ("мляко – 3,2%"), so shield them from the split.
    shielded = re.sub(r"(\d),(\d)", "\\1\x00\\2", text)
    parts = re.split(r"[,;]", shielded)
    return [p.replace("\x00", ",").strip(" .–-") for p in parts if p.strip(" .–-\x00")]


def parse_pdf(data: bytes, week_start: dt.date, url: str) -> list[DayMenu]:
    days: list[DayMenu] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        page = pdf.pages[0]
        tables = page.find_tables()
        if not tables:
            return []
        rows = tables[0].rows
        current: DayMenu | None = None
        for row in rows:
            cells = row.cells
            # The "Ден" cell is vertically merged across a day's three dishes, so
            # continuation rows have None there - that is normal, not a bad row.
            if len(cells) < 4 or cells[1] is None or cells[3] is None:
                continue
            day_txt = " ".join(t for t, _ in _cell_runs(page, cells[0])) if cells[0] else ""
            dish_runs = _cell_runs(page, cells[1])
            qty_txt = " ".join(t for t, _ in _cell_runs(page, cells[2]))
            comp_runs = _cell_runs(page, cells[3])

            dish_txt = " ".join(t for t, _ in dish_runs).strip()
            comp_txt = " ".join(t for t, _ in comp_runs).strip()

            if day_txt.strip() in {"Ден"} or dish_txt.startswith("Ястия"):
                continue

            # A single Cyrillic letter in column 0 opens a new day. П and С are
            # ambiguous (Пон/Пет, Сря/Съб) so days are assigned by order, not letter.
            letter = day_txt.strip()
            if len(letter) == 1 and letter in "ПВСЧН":
                idx = len(days)
                if idx > 6:
                    break
                current = DayMenu(
                    kitchen="opkdh",
                    date=week_start + dt.timedelta(days=idx),
                    source_url=url,
                )
                days.append(current)

            if current is None:
                continue

            blob = f"{dish_txt} {comp_txt}"
            if "ПОЧИВЕН" in blob.upper():
                current.note = "Почивен ден"
                continue

            m = _DISH.match(dish_txt)
            if not m:
                continue
            name = _RECIPE.sub("", m.group(2)).strip(" .–-")

            # Runs break wherever the italics stop, i.e. at the separators between
            # marked terms - so rejoin with a comma, not a space, or "краве масло"
            # and "яйца" fuse into one bogus term.
            marked = ", ".join(t for t, ital in comp_runs if ital)
            marked_terms = _split_terms(marked)
            tokens, printed = alg.classify_many(marked_terms)
            ingredients = _split_terms(comp_txt)
            # Secondary sweep: catch anything the source forgot to mark.
            extra, _ = alg.classify_many(ingredients)

            current.items.append(MenuItem(
                position=int(m.group(1)),
                name=re.sub(r"\s+", " ", name),
                ingredients=ingredients,
                allergens=sorted(set(tokens) | set(extra)),
                allergen_source_terms=printed,
                portion=qty_txt.strip() or None,
            ))
    return days


class OpkdhAdapter(Adapter):
    id = "opkdh"
    label = "ОПКДХ Варна — II група (1–3 г.)"

    async def _pdf_links(self, http: httpx.AsyncClient) -> list[tuple[dt.date, str]]:
        r = await http.get(PAGE)
        r.raise_for_status()
        out: list[tuple[dt.date, str]] = []
        for href in re.findall(r'href="([^"]*\.pdf[^"]*)"', r.text, re.I):
            url = href if href.startswith("http") else urllib.parse.urljoin(PAGE, href)
            name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
            ws = _week_start(name)
            if ws:
                out.append((ws, url))
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
            days.extend(parse_pdf(r.content, ws, url))
        return [d for d in days if start <= d.date <= end]
