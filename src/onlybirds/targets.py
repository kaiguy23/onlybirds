"""Compute target birds: species seen at nearby hotspots that the user has not logged."""

import datetime as dt
import sqlite3


def compute_targets(conn: sqlite3.Connection) -> dict[str, int]:
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    # Reset rare flags on every run; rare.py repopulates them.
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
    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS c FROM targets").fetchone()["c"]
    return {"targets": n}
