"""Maps Bulgarian allergen wording from three kitchens onto one vocabulary.

Every source marks allergens explicitly in its own way:
  * Furisto  - ALL CAPS inside the ingredient list
  * VIP Bebe - bold (preserved by the vision OCR prompt)
  * OPKDH    - italic + underline (redundant; the PDF legend states
               "включените в ястията възможни алергени са подчертани")

So detection is done by each adapter from its own markup; this module only
normalizes the marked term to a canonical token. It is also used as a
*secondary* sweep over unmarked ingredients to catch marking mistakes.
"""
from __future__ import annotations

import re
import unicodedata

# canonical token -> (Bulgarian label, short English label)
LABELS: dict[str, tuple[str, str]] = {
    "MILK": ("Мляко", "milk"),
    "EGG": ("Яйца", "egg"),
    "GLUTEN": ("Глутен", "gluten"),
    "SUGAR": ("Захар", "sugar"),
    "FISH": ("Риба", "fish"),
    "NUTS": ("Ядки", "nuts"),
    "PEANUT": ("Фъстъци", "peanut"),
    "SESAME": ("Сусам", "sesame"),
    "MUSTARD": ("Синап", "mustard"),
    "SULPHITES": ("Сулфити", "sulphites"),
    "CRUSTACEAN": ("Ракообразни", "crustacean"),
    "MOLLUSC": ("Мекотели", "mollusc"),
    "LUPIN": ("Лупина", "lupin"),
}

# Non-dairy "milks" and non-dairy "butters"/oils that must never read as MILK.
_PLANT_MILK = r"(?:кокосово|соево|бадемово|оризово|овесено|лешниково|кашуово)"
_OILS = r"(?:маслиново|слънчогледово|рапично|царевично|сусамово|тиквено|ленено)"
# Flours that contain no gluten - "брашно" alone must not imply wheat.
_GF_FLOUR = r"(?:картофено|оризово|царевично|нахутено|бадемово|кокосово|елдово|просено|соево|бяло\s+просо)"

# Ordered: the first pattern that matches wins for its token.
_PATTERNS: list[tuple[str, str]] = [
    # --- explicitly gluten-free / dairy-free forms, matched first as guards ---
    ("_SKIP", rf"безглутен\w*"),
    ("_SKIP", rf"без\s+глутен"),
    ("_SKIP", rf"без\s+млечни"),

    # Plant milks and vegetable oils are stripped from the probe before this runs.
    ("MILK", r"\bмляк\w*|\bмлечн\w*|краве\s+масло|сирене|кашкавал|извара|сметана|йогурт"),
    ("EGG", r"\bяйц\w*|\bяйе?ца\b"),
    # Gluten-free flours are stripped from the probe before this runs.
    # NB: елда (buckwheat) is gluten-free and OPKDH does not mark it - do not add it.
    ("GLUTEN", r"пшенич\w*|пшеница|\bбрашно\b|галета|бишкот\w*|"
               r"макарон\w*|грис\b|\bръж\w*|\bечемик\w*|\bспелта\b|кус-кус|булгур"),
    # Not an allergen - tracked because added sugar is worth seeing at a glance.
    # No source marks it, so this always comes from the secondary ingredient sweep.
    ("SUGAR", r"\bзахар\w*|\bмед\b|глюкоз\w*|фруктоз\w*"),
    ("FISH", r"\bриба\b|\bрибн\w*|сьомга|есетра|\bтон\b|скумрия|пъстърва|хек\b"),
    ("CRUSTACEAN", r"скарид\w*|рак\w*|омар\w*"),
    ("MOLLUSC", r"миди|калмар\w*|октопод\w*"),
    ("PEANUT", r"фъстъ\w*"),
    ("NUTS", r"\bядки\b|орех\w*|бадем\w*|лешник\w*|шам-фъстък|кашу|кестен\w*"),
    ("SESAME", r"сусам\w*|тахан"),
    ("MUSTARD", r"синап\w*|горчиц\w*"),
    ("SULPHITES", r"сулфит\w*"),
    ("LUPIN", r"лупин\w*"),
]

_COMPILED = [(tok, re.compile(pat, re.IGNORECASE)) for tok, pat in _PATTERNS]

# "ядки" appears inside "овесени ядки" (oats). Oats are not tracked as an allergen
# here, but the phrase must still be stripped or it reads as tree nuts.
_OATS_PHRASE = re.compile(r"овесени\s+ядки", re.IGNORECASE)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def classify(term: str) -> list[str]:
    """Canonical allergen tokens for one printed term (e.g. 'прясно мляко – 3,2%')."""
    t = _norm(term)
    if not t:
        return []

    # Guard clauses: an explicitly free-from term carries no allergen of that kind.
    gluten_free = bool(re.search(r"безглутен|без\s+глутен", t, re.I))
    dairy_free = bool(re.search(r"без\s+млечни|без\s+мляко", t, re.I))

    # Strip plant-milk phrases so they cannot leak into MILK.
    milk_probe = re.sub(rf"{_PLANT_MILK}\s+мляко", " ", t, flags=re.I)
    milk_probe = re.sub(rf"{_OILS}\s+масло", " ", milk_probe, flags=re.I)

    # Strip gluten-free flours and semolinas so bare "брашно"/"грис" cannot imply
    # wheat. "царевичен грис" (corn) is common in these gluten-free menus.
    gluten_probe = re.sub(rf"{_GF_FLOUR}\s+(?:брашно|грис)", " ", t, flags=re.I)
    gluten_probe = re.sub(r"(?:царевичен|оризов|елдов|просен)\s+грис", " ", gluten_probe, flags=re.I)
    gluten_probe = re.sub(r"грис\s+(?:царевичен|оризов)", " ", gluten_probe, flags=re.I)
    gluten_probe = re.sub(r"брашно\W*от\s+(?:ябълки|рожков|кокос\w*|бадем\w*)", " ", gluten_probe, flags=re.I)

    oats_only = bool(_OATS_PHRASE.search(t))

    found: list[str] = []
    for token, rx in _COMPILED:
        if token == "_SKIP":
            continue
        probe = {"MILK": milk_probe, "GLUTEN": gluten_probe}.get(token, t)
        if token == "NUTS" and oats_only:
            probe = _OATS_PHRASE.sub(" ", probe)
        if not rx.search(probe):
            continue
        if token == "GLUTEN" and gluten_free:
            continue
        if token == "MILK" and dairy_free:
            continue
        if token == "MILK" and not re.search(r"мляк|млечн|сирене|кашкавал|извара|сметана|йогурт|краве\s+масло",
                                             milk_probe, re.I):
            continue
        found.append(token)

    return sorted(set(found))


def classify_many(terms: list[str]) -> tuple[list[str], list[str]]:
    """Returns (canonical tokens, the printed terms that produced them)."""
    tokens: list[str] = []
    kept: list[str] = []
    for term in terms:
        hits = classify(term)
        if hits:
            tokens.extend(hits)
            kept.append(_norm(term))
    return sorted(set(tokens)), kept


def label(token: str) -> str:
    return LABELS.get(token, (token, token))[0]
