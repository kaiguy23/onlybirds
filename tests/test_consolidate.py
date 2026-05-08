from __future__ import annotations

import datetime as dt

import pytest

from onlybirds.consolidate import (
    _complete_link_cluster,
    _haversine_km,
    consolidate_hotspots,
)


def _insert_hotspot(conn, hid: str, lat: float, lon: float, name: str | None = None) -> None:
    conn.execute(
        "INSERT INTO hotspots(hotspot_id, name, lat, lon, region, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (hid, name or hid, lat, lon, "US-CA", "2026-01-01T00:00:00"),
    )


def _insert_obs(conn, hotspot_id: str, species_codes: list[str]) -> None:
    for code in species_codes:
        conn.execute(
            "INSERT INTO hotspot_obs(hotspot_id, species_code, last_seen, how_many, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (hotspot_id, code, "2026-01-01", 1, "2026-01-01T00:00:00"),
        )


class TestHaversine:
    def test_zero_distance(self):
        assert _haversine_km(40.0, -73.0, 40.0, -73.0) == pytest.approx(0.0, abs=1e-9)

    def test_one_degree_lat(self):
        # 1° latitude ≈ 111 km
        assert _haversine_km(0.0, 0.0, 1.0, 0.0) == pytest.approx(111.19, abs=0.5)

    def test_symmetric(self):
        a = _haversine_km(40.7, -74.0, 34.0, -118.0)
        b = _haversine_km(34.0, -118.0, 40.7, -74.0)
        assert a == pytest.approx(b, abs=1e-9)


class TestCompleteLinkCluster:
    def test_empty(self):
        assert _complete_link_cluster([], 1.5) == []

    def test_singletons_when_far_apart(self):
        pts = [("a", 0.0, 0.0), ("b", 10.0, 10.0), ("c", -20.0, 30.0)]
        clusters = _complete_link_cluster(pts, 1.5)
        assert sorted(sorted(c) for c in clusters) == [["a"], ["b"], ["c"]]

    def test_merges_close_pair(self):
        # ~110 m apart
        pts = [("a", 40.0, -73.0), ("b", 40.001, -73.0)]
        clusters = _complete_link_cluster(pts, 1.5)
        assert len(clusters) == 1
        assert sorted(clusters[0]) == ["a", "b"]

    def test_complete_link_prevents_chain(self):
        # Three colinear points each ~1.0 km apart along longitude.
        # Single-link would chain a-b-c into one cluster (diameter ~2km > 1.5km radius).
        # Complete-link must NOT merge a with c.
        pts = [
            ("a", 40.0, -73.0),
            ("b", 40.0, -73.0 + 1.0 / 85.0),
            ("c", 40.0, -73.0 + 2.0 / 85.0),
        ]
        clusters = _complete_link_cluster(pts, 1.5)
        cluster_sets = [set(c) for c in clusters]
        # a and c should not be in the same cluster
        for cs in cluster_sets:
            assert not ({"a", "c"}.issubset(cs))

    def test_diameter_bounded(self):
        pts = [
            ("a", 40.0, -73.0),
            ("b", 40.0005, -73.0),
            ("c", 40.001, -73.0),
            ("d", 40.0015, -73.0),
        ]
        radius = 0.15  # ~150 m
        clusters = _complete_link_cluster(pts, radius)
        for c in clusters:
            for i, h1 in enumerate(c):
                for h2 in c[i + 1 :]:
                    p1 = next(p for p in pts if p[0] == h1)
                    p2 = next(p for p in pts if p[0] == h2)
                    assert _haversine_km(p1[1], p1[2], p2[1], p2[2]) <= radius + 1e-9


class TestConsolidateHotspots:
    def test_no_hotspots(self, conn):
        stats = consolidate_hotspots(conn)
        assert stats == {"hotspots": 0, "consolidated": 0, "members": 0, "radius_km": 1.5}

    def test_singletons_not_materialized(self, conn):
        _insert_hotspot(conn, "h1", 40.0, -73.0)
        _insert_hotspot(conn, "h2", 41.0, -74.0)
        stats = consolidate_hotspots(conn)
        assert stats["hotspots"] == 2
        assert stats["consolidated"] == 0
        rows = conn.execute("SELECT * FROM consolidated_hotspots").fetchall()
        assert rows == []

    def test_merges_cluster_and_writes_members(self, conn):
        _insert_hotspot(conn, "L99", 40.0, -73.0, name="Park A")
        _insert_hotspot(conn, "L01", 40.0005, -73.0, name="Park A North")
        _insert_obs(conn, "L99", ["norcar", "amerob"])
        _insert_obs(conn, "L01", ["norcar"])

        stats = consolidate_hotspots(conn)
        assert stats["consolidated"] == 1
        assert stats["members"] == 2

        cons = conn.execute("SELECT * FROM consolidated_hotspots").fetchall()
        assert len(cons) == 1
        # cid is "cons-" + min(member ids) — lexicographic min is "L01"
        assert cons[0]["consolidated_id"] == "cons-L01"
        # Anchor wins by max species count: L99 has 2, L01 has 1
        assert cons[0]["name"] == "Park A"
        assert cons[0]["member_count"] == 2
        # Centroid is the mean
        assert cons[0]["lat"] == pytest.approx((40.0 + 40.0005) / 2)

        members = conn.execute(
            "SELECT hotspot_id FROM consolidated_hotspot_members WHERE consolidated_id = ? "
            "ORDER BY hotspot_id",
            ("cons-L01",),
        ).fetchall()
        assert [m["hotspot_id"] for m in members] == ["L01", "L99"]

    def test_rerun_replaces_existing(self, conn):
        _insert_hotspot(conn, "L99", 40.0, -73.0)
        _insert_hotspot(conn, "L01", 40.0005, -73.0)
        consolidate_hotspots(conn)
        # Move L01 far away — should now be a singleton, no consolidation.
        conn.execute("UPDATE hotspots SET lat = 50.0, lon = 0.0 WHERE hotspot_id = 'L01'")
        stats = consolidate_hotspots(conn)
        assert stats["consolidated"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM consolidated_hotspots").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM consolidated_hotspot_members").fetchone()["c"] == 0

    def test_anchor_tiebreak_uses_shortest_name(self, conn):
        # Two hotspots, equal species counts (zero) → tiebreak on shortest name, then min id.
        _insert_hotspot(conn, "L02", 40.0, -73.0, name="Long Park Name Here")
        _insert_hotspot(conn, "L01", 40.0005, -73.0, name="Short")
        consolidate_hotspots(conn)
        cons = conn.execute("SELECT * FROM consolidated_hotspots").fetchone()
        assert cons["name"] == "Short"
