"""Taxonomy cache + species-name resolution.

eBird identifies species by 6-letter `species_code` (e.g. `norcar` for Northern Cardinal).
User CSVs almost always use common names. This module resolves names → codes,
falling back to fuzzy match when the CSV doesn't use canonical eBird names.
"""

import datetime as dt
import sqlite3
from enum import Enum
from typing import Iterable

from rapidfuzz import process, fuzz

from .ebird import EBirdClient

TAXONOMY_TTL_DAYS = 90


class TaxonomyCategory(str, Enum):
    SPECIES = "species"
    SUBSPECIES = "issf"
    HYBRID = "hybrid"
    INTERGRADE = "intergrade"
    FORM = "form"
    SPUH = "spuh"
    SLASH = "slash"
    DOMESTIC = "domestic"


def _is_stale(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT MAX(fetched_at) AS f FROM taxonomy").fetchone()
    if not row or not row["f"]:
        return True
    fetched = dt.datetime.fromisoformat(row["f"])
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - fetched).days >= TAXONOMY_TTL_DAYS


def refresh_if_stale(conn: sqlite3.Connection, client: EBirdClient) -> int:
    """Fetch eBird taxonomy if cache is stale. Returns row count after refresh."""
    if not _is_stale(conn):
        return conn.execute("SELECT COUNT(*) AS c FROM taxonomy").fetchone()["c"]

    rows = client.taxonomy()
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    conn.execute("DELETE FROM taxonomy")
    conn.executemany(
        "INSERT INTO taxonomy(species_code, common_name, sci_name, family, fetched_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (r["speciesCode"], r["comName"], r["sciName"], r.get("familyComName"), now)
            for r in rows
            if r.get("category") == TaxonomyCategory.SPECIES
        ],
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) AS c FROM taxonomy").fetchone()["c"]


def resolve_names(conn: sqlite3.Connection, names: Iterable[str], min_score: int = 88) -> dict[str, str | None]:
    """Map free-text names → species_code. Unmatched entries get None."""
    cur = conn.execute("SELECT species_code, common_name, sci_name FROM taxonomy")
    table = cur.fetchall()
    by_common = {r["common_name"].lower(): r["species_code"] for r in table}
    by_sci = {r["sci_name"].lower(): r["species_code"] for r in table}
    choices = list(by_common.keys()) + list(by_sci.keys())

    out: dict[str, str | None] = {}
    for name in names:
        if name in out:
            continue
        key = name.strip().lower()
        if key in by_common:
            out[name] = by_common[key]
            continue
        if key in by_sci:
            out[name] = by_sci[key]
            continue
        match = process.extractOne(key, choices, scorer=fuzz.WRatio, score_cutoff=min_score)
        if match:
            matched_key = match[0]
            out[name] = by_common.get(matched_key) or by_sci.get(matched_key)
        else:
            out[name] = None
    return out
