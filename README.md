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
echo 'EBIRD_API_KEY=…' > .env   # see "Ebird Api Key" above
```

The CLI auto-loads `.env` from the cwd, so no `export` needed.

## Run the pipeline

```bash
just latest                                    # newest CSV + lat/lon from .env
just latest 33.88 -118.40                      # override lat/lon
just run life-lists/05-07-2026.csv             # explicit CSV, lat/lon from .env
just run life-lists/05-07-2026.csv 33.88 -118.40
```

Set defaults in `.env`:

```
EBIRD_API_KEY=…
ONLYBIRDS_LAT=33.88
ONLYBIRDS_LON=-118.40
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

Map tab shows hotspots — red pins have at least one rare target, blue pins have non-rare targets, popups list the species. Target-list tab is a scrollable feed with photos and Wikipedia blurbs.

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
  targets.py        # set diff: hotspot species not in life list
  rare.py           # cross-check against /recent/notable
  enrich.py         # Wikipedia summary + image (TTL 30d)
dashboard/
  app.py            # Streamlit + folium UI
life-lists/         # your CSVs go here
data/               # SQLite db + caches (gitignored)
```

## Caching

| Resource           | Where         | TTL     |
|--------------------|---------------|---------|
| eBird taxonomy     | `taxonomy`    | 90 days |
| Hotspot obs        | `hotspot_obs` | 6 hours |
| Wikipedia summary  | `species_info`| 30 days |

Pass `--force-refresh` to bypass hotspot + Wikipedia caches.
