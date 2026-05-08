"""Cross-check target birds against eBird rare-bird alerts from the last week."""

import sqlite3

from .ebird import EBirdClient, EBirdObservation


def mark_rare(
    conn: sqlite3.Connection,
    client: EBirdClient,
    lat: float,
    lon: float,
    radius_km: int = 50,
    days_back: int = 7,
) -> dict[str, int]:
    notable = client.geo_notable(lat=lat, lon=lon, dist_km=radius_km, back=days_back)

    # Index target species for quick membership check.
    target_codes = {
        r["species_code"] for r in conn.execute("SELECT species_code FROM targets")
    }

    # Pick the most recent notable record per species, restricted to target set.
    best: dict[str, EBirdObservation] = {}
    for r in notable:
        code = r.species_code
        if code not in target_codes:
            continue
        prev = best.get(code)
        if prev is None or (r.obs_dt or "") > (prev.obs_dt or ""):
            best[code] = r

    rows = [
        (
            rec.obs_dt,
            rec.lat,
            rec.lng,
            rec.loc_name,
            code,
        )
        for code, rec in best.items()
    ]
    conn.executemany(
        "UPDATE targets SET is_rare = 1, rare_seen_at = ?, rare_lat = ?, rare_lon = ?, rare_loc_name = ? "
        "WHERE species_code = ?",
        rows,
    )
    conn.commit()
    return {"notable_seen": len(notable), "rare_targets": len(rows)}
