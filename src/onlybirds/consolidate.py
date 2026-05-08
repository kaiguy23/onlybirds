"""Consolidate nearby hotspots into spatial clusters.

Some eBird hotspots cover the same physical area (multiple markers for one
park, marsh, or trail). This stage groups any hotspots whose pairwise
distance stays within `radius_km` (complete-link agglomerative clustering)
into a single 'consolidated hotspot'. Singletons (clusters of size 1) aren't
materialized — only clusters of 2+ become rows in `consolidated_hotspots`.

We use complete-link rather than single-link to avoid 'chaining' (where a
ribbon of nearby hotspots — e.g. along a river or coastline — gets merged
into one mega-cluster). Complete-link guarantees every cluster's diameter
is ≤ radius_km.

The dashboard hides member hotspots from the map and shows a consolidated
marker instead; clicking it reveals the originals and a deduplicated species
list.
"""

from __future__ import annotations

import datetime as dt
import math
import sqlite3

DEFAULT_RADIUS_KM = 1.5
EARTH_RADIUS_KM = 6371.0088


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _complete_link_cluster(
    pts: list[tuple[str, float, float]], radius_km: float
) -> list[list[str]]:
    """Agglomerative complete-link clustering on great-circle distance.

    Returns a list of clusters; each is a list of hotspot ids. Diameter of
    every cluster is ≤ radius_km.
    """
    n = len(pts)
    if n == 0:
        return []
    # Pairwise within radius — limit candidate pairs via a coarse grid.
    cell_deg = radius_km / 111.0
    mean_lat = sum(p[1] for p in pts) / n
    cell_lon = cell_deg / max(math.cos(math.radians(mean_lat)), 0.1)
    grid: dict[tuple[int, int], list[int]] = {}
    for i, (_, lat, lon) in enumerate(pts):
        grid.setdefault((int(lat / cell_deg), int(lon / cell_lon)), []).append(i)

    edges: list[tuple[float, int, int]] = []
    for i, (_, lat_i, lon_i) in enumerate(pts):
        cy, cx = int(lat_i / cell_deg), int(lon_i / cell_lon)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for j in grid.get((cy + dy, cx + dx), ()):
                    if j <= i:
                        continue
                    d = _haversine_km(lat_i, lon_i, pts[j][1], pts[j][2])
                    if d <= radius_km:
                        edges.append((d, i, j))
    edges.sort()

    # Each cluster is a list of point indices; merge via owner pointers so
    # cluster lookup is O(α(n)) per call.
    owner = list(range(n))
    members: list[list[int]] = [[i] for i in range(n)]

    def root(x: int) -> int:
        while owner[x] != x:
            owner[x] = owner[owner[x]]
            x = owner[x]
        return x

    for _, i, j in edges:
        ci, cj = root(i), root(j)
        if ci == cj:
            continue
        # Complete-link admissibility: every cross-pair must already be ≤ radius.
        ok = True
        for x in members[ci]:
            lat_x, lon_x = pts[x][1], pts[x][2]
            for y in members[cj]:
                if _haversine_km(lat_x, lon_x, pts[y][1], pts[y][2]) > radius_km:
                    ok = False
                    break
            if not ok:
                break
        if not ok:
            continue
        # Merge cj into ci (union by size to keep tree shallow).
        if len(members[ci]) < len(members[cj]):
            ci, cj = cj, ci
        members[ci].extend(members[cj])
        owner[cj] = ci
        members[cj] = []

    return [[pts[i][0] for i in m] for m in members if m]


def consolidate_hotspots(
    conn: sqlite3.Connection,
    radius_km: float = DEFAULT_RADIUS_KM,
) -> dict[str, float]:
    rows = conn.execute(
        "SELECT hotspot_id, name, lat, lon FROM hotspots"
    ).fetchall()

    # Replace existing consolidations every run — cheap and avoids stale state
    # if hotspots were added/removed since the last run.
    conn.execute("DELETE FROM consolidated_hotspot_members")
    conn.execute("DELETE FROM consolidated_hotspots")
    if not rows:
        conn.commit()
        return {"hotspots": 0, "consolidated": 0, "members": 0, "radius_km": radius_km}

    pts = [(r["hotspot_id"], r["lat"], r["lon"]) for r in rows]
    raw_clusters = _complete_link_cluster(pts, radius_km)
    clusters: dict[str, list[str]] = {
        # Keyed by min-id so cluster ids are stable across reruns.
        min(members): members
        for members in raw_clusters
    }

    by_id = {r["hotspot_id"]: r for r in rows}
    species_counts = dict(
        conn.execute(
            "SELECT hotspot_id, COUNT(*) FROM hotspot_obs GROUP BY hotspot_id"
        ).fetchall()
    )
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    n_consolidated = 0
    n_members = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        # Deterministic id: sorted-min member id keeps the cluster stable across
        # runs as long as membership is unchanged.
        cid = "cons-" + min(members)
        lat_c = sum(by_id[m]["lat"] for m in members) / len(members)
        lon_c = sum(by_id[m]["lon"] for m in members) / len(members)
        # Member with the most species observed wins the label — the "anchor"
        # hotspot of the cluster. Shortest name breaks ties, then min id for
        # determinism when species counts are missing/equal.
        anchor = min(
            members,
            key=lambda m: (-species_counts.get(m, 0), len(by_id[m]["name"] or m), m),
        )
        name = by_id[anchor]["name"] or anchor
        conn.execute(
            "INSERT INTO consolidated_hotspots(consolidated_id, name, lat, lon, member_count, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cid, name, lat_c, lon_c, len(members), now),
        )
        conn.executemany(
            "INSERT INTO consolidated_hotspot_members(consolidated_id, hotspot_id) VALUES (?, ?)",
            [(cid, m) for m in members],
        )
        n_consolidated += 1
        n_members += len(members)

    conn.commit()
    return {
        "hotspots": len(rows),
        "consolidated": n_consolidated,
        "members": n_members,
        "radius_km": radius_km,
    }
