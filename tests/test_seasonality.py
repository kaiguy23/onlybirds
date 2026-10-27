import datetime as dt
import json
from typing import cast

import pytest

from onlybirds import seasonality
from onlybirds.ebird import EBirdClient, EBirdError, EBirdObservation, EBirdRateLimited
from onlybirds.seasonality import _is_fresh, _sample_dates, compute_seasonality


def _obs(species_code: str) -> EBirdObservation:
    return EBirdObservation.from_api({"speciesCode": species_code})


class FakeClient:
    """Stand-in for EBirdClient.region_historic.

    obs_by_key maps (region, year, month, day) → list of obs dicts.
    Missing keys default to []. Setting a value to an EBirdError raises.
    """

    def __init__(self, obs_by_key: dict | None = None, hourly_remaining: int | None = None):
        self.obs_by_key = obs_by_key or {}
        self.calls: list[tuple] = []
        self.hourly_remaining = hourly_remaining
        self.wait_for_quota = False

    def region_historic(self, region: str, year: int, month: int, day: int):
        self.calls.append((region, year, month, day))
        v = self.obs_by_key.get((region, year, month, day), [])
        if isinstance(v, Exception):
            raise v
        return v


class TestSampleDates:
    def test_count_excludes_today_and_future(self, monkeypatch):
        # Freeze "today" to a known date.
        fake_today = dt.date(2026, 5, 8)

        class _D(dt.date):
            @classmethod
            def today(cls):
                return fake_today

        monkeypatch.setattr(seasonality.dt, "date", _D)

        dates = _sample_dates(fake_today, n_months=12)
        # 12 months × 1 sample day = 12, minus same-month samples that fall on/after today.
        # SAMPLE_DAYS = (15,); today = May 8 → drop May 15.
        assert len(dates) == 12 - 1
        assert all(d < fake_today for d in dates)

    def test_clamps_to_last_day_of_month(self, monkeypatch):
        # End-date in February of a non-leap year — day 25 stays, 30 doesn't apply (max is 25).
        fake_today = dt.date(2027, 3, 15)

        class _D(dt.date):
            @classmethod
            def today(cls):
                return fake_today

        monkeypatch.setattr(seasonality.dt, "date", _D)

        dates = _sample_dates(dt.date(2026, 2, 28), n_months=1)
        assert sorted(dates) == [dt.date(2026, 2, 15)]

    def test_wraps_year_boundary(self, monkeypatch):
        fake_today = dt.date(2026, 5, 8)

        class _D(dt.date):
            @classmethod
            def today(cls):
                return fake_today

        monkeypatch.setattr(seasonality.dt, "date", _D)

        dates = _sample_dates(dt.date(2026, 1, 15), n_months=3)
        years_months = sorted({(d.year, d.month) for d in dates})
        assert years_months == [(2025, 11), (2025, 12), (2026, 1)]


class TestIsFresh:
    def test_no_rows_is_stale(self, conn):
        assert _is_fresh(conn, "US-CA", 30) is False

    def test_recent_is_fresh(self, conn):
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO species_seasonality(species_code, region, months, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("norcar", "US-CA", "[1,2]", now),
        )
        assert _is_fresh(conn, "US-CA", 30) is True

    def test_old_is_stale(self, conn):
        old = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=45)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO species_seasonality(species_code, region, months, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("norcar", "US-CA", "[1]", old),
        )
        assert _is_fresh(conn, "US-CA", 30) is False


