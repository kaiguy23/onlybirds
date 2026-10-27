"""Compute target birds: species seen at nearby hotspots that the user has not logged."""

import datetime as dt
import sqlite3


def compute_targets(conn: sqlite3.Connection, keep_rare: bool = False) -> dict[str, int]:
    """Rebuild targets from hotspot obs minus the life list.

    A full pipeline run resets rare flags and rare.py repopulates them. With
    keep_rare (a life-list-only sync, which doesn't call rare.py), species that
    stay targets keep their existing rare-alert fields.
    """
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    rare_rows = (
        conn.execute(
            "SELECT rare_seen_at, rare_lat, rare_lon, rare_loc_name, species_code FROM targets WHERE is_rare = 1"
        ).fetchall()
        if keep_rare
        else []
    )
    conn.execute("DELETE FROM targets")
    conn.execute(
        """
        INSERT INTO targets(species_code, first_flagged, is_rare)
        SELECT DISTINCT ho.species_code, ?, 0
        FROM hotspot_obs ho
        WHERE ho.species_code NOT IN (SELECT species_code FROM observations)
        """,
        (now,),
    )
    conn.executemany(
        "UPDATE targets SET is_rare = 1, rare_seen_at = ?, rare_lat = ?, rare_lon = ?, rare_loc_name = ? "
        "WHERE species_code = ?",
        [tuple(r) for r in rare_rows],
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS c FROM targets").fetchone()["c"]
    return {"targets": n}
