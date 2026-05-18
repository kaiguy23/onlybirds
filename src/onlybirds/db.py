import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path("data/onlybirds.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS taxonomy (
    species_code TEXT PRIMARY KEY,
    common_name  TEXT NOT NULL,
    sci_name     TEXT NOT NULL,
    family       TEXT,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_common ON taxonomy(common_name);
CREATE INDEX IF NOT EXISTS idx_taxonomy_sci    ON taxonomy(sci_name);

CREATE TABLE IF NOT EXISTS observations (
    species_code TEXT NOT NULL,
    observed_on  TEXT NOT NULL,
    lat          REAL,
    lon          REAL,
    location     TEXT NOT NULL DEFAULT '',
    source_row   INTEGER,
    PRIMARY KEY (species_code, observed_on, location)
);
CREATE INDEX IF NOT EXISTS idx_obs_species ON observations(species_code);

CREATE TABLE IF NOT EXISTS hotspots (
    hotspot_id  TEXT PRIMARY KEY,
    name        TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    region      TEXT,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hotspot_obs (
    hotspot_id    TEXT NOT NULL,
    species_code  TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    how_many      INTEGER,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (hotspot_id, species_code)
);
CREATE INDEX IF NOT EXISTS idx_hotspot_obs_species ON hotspot_obs(species_code);

CREATE TABLE IF NOT EXISTS targets (
    species_code  TEXT PRIMARY KEY,
    first_flagged TEXT NOT NULL,
    is_rare       INTEGER NOT NULL DEFAULT 0,
    rare_seen_at  TEXT,
    rare_lat      REAL,
    rare_lon      REAL,
    rare_loc_name TEXT
);

CREATE TABLE IF NOT EXISTS species_info (
    species_code TEXT PRIMARY KEY,
    summary      TEXT,
    image_url    TEXT,
    wiki_url     TEXT,
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS species_seasonality (
    species_code TEXT NOT NULL,
    region       TEXT NOT NULL,
    months       TEXT NOT NULL,  -- JSON array of month numbers (1-12) where reported
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (species_code, region)
);
CREATE INDEX IF NOT EXISTS idx_seasonality_species ON species_seasonality(species_code);

CREATE TABLE IF NOT EXISTS consolidated_hotspots (
    consolidated_id TEXT PRIMARY KEY,
    name            TEXT,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    member_count    INTEGER NOT NULL,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consolidated_hotspot_members (
    consolidated_id TEXT NOT NULL,
    hotspot_id      TEXT NOT NULL,
    PRIMARY KEY (consolidated_id, hotspot_id),
    FOREIGN KEY (consolidated_id) REFERENCES consolidated_hotspots(consolidated_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chm_hotspot ON consolidated_hotspot_members(hotspot_id);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


_SPECIES_INFO_EXTRA_COLUMNS = (
    ("ebird_id_text", "TEXT"),
    ("ebird_fetched_at", "TEXT"),
    ("embedding", "BLOB"),
    ("embedding_source_hash", "TEXT"),
    ("embedding_model", "TEXT"),
)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add columns missing from `species_info`.

    SQLite has no `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, so we read
    `PRAGMA table_info` and only run ALTERs for columns that don't yet exist.
    Safe to call on every connection.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(species_info)")}
    for name, decl in _SPECIES_INFO_EXTRA_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE species_info ADD COLUMN {name} {decl}")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.commit()


@contextmanager
def session(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        init_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()