class TestComputeSeasonality:
    def _seed_hotspot(self, conn, region: str = "US-CA"):
        conn.execute(
            "INSERT INTO hotspots(hotspot_id, name, lat, lon, region, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("L1", "Park", 40.0, -73.0, region, "2026-01-01T00:00:00"),
        )

    def test_no_hotspots(self, conn):
        client = FakeClient()
        stats = compute_seasonality(conn, cast(EBirdClient, client))
        assert stats["regions_fetched"] == 0
        assert stats["api_calls"] == 0
        assert client.calls == []

    def test_records_months_per_species(self, conn, monkeypatch):
        self._seed_hotspot(conn)

        # Patch _sample_dates to return a tiny deterministic set across two months.
        sample = [dt.date(2026, 1, 5), dt.date(2026, 2, 5)]
        monkeypatch.setattr(seasonality, "_sample_dates", lambda *a, **k: sample)

        client = FakeClient(
            obs_by_key={
                ("US-CA", 2026, 1, 5): [_obs("norcar"), _obs("amerob")],
                ("US-CA", 2026, 2, 5): [_obs("norcar")],
            }
        )
        stats = compute_seasonality(conn, cast(EBirdClient, client))
        assert stats["regions_fetched"] == 1
        assert stats["api_calls"] == 2

        rows = {
            r["species_code"]: json.loads(r["months"])
            for r in conn.execute("SELECT species_code, months FROM species_seasonality")
        }
        assert rows == {"norcar": [1, 2], "amerob": [1]}

    def test_skips_fresh_region(self, conn, monkeypatch):
        self._seed_hotspot(conn)
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO species_seasonality(species_code, region, months, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("norcar", "US-CA", "[5]", now),
        )
        monkeypatch.setattr(seasonality, "_sample_dates", lambda *a, **k: [dt.date(2026, 1, 5)])

        client = FakeClient()
        stats = compute_seasonality(conn, cast(EBirdClient, client), force=False)
        assert stats["regions_fetched"] == 0
        assert stats["regions_cached"] == 1
        assert client.calls == []

    def test_force_refetches(self, conn, monkeypatch):
        self._seed_hotspot(conn)
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO species_seasonality(species_code, region, months, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("norcar", "US-CA", "[5]", now),
        )
        monkeypatch.setattr(seasonality, "_sample_dates", lambda *a, **k: [dt.date(2026, 3, 5)])

        client = FakeClient(obs_by_key={("US-CA", 2026, 3, 5): [_obs("blujay")]})
        stats = compute_seasonality(conn, cast(EBirdClient, client), force=True)
        assert stats["regions_fetched"] == 1
        rows = list(conn.execute("SELECT species_code, months FROM species_seasonality"))
        assert len(rows) == 1
        assert rows[0]["species_code"] == "blujay"
        assert json.loads(rows[0]["months"]) == [3]

    def test_skips_failed_dates(self, conn, monkeypatch):
        self._seed_hotspot(conn)
        sample = [dt.date(2026, 1, 5), dt.date(2026, 2, 5)]
        monkeypatch.setattr(seasonality, "_sample_dates", lambda *a, **k: sample)

        client = FakeClient(
            obs_by_key={
                ("US-CA", 2026, 1, 5): EBirdError("boom"),
                ("US-CA", 2026, 2, 5): [_obs("norcar")],
            }
        )
        stats = compute_seasonality(conn, cast(EBirdClient, client))
        assert stats["skipped_dates"] == 1
        assert stats["api_calls"] == 1
        rows = list(conn.execute("SELECT species_code, months FROM species_seasonality"))
        assert json.loads(rows[0]["months"]) == [2]

    def test_rate_limit_keeps_old_rows_and_defers(self, conn, monkeypatch):
        self._seed_hotspot(conn, region="US-CA")
        conn.execute(
            "INSERT INTO hotspots(hotspot_id, name, lat, lon, region, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("L2", "Pond", 41.0, -74.0, "US-NY", "2026-01-01T00:00:00"),
        )
        old = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=45)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO species_seasonality(species_code, region, months, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("norcar", "US-CA", "[5]", old),
        )
        sample = [dt.date(2026, 1, 15), dt.date(2026, 2, 15)]
        monkeypatch.setattr(seasonality, "_sample_dates", lambda *a, **k: sample)

        client = FakeClient(
            obs_by_key={
                ("US-CA", 2026, 1, 15): [_obs("blujay")],
                ("US-CA", 2026, 2, 15): EBirdRateLimited("quota"),
            }
        )
        stats = compute_seasonality(conn, cast(EBirdClient, client))
        assert stats["regions_fetched"] == 0
        assert stats["regions_deferred"] == 2
        assert all(c[0] == "US-CA" for c in client.calls)
        rows = list(conn.execute("SELECT species_code, months, fetched_at FROM species_seasonality"))
        assert [(r["species_code"], r["months"], r["fetched_at"]) for r in rows] == [("norcar", "[5]", old)]

    def test_defers_when_budget_too_low(self, conn, monkeypatch):
        self._seed_hotspot(conn)
        monkeypatch.setattr(seasonality, "_sample_dates", lambda *a, **k: [dt.date(2026, 1, 15)])

        client = FakeClient(hourly_remaining=seasonality.RESERVED_CALLS)
        stats = compute_seasonality(conn, cast(EBirdClient, client))
        assert stats["regions_deferred"] == 1
        assert client.calls == []

    def test_drops_orphaned_regions(self, conn, monkeypatch):
        self._seed_hotspot(conn, region="US-CA")
        # Stale row for a region with no hotspot.
        old = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=1)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO species_seasonality(species_code, region, months, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            ("norcar", "US-NY", "[1]", old),
        )
        monkeypatch.setattr(seasonality, "_sample_dates", lambda *a, **k: [])

        compute_seasonality(conn, cast(EBirdClient, FakeClient()))
        regions = {r["region"] for r in conn.execute("SELECT DISTINCT region FROM species_seasonality")}
        assert "US-NY" not in regions
