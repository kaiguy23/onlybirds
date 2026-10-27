"""Fetch nearby hotspots and their recent observations.

Cache model: hotspots and hotspot_obs accumulate across runs/locations. On each
run we always fetch the *list* of nearby hotspots (cheap — one API call), then
only re-fetch *observations* for hotspots whose obs are stale.

When we do re-fetch a single hotspot's obs, we replace its rows — so a species
that has migrated through and is no longer being reported drops out of targets.

Each hotspot costs one call against eBird's 500/hour quota, so a run refreshes
the stalest hotspots first, commits as it goes, and stops while some quota is
left for later steps; the rest stay stale and get picked up on a later run.
"""

import datetime as dt
import sqlite3

from tqdm import tqdm

from .ebird import EBirdClient, EBirdRateLimited

# Obs are already a 14-day window, so a day-old snapshot is close enough and
# costs a quarter of the calls a 6h TTL did.
HOTSPOT_OBS_TTL_HOURS = 24
# Hourly quota left untouched for later pipeline steps (a few seasonality
# regions + rare-bird alerts).
RESERVED_CALLS = 30
COMMIT_EVERY = 25


def _stale_hotspot_ids(conn: sqlite3.Connection, hotspot_ids: list[str], ttl_hours: int) -> list[str]:
    """Return ids with no fresh obs cache (never fetched OR older than TTL), stalest first."""
    if not hotspot_ids:
        return []
    placeholders = ",".join("?" * len(hotspot_ids))
    cutoff = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    rows = conn.execute(
        f"SELECT hotspot_id, MAX(fetched_at) AS f FROM hotspot_obs "
        f"WHERE hotspot_id IN ({placeholders}) GROUP BY hotspot_id",
        hotspot_ids,
    ).fetchall()
    fetched = {r["hotspot_id"]: r["f"] or "" for r in rows}
    # Never-fetched hotspots sort first (""), then oldest fetch first.
    return sorted(
        (h for h in dict.fromkeys(hotspot_ids) if fetched.get(h, "") <= cutoff),
        key=lambda h: (fetched.get(h, ""), h),
    )


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
        to_refresh = list(dict.fromkeys(nearby_ids))
    else:
        to_refresh = _stale_hotspot_ids(conn, nearby_ids, HOTSPOT_OBS_TTL_HOURS)
    conn.commit()

    refreshed = 0
    new_obs = 0
    deferred = 0
    for i, hotspot_id in enumerate(
        tqdm(to_refresh, desc="  hotspot obs", unit="hs", leave=False, disable=not to_refresh)
    ):
        remaining = client.hourly_remaining
        if not client.wait_for_quota and remaining is not None and remaining < RESERVED_CALLS:
            deferred = len(to_refresh) - i
            tqdm.write(
                f"  ! hotspot obs: {remaining} eBird calls left this hour — "
                f"deferring {deferred} hotspot(s) to a later run"
            )
            break
        try:
            recent = client.hotspot_recent(hotspot_id, back=days_back)
        except EBirdRateLimited:
            deferred = len(to_refresh) - i
            tqdm.write(
                f"  ! hotspot obs: eBird hourly quota exhausted — "
                f"deferring {deferred} hotspot(s) to a later run"
            )
            break
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
        # Keep progress if a later call fails or the run is killed.
        if refreshed % COMMIT_EVERY == 0:
            conn.commit()

    conn.commit()
    total_hotspots = conn.execute("SELECT COUNT(*) AS c FROM hotspots").fetchone()["c"]
    total_obs = conn.execute("SELECT COUNT(*) AS c FROM hotspot_obs").fetchone()["c"]
    return {
        "nearby": len(nearby),
        "refreshed": refreshed,
        "cached": len(nearby) - refreshed - deferred,
        "deferred": deferred,
        "total_hotspots": total_hotspots,
        "total_obs": total_obs,
    }
