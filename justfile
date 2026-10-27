set dotenv-load := true

# Show available recipes.
default:
    @just --list

# Install/sync Python deps via uv.
install:
    uv sync

# Run the pipeline against an explicit CSV. lat/lon fall back to ONLYBIRDS_LAT/LON in .env.
run csv lat="" lon="" radius="25":
    uv run onlybirds run --csv {{csv}} {{ if lat != "" { "--lat " + lat } else { "" } }} {{ if lon != "" { "--lon " + lon } else { "" } }} --radius {{radius}}

# Download today's life list from eBird into life-lists/MM-DD-YYYY.csv (needs EBIRD_USERNAME/EBIRD_PASSWORD or EBIRD_SESSIONID in .env).
fetch:
    uv run onlybirds fetch-lifelist

# Fetch the life list from eBird, then run the pipeline against the most recent CSV in life-lists/. lat/lon fall back to ONLYBIRDS_LAT/LON in .env.
latest lat="" lon="" radius="50": fetch
    uv run onlybirds run --csv life-lists/ {{ if lat != "" { "--lat " + lat } else { "" } }} {{ if lon != "" { "--lon " + lon } else { "" } }} --radius {{radius}}

# Unattended run against the most recent CSV in life-lists/: waits out eBird's hourly quota instead of skipping work.
overnight lat lon radius="50":
    uv run onlybirds run --csv life-lists/ --lat {{lat}} --lon {{lon}} --radius {{radius}} --wait-for-quota

# Refresh a trip destination in GitHub Actions (laptop can be off): just trip "Austin, TX" [radius]
trip city radius="50":
    gh workflow run trip.yml -f city="{{city}}" -f radius={{radius}}

# Launch the Streamlit + folium dashboard.
serve port="8501":
    uv run onlybirds serve --port {{port}}

# Show row counts in the local SQLite DB.
status:
    uv run onlybirds status

# Run the type checker.
check:
    uv run ty check

# Wipe local cache (SQLite + WAL files).
clean:
    rm -rf data/
