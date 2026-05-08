"""Approximate species seasonality by sampling historical eBird obs.

eBird's clean seasonality data (bar charts) is gated behind an authenticated
session. Workaround: for each region we have hotspots in, sample a handful of
days per month over the past year via /data/obs/{region}/historic/{y}/{m}/{d}
and record which months each species shows up in.

Coarse — only "presence per month", no frequency. Cached 30 days because
seasonality doesn't shift run-to-run.
"""

import calendar
import datetime as dt
import json
import sqlite3
from collections import defaultdict

from tqdm import tqdm

from .ebird import EBirdClient, EBirdError

SAMPLE_DAYS = (5, 15, 25)        # 3 evenly-spaced days per month
MONTHS_HISTORY = 12              # look back this many months
SEASONALITY_TTL_DAYS = 30


def _sample_dates(end_date: dt.date, n_months: int = MONTHS_HISTORY) -> list[dt.date]:
    today = dt.date.today()
    out: list[dt.date] = []
    for offset in range(n_months):
        y, m = end_date.year, end_date.month - offset
        while m <= 0:
            y -= 1
            m += 12
        last_day = calendar.monthrange(y, m)[1]
        for d in SAMPLE_DAYS:
            sample = dt.date(y, m, min(d, last_day))
            if sample < today:
                out.append(sample)
    return out


def _is_fresh(conn: sqlite3.Connection, region: str, ttl_days: int) -> bool:
    row = conn.execute(
        "SELECT MAX(fetched_at) AS f FROM species_seasonality WHERE region = ?", (region,)
    ).fetchone()
    if not row or not row["f"]:
        return False
    age = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.datetime.fromisoformat(row["f"])).days
    return age < ttl_days


def compute_seasonality(
    conn: sqlite3.Connection,
    client: EBirdClient,
    force: bool = False,
) -> dict[str, int]:
    regions = [
        r["region"]
        for r in conn.execute(
            "SELECT DISTINCT region FROM hotspots WHERE region IS NOT NULL AND region != ''"
        )
    ]

    api_calls = 0
    fetched = 0
    skipped_dates = 0
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    today = dt.date.today()
    sample_dates = _sample_dates(today)

    to_fetch = [r for r in regions if force or not _is_fresh(conn, r, SEASONALITY_TTL_DAYS)]
    cached = len(regions) - len(to_fetch)
    pbar = tqdm(
        total=len(to_fetch) * len(sample_dates),
        desc="  seasonality",
        unit="call",
        leave=False,
        disable=not to_fetch,
    )

    for region in to_fetch:
        species_months: dict[str, set[int]] = defaultdict(set)
        for d in sample_dates:
            pbar.set_postfix_str(f"{region} {d.isoformat()}", refresh=False)
            try:
                obs = client.region_historic(region, d.year, d.month, d.day)
            except EBirdError as e:
                # One slow date shouldn't blow up the whole pipeline — coarser
                # seasonality is better than none.
                skipped_dates += 1
                tqdm.write(f"  ! seasonality: skipping {region} {d.isoformat()} ({e})")
                pbar.update(1)
                continue
            api_calls += 1
            for o in obs:
                code = o.get("speciesCode")
                if code:
                    species_months[code].add(d.month)
            pbar.update(1)

        conn.execute("DELETE FROM species_seasonality WHERE region = ?", (region,))
        conn.executemany(
            "INSERT INTO species_seasonality(species_code, region, months, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            [
                (code, region, json.dumps(sorted(months)), now)
                for code, months in species_months.items()
            ],
        )
        fetched += 1

    pbar.close()

    # Drop orphaned cache rows — after switching region scope (e.g. state→county),
    # old entries are unreachable and just take up space.
    conn.execute(
        "DELETE FROM species_seasonality "
        "WHERE region NOT IN (SELECT region FROM hotspots WHERE region IS NOT NULL)"
    )

    conn.commit()
    return {
        "regions_fetched": fetched,
        "regions_cached": cached,
        "api_calls": api_calls,
        "sample_dates_per_region": len(sample_dates),
        "skipped_dates": skipped_dates,
    }
