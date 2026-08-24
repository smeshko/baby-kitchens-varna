# Детски кухни — Варна

Compares the daily menus of three Varna children's kitchens side by side, with
allergens normalised across all three.

Open <http://127.0.0.1:8787>.

| Kitchen | Menu | Source format |
|---|---|---|
| Фуристо | БЕЗ — без млечни и глутен, 200 гр. | Odoo shop HTML, one product per day |
| ВИП Бебе | без глутен (`menu.php?id=7`) | a single PNG per week → OCR |
| ОПКДХ Варна | II група (1–3 г.), обедно хранене | weekly PDF with a real text layer |

## Layout

Each day is a banded section; inside it the three meal slots (супа / основно /
десерт) are **aligned rows across all three kitchens**, keyed on the dish's
`position`, so meal 1 of every kitchen sits on one line and can be compared at a
glance. Full ingredients show under every dish, with the source-marked allergen
terms bolded and underlined inline.

View state is deep-linkable: `?week=1`, `?compact=1`, `?watch=GLUTEN,MILK`,
`?theme=light|dark`.

## Running

Needs [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/). OCR
additionally needs the `claude` CLI on PATH (or `codex`, see below).

```sh
just run                # foreground, with reload
just install-service    # run in the background from login (launchd)
just status             # is it up?
just logs               # tail the log
just check              # fetch every kitchen, print a summary
just --list             # everything else
```

`just install-service` renders `com.ivo.kitchen.plist.template` for this machine
and loads it — the generated plist is gitignored, since it embeds absolute paths.
Config knobs live in that template: `KITCHEN_PORT`, `KITCHEN_OCR_BACKEND`,
`KITCHEN_WARM_SECONDS`, `KITCHEN_RELOAD`.

The background service hot-reloads: it watches `app/` and restarts in place on
save, so edits go live without `just restart`. The watch is scoped to `app/`
because the default root is the whole project, `.venv` included. A reload drops
the in-memory cache and re-warms (~20s of upstream probes), so set
`KITCHEN_RELOAD=0` in the plist to pin the process instead. `just restart` is
still needed for changes outside `app/` — `run.sh`, the plist, dependencies.

On this machine the service is started by launchd, not by
`~/Developer/start-services.sh`; that script only bootstraps the agent if it is
not loaded. The log is shared with the other local services at
`~/Developer/logs/kitchen.log`.

## How it works

Each kitchen is an `Adapter` (`app/adapters/`) with two phases:

- `probe()` — cheap fingerprint of upstream state (ETag, PDF URL set, listing hash)
- `load()` — the expensive full parse, run **only** when the fingerprint moves

That split matters because the sources differ by an order of magnitude in cost:
OPKDH is 3 requests, Furisto is 1 + 20, VIP Bebe is a vision call. A single
shared TTL would either overspend on OCR or serve stale Furisto data.

`GET /api/menus` serves the disk cache immediately and revalidates behind the
response — even the "nothing changed" path costs ~7s of probes, too slow to sit
in front of a page load. A background loop re-warms every 30 min. If a source
fails, its last good data is served marked `stale`; a source that has never
loaded renders as a greyed column instead of breaking the page.

### Allergens

Tracked: `MILK`, `GLUTEN`, `EGG`, `FISH`, `SUGAR`, plus the rarer EU-14 entries.
Oats, soy and celery are deliberately **not** tracked — they showed up constantly
in these menus without being relevant here. `SUGAR` is not an allergen at all; it
is tracked because added sugar is worth seeing at a glance, and since no source
marks it, it comes entirely from the secondary ingredient sweep.

All three mark allergens, each in its own way, so detection is per-adapter and
only the *normalisation* is shared (`app/allergens.py`):

- **Фуристо** — ALL CAPS in the ingredient list (`ЦЕЛИНА`, `ЯЙЦА`)
- **ВИП Бебе** — bold, which the OCR prompt is told to preserve
- **ОПКДХ** — italic *and* underlined; the PDF legend says so outright, and the
  two markings agree on 451/452 characters, so italics are read via font name
  and the underline rects act as a cross-check

A secondary lexicon sweep runs over unmarked ingredients to catch marking
mistakes (it already found one: `Крем ванилия` has un-italicised `прясно мляко`).

The lexicon carries the traps these menus actually contain — `кокосово мляко`
and `соево мляко` are not dairy, `маслиново масло` is not butter, `царевичен
грис` and `картофено брашно` are not gluten, `елда` is not gluten, and
`овесени ядки` is not tree nuts.

### OCR

Shells out to the local `claude -p` CLI, so it bills against the Claude
subscription — no API key to store. Set `KITCHEN_OCR_BACKEND=codex` to use
`codex exec` instead.

The image is split into overlapping tiles first: at 1414×2000 the long edge
would be downscaled to 1568px and the ingredient print is small. **Each tile is
written to its own directory** — these CLIs are agents, not OCR endpoints, and
codex was observed reading a *different* image that happened to be in the
working directory rather than the tile it was asked for.

OCR results are cached against the image's ETag, so this runs about once a week.

## Coverage

- **ВИП Бебе publishes only the current week** — verified: its second image is
  the *previous* week, not the next one. Its "next week" column is empty by
  design, not by failure.
- Фуристо runs ~3 weeks ahead; ОПКДХ publishes two weeks at a time.

## Known fragilities

1. OPKDH filenames are hand-typed and already inconsistent
   (`10.08.-16.08.2026Г.` vs `17.08.- 23.08. 2026г.`), and the page still links
   a dead 2025 PDF. Date parsing is deliberately lenient and filters by window.
2. VIP Bebe depends on OCR; the PNG is always linked in the UI as a fallback.
3. Furisto depends on Odoo class names (`o_wsale_products_item_title`, `.oe_price`).
