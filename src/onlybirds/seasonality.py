"""Approximate species seasonality by sampling historical eBird obs.

eBird's clean seasonality data (bar charts) is gated behind an authenticated
session. Workaround: for each region we have hotspots in, sample a handful of
days per month over the past year via /data/obs/{region}/historic/{y}/{m}/{d}
and record which months each species shows up in.

Coarse — only "presence per month", no frequency. Cached 30 days because
seasonality doesn't shift run-to-run.

eBird caps keys at 500 requests per rolling hour, so a run only samples as many
regions as the remaining quota allows; the rest stay stale and get picked up
on a later run.
"""

import calendar
import datetime as dt
import json
import sqlite3
from collections import defaultdict

from tqdm import tqdm

from .ebird import EBirdClient, EBirdError, EBirdRateLimited

SAMPLE_DAYS = (15,)              # mid-month only — keeps a region at ~12 calls
MONTHS_HISTORY = 12              # look back this many months
SEASONALITY_TTL_DAYS = 30
# Hourly quota left untouched for later pipeline steps (rare-bird alerts is 1 call).
RESERVED_CALLS = 5


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

    deferred = 0
    for i, region in enumerate(to_fetch):
        remaining = client.hourly_remaining
        if (
            not client.wait_for_quota
            and remaining is not None
            and remaining < len(sample_dates) + RESERVED_CALLS
        ):
            deferred = len(to_fetch) - i
            tqdm.write(
                f"  ! seasonality: {remaining} eBird calls left this hour — "
                f"deferring {deferred} region(s) to a later run"
            )
            break
        species_months: dict[str, set[int]] = defaultdict(set)
        rate_limited = False
        for d in sample_dates:
            pbar.set_postfix_str(f"{region} {d.isoformat()}", refresh=False)
            try:
                obs = client.region_historic(region, d.year, d.month, d.day)
            except EBirdRateLimited:
                rate_limited = True
                break
            except EBirdError as e:
                # One slow date shouldn't blow up the whole pipeline — coarser
                # seasonality is better than none.
                skipped_dates += 1
                tqdm.write(f"  ! seasonality: skipping {region} {d.isoformat()} ({e})")
                pbar.update(1)
                continue
            api_calls += 1
            for o in obs:
                if o.species_code:
                    species_months[o.species_code].add(d.month)
            pbar.update(1)

        if rate_limited:
            # Partial data would be cached as fresh for 30 days; keep the old rows instead.
            deferred = len(to_fetch) - i
            tqdm.write(
                f"  ! seasonality: eBird hourly quota exhausted — "
                f"deferring {deferred} region(s) to a later run"
            )
            break

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
        "regions_deferred": deferred,
        "api_calls": api_calls,
        "sample_dates_per_region": len(sample_dates),
        "skipped_dates": skipped_dates,
    }
