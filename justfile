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

# Run the pipeline against the most recent CSV in life-lists/ (parsed from MM-DD-YYYY.csv). lat/lon fall back to ONLYBIRDS_LAT/LON in .env.
latest lat="" lon="" radius="25":
    uv run onlybirds run --csv life-lists/ {{ if lat != "" { "--lat " + lat } else { "" } }} {{ if lon != "" { "--lon " + lon } else { "" } }} --radius {{radius}}

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
