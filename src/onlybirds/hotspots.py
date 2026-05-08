"""Fetch nearby hotspots and their recent observations.

Cache model: hotspots and hotspot_obs accumulate across runs/locations. On each
run we always fetch the *list* of nearby hotspots (cheap — one API call), then
only re-fetch *observations* for hotspots whose obs are stale.

When we do re-fetch a single hotspot's obs, we replace its rows — so a species
that has migrated through and is no longer being reported drops out of targets.
"""

import datetime as dt
import sqlite3

from tqdm import tqdm

from .ebird import EBirdClient

HOTSPOT_OBS_TTL_HOURS = 6


def _stale_hotspot_ids(conn: sqlite3.Connection, hotspot_ids: list[str], ttl_hours: int) -> set[str]:
    """Return ids that have no fresh obs cache (never fetched OR older than TTL)."""
    if not hotspot_ids:
        return set()
    placeholders = ",".join("?" * len(hotspot_ids))
    cutoff = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    rows = conn.execute(
        f"SELECT hotspot_id, MAX(fetched_at) AS f FROM hotspot_obs "
        f"WHERE hotspot_id IN ({placeholders}) GROUP BY hotspot_id",
        hotspot_ids,
    ).fetchall()
    fresh = {r["hotspot_id"] for r in rows if r["f"] and r["f"] > cutoff}
    return set(hotspot_ids) - fresh


def fetch_nearby(
    conn: sqlite3.Connection,
    client: EBirdClient,
    lat: float,
    lon: float,
    radius_km: int = 25,
    days_back: int = 14,
    force: bool = False,
) -> dict[str, int]:
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    nearby = client.nearby_hotspots(lat, lon, dist_km=radius_km, back=days_back)

    # Upsert every nearby hotspot — accumulate across runs.
    conn.executemany(
        "INSERT OR REPLACE INTO hotspots(hotspot_id, name, lat, lon, region, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                h.loc_id,
                h.name,
                h.lat,
                h.lng,
                h.subnational2_code or h.subnational1_code or h.country_code,
                now,
            )
            for h in nearby
        ],
    )

    nearby_ids = [h.loc_id for h in nearby]
    if force:
        to_refresh = set(nearby_ids)
    else:
        to_refresh = _stale_hotspot_ids(conn, nearby_ids, HOTSPOT_OBS_TTL_HOURS)

    refreshed = 0
    new_obs = 0
    for hotspot_id in tqdm(
        sorted(to_refresh), desc="  hotspot obs", unit="hs", leave=False, disable=not to_refresh
    ):
        recent = client.hotspot_recent(hotspot_id, back=days_back)
        # Per-hotspot replace: drop stale species so migrated-through birds expire.
        conn.execute("DELETE FROM hotspot_obs WHERE hotspot_id = ?", (hotspot_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO hotspot_obs(hotspot_id, species_code, last_seen, how_many, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (hotspot_id, r.species_code, r.obs_dt, r.how_many, now)
                for r in recent
            ],
        )
        refreshed += 1
        new_obs += len(recent)

    conn.commit()
    total_hotspots = conn.execute("SELECT COUNT(*) AS c FROM hotspots").fetchone()["c"]
    total_obs = conn.execute("SELECT COUNT(*) AS c FROM hotspot_obs").fetchone()["c"]
    return {
        "nearby": len(nearby),
        "refreshed": refreshed,
        "cached": len(nearby) - refreshed,
        "total_hotspots": total_hotspots,
        "total_obs": total_obs,
    }
