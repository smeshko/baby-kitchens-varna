"""Normalized menu model shared by every kitchen adapter."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

KitchenId = Literal["furisto", "vipbebe", "opkdh"]

BG_WEEKDAYS = ["Понеделник", "Вторник", "Сряда", "Четвъртък", "Петък", "Събота", "Неделя"]


class MenuItem(BaseModel):
    position: int | None = None          # 1 = soup, 2 = main, 3 = dessert
    name: str
    ingredients: list[str] = Field(default_factory=list)
    allergens: list[str] = Field(default_factory=list)   # canonical tokens, see allergens.py
    allergen_source_terms: list[str] = Field(default_factory=list)  # as printed, for display
    portion: str | None = None


class DayMenu(BaseModel):
    kitchen: KitchenId
    date: dt.date
    weekday: str = ""
    items: list[MenuItem] = Field(default_factory=list)
    note: str | None = None              # e.g. "ПОЧИВЕН ДЕН"
    source_url: str | None = None
    image_url: str | None = None         # VIP Bebe: the raw PNG, always shown as fallback

    def model_post_init(self, _ctx) -> None:
        if not self.weekday:
            self.weekday = BG_WEEKDAYS[self.date.weekday()]


class SourceResult(BaseModel):
    """One adapter's outcome. Failure is data, not an exception — a dead source
    must render as a greyed card, never break the page."""
    kitchen: KitchenId
    label: str
    ok: bool
    days: list[DayMenu] = Field(default_factory=list)
    error: str | None = None
    fetched_at: dt.datetime | None = None
    stale: bool = False                  # served from cache after a failed refresh
    change_key: str | None = None
    coverage_note: str | None = None     # e.g. VIP Bebe publishes current week only


class MenuResponse(BaseModel):
    generated_at: dt.datetime
    week_start: dt.date
    week_end: dt.date
    sources: list[SourceResult]
