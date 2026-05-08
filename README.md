# onlybirds

Find birds you haven't seen yet at nearby eBird hotspots, with rare-bird flags and Wikipedia enrichment, served up in a Streamlit + Folium dashboard.

## Ebird Api Key
visit https://ebird.org/api/keygen and request a key

## How it works

```
CSV (life list) ──┐
                  ▼
     ingest → resolve to eBird species codes
                  │
   eBird hotspots within radius ──► hotspot_obs
                  │
   sample historical obs per county ──► species_seasonality
                  │
              targets = hotspot_obs − life list
                  │
       eBird notable obs (last 7 days) ──► flag rare
                  │
        Wikipedia summary + image ──► enrich
                  │
                  ▼
        SQLite ──► Streamlit + Folium dashboard
```

Each pipeline stage writes to SQLite (`data/onlybirds.db`), so the dashboard always reads from the cache and stages are independently re-runnable.

## Setup

On a fresh machine:

```bash
brew bundle                     # installs `just` and `uv`
just install                    # uv sync
cp .env.example .env            # then fill in your values
```

Minimum `.env`:

```
EBIRD_API_KEY=…                 # https://ebird.org/api/keygen
ONLYBIRDS_LAT=33.88
ONLYBIRDS_LON=-118.40
ONLYBIRDS_CONTACT=you@example.com   # or a project URL — used in the Wikipedia User-Agent
```

`ONLYBIRDS_CONTACT` is optional but recommended — Wikimedia throttles requests with vague User-Agents, and you'll see Wikipedia fetches start failing during enrichment without it. The CLI auto-loads `.env` from the cwd, so no `export` needed.

## Run the pipeline

```bash
just latest                                    # newest CSV + lat/lon from .env
just latest 33.88 -118.40                      # override lat/lon
just run life-lists/05-07-2026.csv             # explicit CSV, lat/lon from .env
just run life-lists/05-07-2026.csv 33.88 -118.40
```

Underlying CLI (if you want extra flags like `--rare-radius`, `--days-back`, `--force-refresh`):

```bash
uv run onlybirds run --csv … --lat … --lon … --radius 25 --rare-days 7
```

## Launch the dashboard

```bash
just serve              # default port 8501
just serve 8600         # custom port
```

**Map tab.** Hotspots are color- and size-coded so the best ones stand out at a glance:

- 🔴 red — at least one rare-bird alert (prioritize these)
- 🔵 dark blue — 5+ targets
- 🔵 mid blue — 2–4 targets
- 🔵 light blue — 1 target
- ⚪ grey — no targets

Circle size scales with target count; cluster bubbles use the same scheme. A floating legend in the bottom-left explains it. Above the map, a "Top hotspots" ribbon lists the highest-scoring spots (rare-weighted) as clickable chips. Hover for a quick tooltip; click for the full species list (each species shows when it was last seen — `today`, `2d ago`, `Apr 17`); click the popup title to open a per-hotspot detail page in a new tab.

**Target-list tab.** Scrollable feed with photos, Wikipedia blurbs, in-season badges (✅ / ⚠️ off-season with a Jan–Dec strip), and a "rare alerts only" toggle.

## CSV format

Standard eBird life-list export works out of the box (`Common Name`, `Scientific Name`, `Date`, `Location`). Custom CSVs are accepted as long as they have a species column (any of: `species`, `common_name`, `bird`) and a date column. Lat/lon and location are optional.

## Layout

```
src/onlybirds/
  cli.py            # `onlybirds run` / `serve` / `status`
  db.py             # SQLite schema + connection helpers
  ebird.py          # eBird API 2.0 client
  taxonomy.py       # taxonomy cache + name → species_code resolver
  ingest.py         # CSV → observations
  hotspots.py       # nearby hotspots + recent obs (TTL 6h)
  seasonality.py    # sample historical obs per county (TTL 30d)
  targets.py        # set diff: hotspot species not in life list
  rare.py           # cross-check against /recent/notable
  enrich.py         # Wikipedia summary + image (TTL 30d)
dashboard/
  app.py            # Streamlit + folium UI
life-lists/         # your CSVs go here
data/               # SQLite db + caches (gitignored)
```

## Caching

| Resource              | Where                 | TTL     |
|-----------------------|-----------------------|---------|
| eBird taxonomy        | `taxonomy`            | 90 days |
| Hotspot obs           | `hotspot_obs`         | 6 hours |
| Seasonality samples   | `species_seasonality` | 30 days |
| Wikipedia summary     | `species_info`        | 30 days |

Pass `--force-refresh` to bypass hotspot, seasonality, and Wikipedia caches. Failed Wikipedia fetches (empty rows from a 4xx) are auto-cleared at the start of every enrichment pass and retried.
