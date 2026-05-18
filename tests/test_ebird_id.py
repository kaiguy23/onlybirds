"""Tests for the eBird Identification scraper.

Live HTTP is monkeypatched out — these only verify our parsing, TTL/skip
logic, and cache-update behavior.
"""

import datetime as dt

import pytest

from onlybirds import ebird_id

# Verbatim snippet from ebird.org/species/oaktit fetched during development —
# the selector we rely on is `div.Species-identification-text > p`.
SAMPLE_HTML = """
<html><body>
<div class="Species-identification">
  <div class="Species-identification-heading"><h2>Identification</h2></div>
  <div class="Species-identification-text">
    <p class="u-stack-sm">Completely nondescript: all gray-brown without any sort of color pattern.</p>
  </div>
</div>
</body></html>
"""

HTML_NO_BLOCK = "<html><body><p>no identification section here</p></body></html>"


def _seed(conn, code="oaktit"):
    conn.execute(
        "INSERT INTO taxonomy(species_code, common_name, sci_name, fetched_at) VALUES (?, ?, ?, ?)",
        (code, "Oak Titmouse", "Baeolophus inornatus", "2025-01-01"),
    )
    conn.execute(
        "INSERT INTO targets(species_code, first_flagged, is_rare) VALUES (?, ?, 0)",
        (code, "2025-01-01"),
    )
    conn.commit()


def test_parse_extracts_identification_paragraph():
    text = ebird_id._parse_id_text(SAMPLE_HTML)
    assert text is not None
    assert text.startswith("Completely nondescript")


def test_parse_returns_none_when_block_missing():
    assert ebird_id._parse_id_text(HTML_NO_BLOCK) is None


def test_enrich_disabled_without_env(conn, monkeypatch):
    monkeypatch.delenv("ONLYBIRDS_EBIRD_SCRAPE", raising=False)
    _seed(conn)
    stats = ebird_id.enrich_ebird_id_text(conn)
    assert stats == {
        "skipped_disabled": 1, "fetched": 0, "skipped_fresh": 0,
        "cleared_failed": 0, "failed_now": 0, "total_species": 0,
    }


def test_enrich_populates_text(conn, monkeypatch):
    monkeypatch.setenv("ONLYBIRDS_EBIRD_SCRAPE", "1")
    monkeypatch.setattr(ebird_id, "RATE_LIMIT_SLEEP", 0)
    monkeypatch.setattr(ebird_id, "_fetch_id_text", lambda code, headers: "Small drab gray bird with a black cap.")
    _seed(conn)
    stats = ebird_id.enrich_ebird_id_text(conn)
    assert stats["fetched"] == 1
    assert stats["failed_now"] == 0
    row = conn.execute("SELECT ebird_id_text, ebird_fetched_at FROM species_info WHERE species_code='oaktit'").fetchone()
    assert row["ebird_id_text"].startswith("Small drab")
    assert row["ebird_fetched_at"]


def test_enrich_does_not_clobber_on_fetch_failure(conn, monkeypatch):
    monkeypatch.setenv("ONLYBIRDS_EBIRD_SCRAPE", "1")
    monkeypatch.setattr(ebird_id, "RATE_LIMIT_SLEEP", 0)
    _seed(conn)
    # Pre-seed a good row.
    conn.execute(
        "INSERT INTO species_info(species_code, fetched_at, ebird_id_text, ebird_fetched_at) "
        "VALUES ('oaktit', '2025-01-01', 'GOOD TEXT', '1900-01-01')",
    )
    conn.commit()
    monkeypatch.setattr(ebird_id, "_fetch_id_text", lambda code, headers: None)
    stats = ebird_id.enrich_ebird_id_text(conn)
    assert stats["failed_now"] == 1
    row = conn.execute("SELECT ebird_id_text FROM species_info WHERE species_code='oaktit'").fetchone()
    assert row["ebird_id_text"] == "GOOD TEXT"  # untouched


def test_enrich_skips_when_fresh(conn, monkeypatch):
    monkeypatch.setenv("ONLYBIRDS_EBIRD_SCRAPE", "1")
    monkeypatch.setattr(ebird_id, "RATE_LIMIT_SLEEP", 0)
    _seed(conn)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO species_info(species_code, fetched_at, ebird_id_text, ebird_fetched_at) "
        "VALUES ('oaktit', ?, 'FRESH', ?)",
        (now, now),
    )
    conn.commit()
    monkeypatch.setattr(ebird_id, "_fetch_id_text", lambda code, headers: pytest.fail("should not fetch"))
    stats = ebird_id.enrich_ebird_id_text(conn)
    assert stats["skipped_fresh"] == 1
    assert stats["fetched"] == 0


def test_clear_failed_fetches_resets_timestamp(conn):
    # A row whose previous fetch failed: timestamp set, text empty.
    conn.execute(
        "INSERT INTO species_info(species_code, fetched_at, ebird_fetched_at) "
        "VALUES ('foo', '2025-01-01', '2025-01-01')",
    )
    # A row with good text: should be untouched.
    conn.execute(
        "INSERT INTO species_info(species_code, fetched_at, ebird_id_text, ebird_fetched_at) "
        "VALUES ('bar', '2025-01-01', 'good', '2025-01-01')",
    )
    conn.commit()
    cleared = ebird_id._clear_failed_fetches(conn)
    assert cleared == 1
    foo = conn.execute("SELECT ebird_fetched_at FROM species_info WHERE species_code='foo'").fetchone()
    bar = conn.execute("SELECT ebird_fetched_at FROM species_info WHERE species_code='bar'").fetchone()
    assert foo["ebird_fetched_at"] is None
    assert bar["ebird_fetched_at"] == "2025-01-01"
