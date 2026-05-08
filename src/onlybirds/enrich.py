"""Enrich target species with Wikipedia summaries + lead images.

The canonical signal that we got the right page is the binomial: every species
article mentions its scientific name. We probe in order — sci name → common name
disambiguated to "(bird)" → plain common name — and refuse a page if the
description doesn't say "bird" and the extract doesn't quote the binomial. This
catches the classic ambiguous-common-name trap (e.g. "Redhead" the duck vs.
"Redhead" the human hair color).
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import time

import httpx
import wikipediaapi
from tqdm import tqdm

ENRICH_TTL_DAYS = 30
WIKIMEDIA_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"


def _user_agent() -> str:
    """Wikimedia asks for an identifying UA with a contact (email or URL).

    Pulled from ONLYBIRDS_CONTACT so we don't bake personal info into source.
    Without it the API still works at low volumes but is more aggressive
    about 403/429 throttling, especially on bursty enrichment runs.
    """
    contact = (os.environ.get("ONLYBIRDS_CONTACT") or "").strip()
    return f"onlybirds/0.1 ({contact})" if contact else "onlybirds/0.1"


def _is_fresh(fetched_at: str | None, ttl_days: int = ENRICH_TTL_DAYS) -> bool:
    if not fetched_at:
        return False
    age = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.datetime.fromisoformat(fetched_at)
    return age.days < ttl_days


def _looks_like_bird_page(data: dict, sci_name: str) -> bool:
    """Reject pages that almost certainly aren't about the bird species.

    We layer several heuristics because Wikipedia's REST summary truncates
    extracts inconsistently — sometimes the binomial sits in the lead, other
    times only the common name does. The combined checks should accept any
    real bird article while still rejecting the classic ambiguous-common-name
    trap (e.g. "Redhead" the duck → "Redhead" the human hair color).
    """
    desc = (data.get("description") or "").lower()
    extract = (data.get("extract") or "").lower()
    title = (data.get("title") or "").lower()
    sci_lower = (sci_name or "").lower()

    # Strongest signal: the binomial appears verbatim in the article body.
    if sci_lower and sci_lower in extract:
        return True
    # Genus alone is usually distinctive enough (e.g. "Cathartes" for vultures).
    # Skip super-short genus tokens that could match incidentally.
    if sci_lower:
        genus = sci_lower.split()[0] if sci_lower.split() else ""
        if len(genus) >= 5 and genus in extract:
            return True
    # Wikidata-derived descriptions for taxon pages start with these prefixes.
    if desc.startswith(("species of", "subspecies of", "genus of", "family of")):
        return True
    # Either side mentions the literal word "bird" — covers articles whose
    # description is family-specific ("Member of the woodpecker family") but
    # whose lead text still says "is a … bird of the …" somewhere.
    if "bird" in desc or "bird" in extract:
        return True
    # Article title was explicitly disambiguated to (bird) — trust it.
    if "(bird)" in title:
        return True
    return False


def _fetch_summary(title: str, headers: dict) -> dict | None:
    url = WIKIMEDIA_SUMMARY.format(title=title.replace(" ", "_"))
    for attempt in range(3):
        try:
            r = httpx.get(url, headers=headers, follow_redirects=True, timeout=15)
        except httpx.HTTPError:
            return None
        # Wikimedia throttles bursty traffic with 429 / 403 — short backoff and retry.
        if r.status_code in (403, 429, 503) and attempt < 2:
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("type") == "disambiguation":
            return None
        return data
    return None


def _fetch_wikipedia(common_name: str, sci_name: str) -> dict[str, str | None]:
    """Resolve a species → Wikipedia summary, with binomial-based validation.

    Probe order:
      1. Scientific name — unambiguous Latin binomial; redirects to the canonical page.
      2. "<common> (bird)" — Wikipedia's standard disambiguator for birds.
      3. Plain common name — last resort, only accepted if it passes the bird check.
    """
    headers = {"User-Agent": _user_agent(), "accept": "application/json"}
    candidates = []
    if sci_name:
        candidates.append(sci_name)
    if common_name:
        candidates.append(f"{common_name} (bird)")
        candidates.append(common_name)

    last_unverified: dict | None = None
    for title in candidates:
        data = _fetch_summary(title, headers)
        if not data:
            continue
        if _looks_like_bird_page(data, sci_name):
            return _summary_to_record(data)
        # Hold onto it as a last-ditch fallback so we don't return nothing when
        # the binomial is missing from the extract on a stub article.
        last_unverified = last_unverified or data

    if last_unverified is not None:
        # Only fall back when the title itself contains the sci name — otherwise
        # we risk surfacing a clearly-wrong article like "Redhead" the hair color.
        title = (last_unverified.get("title") or "").lower()
        if sci_name and sci_name.lower() in title:
            return _summary_to_record(last_unverified)

    return {"summary": None, "image_url": None, "wiki_url": None}


def _summary_to_record(data: dict) -> dict[str, str | None]:
    return {
        "summary": data.get("extract"),
        "image_url": (data.get("thumbnail") or {}).get("source")
                     or (data.get("originalimage") or {}).get("source"),
        "wiki_url": (data.get("content_urls", {}).get("desktop") or {}).get("page"),
    }


def _clear_failed_fetches(conn: sqlite3.Connection) -> int:
    """Drop cached rows where a previous fetch returned nothing.

    These are entries with no wiki_url AND no summary — usually a transient
    Wikipedia 4xx/5xx (rate limit, blocked UA) at fetch time. Clearing them
    lets the TTL check trigger a retry on the next run; we never delete rows
    that have any real content, since the safe move is to keep what we have.
    """
    cur = conn.execute(
        "DELETE FROM species_info "
        "WHERE (wiki_url IS NULL OR wiki_url = '') "
        "  AND (summary IS NULL OR summary = '')"
    )
    conn.commit()
    return cur.rowcount or 0


def enrich_targets(conn: sqlite3.Connection, force: bool = False) -> dict[str, int]:
    cleared = _clear_failed_fetches(conn)

    rows = conn.execute(
        """
        SELECT t.species_code, x.common_name, x.sci_name, s.fetched_at
        FROM targets t
        JOIN taxonomy x ON x.species_code = t.species_code
        LEFT JOIN species_info s ON s.species_code = t.species_code
        """
    ).fetchall()

    fetched = 0
    failed = 0
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    to_fetch = [r for r in rows if force or not _is_fresh(r["fetched_at"])]
    skipped = len(rows) - len(to_fetch)
    for r in tqdm(to_fetch, desc="  wikipedia", unit="bird", leave=False, disable=not to_fetch):
        info = _fetch_wikipedia(r["common_name"], r["sci_name"])
        # Never clobber an existing good row with a failed fetch — if the
        # network or a 4xx ate this request, leave the cache alone and let
        # the next run try again (the row stays absent → still stale).
        if info["summary"] is None and info["wiki_url"] is None:
            failed += 1
            continue
        conn.execute(
            "INSERT OR REPLACE INTO species_info(species_code, summary, image_url, wiki_url, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (r["species_code"], info["summary"], info["image_url"], info["wiki_url"], now),
        )
        fetched += 1
    conn.commit()
    return {
        "fetched": fetched,
        "skipped_fresh": skipped,
        "cleared_failed": cleared,
        "failed_now": failed,
        "total_targets": len(rows),
    }
