from typing import cast

from onlybirds import hotspots
from onlybirds.ebird import EBirdClient, EBirdHotspot, EBirdObservation, EBirdRateLimited
from onlybirds.hotspots import _stale_hotspot_ids, fetch_nearby


def _hs(loc_id: str) -> EBirdHotspot:
    return EBirdHotspot.from_api({"locId": loc_id, "locName": loc_id, "lat": 30.0, "lng": -97.0, "subnational2Code": "US-TX-453"})


class FakeClient:
    """nearby_hotspots + hotspot_recent, with a per-call quota countdown."""

    def __init__(self, ids: list[str], hourly_remaining: int | None = None, fail_after: int | None = None):
        self.ids = ids
        self.hourly_remaining = hourly_remaining
        self.wait_for_quota = False
        self.fail_after = fail_after
        self.calls: list[str] = []

    def nearby_hotspots(self, lat, lon, dist_km=25, back=30):
        return [_hs(i) for i in self.ids]

    def hotspot_recent(self, hotspot_id, back=14, max_results=200):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise EBirdRateLimited("quota")
        self.calls.append(hotspot_id)
        if self.hourly_remaining is not None:
            self.hourly_remaining -= 1
        return [EBirdObservation.from_api({"speciesCode": "amerob", "obsDt": "2026-10-20"})]


def _seed_obs(conn, hotspot_id: str, fetched_at: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO hotspots(hotspot_id, name, lat, lon, region, fetched_at) VALUES (?, ?, 0, 0, '', ?)",
        (hotspot_id, hotspot_id, fetched_at),
    )
    conn.execute(
        "INSERT INTO hotspot_obs(hotspot_id, species_code, last_seen, how_many, fetched_at) VALUES (?, 'x', '', 1, ?)",
        (hotspot_id, fetched_at),
    )


def test_stale_ids_never_fetched_first_then_oldest(conn):
    _seed_obs(conn, "L2", "2000-01-02T00:00:00")
    _seed_obs(conn, "L1", "2000-01-01T00:00:00")
    _seed_obs(conn, "L9", "2999-01-01T00:00:00")  # fresh
    assert _stale_hotspot_ids(conn, ["L1", "L2", "L3", "L9", "L3"], ttl_hours=24) == ["L3", "L1", "L2"]


def test_defers_when_quota_runs_low(conn, monkeypatch):
    monkeypatch.setattr(hotspots, "RESERVED_CALLS", 3)
    c = FakeClient([f"L{i}" for i in range(10)], hourly_remaining=6)
    stats = fetch_nearby(conn, cast(EBirdClient, c), 30.0, -97.0)
    # 6, 5, 4, 3 left → fetch; 2 left is under the reserve → stop.
    assert len(c.calls) == 4
    assert stats["refreshed"] == 4
    assert stats["deferred"] == 6


def test_rate_limited_keeps_earlier_progress(conn):
    c = FakeClient([f"L{i}" for i in range(5)], fail_after=2)
    stats = fetch_nearby(conn, cast(EBirdClient, c), 30.0, -97.0)
    assert stats == {**stats, "refreshed": 2, "deferred": 3}
    conn.rollback()  # anything not committed is lost, as when the run dies
    n = conn.execute("SELECT COUNT(DISTINCT hotspot_id) AS c FROM hotspot_obs").fetchone()["c"]
    assert n == 2
