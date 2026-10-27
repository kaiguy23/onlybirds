# onlybirds

Find birds you haven't seen yet at nearby eBird hotspots, with rare-bird flags and Wikipedia enrichment, served up in a Streamlit + Folium dashboard.

## Ebird Api Key
visit https://ebird.org/api/keygen and request a key. Set it as `EBIRD_API_KEY`.

## Carto Api Key
visit https://carto.com/basemaps/apikey/ and request a key. The dashboard map uses CARTO Positron tiles, which require this key on every tile request. Set it as `CARTO_API_KEY`. The browser loads the tiles, so the key is embedded in the map HTML.

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
CARTO_API_KEY=…                 # https://carto.com/basemaps/apikey/ — Positron map tiles
```

`CARTO_API_KEY` is required for the map. The dashboard raises an error when it is unset. Request one at https://carto.com/basemaps/apikey/.

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

Hotspot and consolidated-hotspot detail pages default to **targets only** (birds you haven't seen). A "Show all birds at this hotspot" toggle expands the list to every species observed there, with the same card layout (image + Wikipedia summary). Wikipedia enrichment runs against the union of targets and species seen at any cached hotspot, so seen-bird cards have the same metadata as targets.

**Target-list tab.** Scrollable feed with photos, Wikipedia blurbs, in-season badges (✅ / ⚠️ off-season with a Jan–Dec strip), and a "rare alerts only" toggle.

## Plan a trip (refresh without your laptop)

`.github/workflows/trip.yml` runs the pipeline in GitHub Actions for a new place, waits out eBird's 500 calls/hour quota (a new city takes 1–2 hours), and commits `data/onlybirds.db`, so Streamlit Cloud redeploys with it. Start it any of three ways:

- the dashboard's **✈️ Plan a trip** sidebar form (city name, or lat/lon),
- the GitHub app: Actions → trip → Run workflow,
- `just trip "Austin, TX"` (needs the `gh` CLI).

**Pull your latest life list.** After a day out, `.github/workflows/lifelist.yml` logs into ebird.org with `EBIRD_USERNAME`/`EBIRD_PASSWORD`, downloads your life list, updates `observations` and targets (rare-alert flags are kept), and commits the DB. It takes a few minutes and makes no hotspot calls. Start it from the **Pull my latest life list** button in the same sidebar section, from Actions → lifelist, or with `just sync`. Locally, `onlybirds sync-lifelist` does the same thing.

One-time setup:

1. **Repo secrets** (Settings → Secrets and variables → Actions): `EBIRD_API_KEY`, `GEMINI_API_KEY`, `EBIRD_USERNAME`/`EBIRD_PASSWORD` (the life-list sync needs them; trips also use them to pull today's list first), and `ONLYBIRDS_CONTACT`.
2. **For the dashboard form**, a [fine-grained token](https://github.com/settings/personal-access-tokens/new) for this repo only, with *Actions: Read and write*. Add to the Streamlit app's secrets:

   ```toml
   GITHUB_TRIP_TOKEN = "github_pat_…"
   TRIP_ADMINS = "you@example.com"   # viewers who see the form; "*" = anyone who can open the app
   ```

   The form stays hidden unless both are set and the viewer's email (from Community Cloud's login) is listed.
3. **Optional:** `GOOGLE_MAPS_API_KEY` (in both the Streamlit secrets and the repo secrets) looks places up with Google's Geocoding API instead of OpenStreetMap. It handles landmarks and loose spellings better. Restrict the key to the Geocoding API.

Each run uses up to a couple of hours of your eBird quota, so keep the app's sharing set to invited viewers only.

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
  hotspots.py       # nearby hotspots + recent obs (TTL 24h, quota-aware)
  seasonality.py    # sample historical obs per county (TTL 30d)
  targets.py        # set diff: hotspot species not in life list
  rare.py           # cross-check against /recent/notable
  enrich.py         # Wikipedia summary + image (TTL 30d) — targets ∪ hotspot species
  trip.py           # geocode a city + start the trip refresh workflow
  dashboard/        # Streamlit + folium UI
    app.py          # entrypoint + routing
    views.py        # map + hotspot/consolidated detail pages
    compare.py      # hotspot-compare feature (state, tray, view)
    targets_view.py # target species rendering + filter bar
    markers.py      # folium markers + tooltip/popup HTML
    regions.py      # region filter chips
    mini_map.py     # compact map for detail pages
    basemap.py      # CARTO Positron tiles (CARTO_API_KEY)
    trip_widget.py  # sidebar "Plan a trip" form (owner-only)
    data.py, urls.py, utils.py, styles.py
life-lists/         # your CSVs go here
data/               # SQLite db + caches (gitignored)
```

## Caching

| Resource              | Where                 | TTL     |
|-----------------------|-----------------------|---------|
| eBird taxonomy        | `taxonomy`            | 90 days |
| Hotspot obs           | `hotspot_obs`         | 24 hours |
| Seasonality samples   | `species_seasonality` | 30 days |
| Wikipedia summary     | `species_info`        | 30 days |

Pass `--force-refresh` to bypass hotspot, seasonality, and Wikipedia caches. Failed Wikipedia fetches (empty rows from a 4xx) are auto-cleared at the start of every enrichment pass and retried.
