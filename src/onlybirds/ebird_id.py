"""Scrape the Identification blurb from eBird species pages.

The Wikipedia lead extract we already cache (see `enrich.py`) is taxonomy- and
range-heavy, and rarely describes the bird visually — useless as an embedding
corpus for "I saw a small gray bird with a black cap" style queries. eBird's
species page has a short, Merlin-written Identification block (3–4 sentences
covering color, pattern, behavior, habitat, similar species) which is exactly
what we want.

Politeness: gated behind ONLYBIRDS_EBIRD_SCRAPE=1 so the scrape never runs
unintentionally. UA includes ONLYBIRDS_CONTACT (email/URL) so eBird ops can
reach us if the volume becomes a concern.
"""

import datetime as dt
import os
import sqlite3
import time

import httpx
from selectolax.parser import HTMLParser
from tqdm import tqdm

EBIRD_TTL_DAYS = 90
EBIRD_SPECIES_URL = "https://ebird.org/species/{code}"
RATE_LIMIT_SLEEP = 0.7


def _user_agent() -> str:
    """UA with contact pulled from .env (ONLYBIRDS_CONTACT). Never hardcoded."""
    contact = (os.environ.get("ONLYBIRDS_CONTACT") or "").strip()
    return f"onlybirds-ebird/0.1 ({contact})" if contact else "onlybirds-ebird/0.1"


def _is_fresh(fetched_at: str | None, ttl_days: int = EBIRD_TTL_DAYS) -> bool:
    if not fetched_at:
        return False
    age = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.datetime.fromisoformat(fetched_at)
    return age.days < ttl_days


def _parse_id_text(html: str) -> str | None:
    """Pull the Identification paragraph from an eBird species page.

    Selector verified on /species/oaktit: the block lives at
    `div.Species-identification-text > p`. We take the first paragraph; some
    pages have more than one but the first is the canonical Merlin blurb.
    """
    tree = HTMLParser(html)
    node = tree.css_first("div.Species-identification-text p")
    if node is None:
        return None
    text = node.text(strip=True)
    return text or None


def _fetch_id_text(species_code: str, headers: dict) -> str | None:
    url = EBIRD_SPECIES_URL.format(code=species_code)
    for attempt in range(3):
        try:
            r = httpx.get(url, headers=headers, follow_redirects=True, timeout=15)
        except httpx.HTTPError:
            return None
        if r.status_code in (403, 429, 503) and attempt < 2:
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None
        return _parse_id_text(r.text)
    return None


def _clear_failed_fetches(conn: sqlite3.Connection) -> int:
    """Drop ebird_fetched_at on rows where the cached eBird fetch returned nothing.

    Mirrors enrich.py's pattern: if ebird_fetched_at is set but ebird_id_text is
    empty, that's a transient failure we'd like the TTL check to retry. We only
    clear the timestamp — never the text — so existing good rows are untouched.
    """
    cur = conn.execute(
        "UPDATE species_info SET ebird_fetched_at = NULL "
        "WHERE ebird_fetched_at IS NOT NULL "
        "  AND (ebird_id_text IS NULL OR ebird_id_text = '')"
    )
    conn.commit()
    return cur.rowcount or 0


def _scrape_enabled() -> bool:
    return (os.environ.get("ONLYBIRDS_EBIRD_SCRAPE") or "").strip() == "1"


def enrich_ebird_id_text(conn: sqlite3.Connection, force: bool = False) -> dict[str, int]:
    """Populate species_info.ebird_id_text for every species we might display.

    Scope: union of (targets) and (species observed at any cached hotspot) — same
    as enrich.py:enrich_species — so the embedding corpus covers everything the
    dashboard can render.
    """
    if not _scrape_enabled():
        return {"skipped_disabled": 1, "fetched": 0, "skipped_fresh": 0,
                "cleared_failed": 0, "failed_now": 0, "total_species": 0}

    cleared = _clear_failed_fetches(conn)

    rows = conn.execute(
        """
        SELECT x.species_code, s.ebird_fetched_at
        FROM taxonomy x
        LEFT JOIN species_info s ON s.species_code = x.species_code
        WHERE x.species_code IN (SELECT species_code FROM targets)
           OR x.species_code IN (SELECT species_code FROM hotspot_obs)
        """
    ).fetchall()

    headers = {"User-Agent": _user_agent(), "Accept": "text/html"}
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    to_fetch = [r for r in rows if force or not _is_fresh(r["ebird_fetched_at"])]
    skipped = len(rows) - len(to_fetch)

    fetched = 0
    failed = 0
    for r in tqdm(to_fetch, desc="  ebird", unit="bird", leave=False, disable=not to_fetch):
        text = _fetch_id_text(r["species_code"], headers)
        if text is None:
            failed += 1
        else:
            # Upsert: species_info may have no row yet for species lacking a Wikipedia hit.
            # `fetched_at` is NOT NULL in the schema, so we provide `now` for new rows.
            conn.execute(
                "INSERT INTO species_info(species_code, fetched_at, ebird_id_text, ebird_fetched_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(species_code) DO UPDATE SET "
                "  ebird_id_text = excluded.ebird_id_text, "
                "  ebird_fetched_at = excluded.ebird_fetched_at",
                (r["species_code"], now, text, now),
            )
            fetched += 1
        time.sleep(RATE_LIMIT_SLEEP)
    conn.commit()

    return {
        "fetched": fetched,
        "skipped_fresh": skipped,
        "cleared_failed": cleared,
        "failed_now": failed,
        "total_species": len(rows),
    }
