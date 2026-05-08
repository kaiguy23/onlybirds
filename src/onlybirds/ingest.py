"""Load a user's CSV of personal sightings into the observations table.

Expected columns (case-insensitive, flexible names):
  - species / common_name / Common Name        (required)
  - date / observed_on / Date                  (required, ISO or any pandas-parseable)
  - lat / latitude                             (optional)
  - lon / lng / longitude                      (optional)
  - location / locality                        (optional)
"""

import re
import sqlite3
from pathlib import Path

import pandas as pd

from .ebird import EBirdClient
from .taxonomy import refresh_if_stale, resolve_names

# eBird life-list exports are named MM-DD-YYYY.csv. We sort by parsed date when
# available so renames / copies don't lie about recency, and fall back to mtime
# only for files that don't match the pattern.
_CSV_DATE_RE = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})")


def pick_latest_csv(directory: Path | str) -> Path:
    files = sorted(Path(directory).glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"no CSVs in {directory}")

    def key(p: Path) -> tuple:
        m = _CSV_DATE_RE.search(p.stem)
        if m:
            mm, dd, yyyy = (int(g) for g in m.groups())
            return (1, yyyy, mm, dd)
        return (0, p.stat().st_mtime, 0, 0)

    return max(files, key=key)

_SPECIES_ALIASES = ("species", "common_name", "common name", "comname", "bird")
_DATE_ALIASES = ("date", "observed_on", "obs_date", "obsdt")
_LAT_ALIASES = ("lat", "latitude")
_LON_ALIASES = ("lon", "lng", "longitude")
_LOC_ALIASES = ("location", "locality", "loc_name")


def _pick(df: pd.DataFrame, aliases: tuple[str, ...]) -> str | None:
    lower = {c.lower(): c for c in df.columns}
    for a in aliases:
        if a in lower:
            return lower[a]
    return None


def ingest_csv(conn: sqlite3.Connection, client: EBirdClient, csv_path: Path | str) -> dict[str, int]:
    df = pd.read_csv(csv_path)
    species_col = _pick(df, _SPECIES_ALIASES)
    date_col = _pick(df, _DATE_ALIASES)
    if not species_col or not date_col:
        raise ValueError(
            f"CSV must have a species column ({_SPECIES_ALIASES}) and a date column ({_DATE_ALIASES}); "
            f"got {list(df.columns)}"
        )
    lat_col = _pick(df, _LAT_ALIASES)
    lon_col = _pick(df, _LON_ALIASES)
    loc_col = _pick(df, _LOC_ALIASES)

    refresh_if_stale(conn, client)
    names = df[species_col].dropna().astype(str).unique().tolist()
    code_map = resolve_names(conn, names)

    rows = []
    unmatched: list[str] = []
    for i, row in df.iterrows():
        raw = str(row[species_col]) if pd.notna(row[species_col]) else None
        if not raw:
            continue
        code = code_map.get(raw)
        if not code:
            unmatched.append(raw)
            continue
        observed_on = pd.to_datetime(row[date_col], errors="coerce")
        if pd.isna(observed_on):
            continue
        rows.append((
            code,
            observed_on.date().isoformat(),
            float(row[lat_col]) if lat_col and pd.notna(row[lat_col]) else None,
            float(row[lon_col]) if lon_col and pd.notna(row[lon_col]) else None,
            str(row[loc_col]) if loc_col and pd.notna(row[loc_col]) else "",
            int(i),  # ty: ignore[invalid-argument-type]  # iterrows() index is int here
        ))

    conn.executemany(
        "INSERT OR REPLACE INTO observations(species_code, observed_on, lat, lon, location, source_row) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return {
        "ingested": len(rows),
        "unmatched": len(set(unmatched)),
        "distinct_species": len({r[0] for r in rows}),
    }
