import os
import re
import time
from dataclasses import dataclass
from typing import Any, Self

import httpx
from tqdm import tqdm

BASE_URL = "https://api.ebird.org/v2"
# The hourly window rolls, so quota trickles back as old requests age out.
QUOTA_POLL_S = 60.0
QUOTA_MAX_WAIT_S = 90 * 60.0


class EBirdError(RuntimeError):
    pass


class EBirdRateLimited(EBirdError):
    """Hourly request quota is exhausted; retrying before the window rolls is pointless."""


# eBird sends e.g. `ratelimit: "policy";r=0,"burst";r=25` — `policy` is the
# 500/hour quota, `burst` is 25 per 5s.
_HOURLY_REMAINING_RE = re.compile(r'"policy";r=(\d+)')


def _hourly_remaining(headers: httpx.Headers) -> int | None:
    m = _HOURLY_REMAINING_RE.search(headers.get("ratelimit", ""))
    return int(m.group(1)) if m else None


def _retry_after(headers: httpx.Headers, default: float) -> float:
    try:
        return float(headers["retry-after"])
    except (KeyError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class EBirdHotspot:
    loc_id: str
    name: str | None
    lat: float
    lng: float
    country_code: str | None
    subnational1_code: str | None
    subnational2_code: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Self:
        return cls(
            loc_id=raw["locId"],
            name=raw.get("locName"),
            lat=raw["lat"],
            lng=raw["lng"],
            country_code=raw.get("countryCode"),
            subnational1_code=raw.get("subnational1Code"),
            subnational2_code=raw.get("subnational2Code"),
        )


@dataclass(frozen=True, slots=True)
class EBirdObservation:
    species_code: str
    common_name: str | None
    sci_name: str | None
    obs_dt: str | None
    how_many: int | None
    loc_id: str | None
    loc_name: str | None
    lat: float | None
    lng: float | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Self:
        return cls(
            species_code=raw["speciesCode"],
            common_name=raw.get("comName"),
            sci_name=raw.get("sciName"),
            obs_dt=raw.get("obsDt"),
            how_many=raw.get("howMany"),
            loc_id=raw.get("locId"),
            loc_name=raw.get("locName"),
            lat=raw.get("lat"),
            lng=raw.get("lng"),
        )


class EBirdClient:
    def __init__(self, api_key: str | None = None, timeout: float = 30.0, wait_for_quota: bool = False) -> None:
        key = api_key or os.environ.get("EBIRD_API_KEY")
        if not key:
            raise EBirdError("EBIRD_API_KEY not set. Get one at https://ebird.org/api/keygen")
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"X-eBirdApiToken": key},
            timeout=timeout,
        )
        # Requests left in the rolling hourly window, as of the last response.
        # None until the first response that carries a `ratelimit` header.
        self.hourly_remaining: int | None = None
        # Block until the hourly quota recovers instead of raising EBirdRateLimited.
        self.wait_for_quota = wait_for_quota

    def __enter__(self) -> "EBirdClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self._client.close()

    def _get(self, path: str, *, timeout: float | None = None, **params: Any) -> Any:
        # crude retry for transient 5xx + burst 429 + read timeouts
        clean = {k: v for k, v in params.items() if v is not None}
        attempt = -1
        quota_waited = 0.0
        while attempt < 2:
            attempt += 1
            try:
                r = self._client.get(path, params=clean, timeout=timeout) if timeout is not None \
                    else self._client.get(path, params=clean)
            except httpx.ReadTimeout:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise EBirdError(f"read timeout after 3 attempts: {path}")
            remaining = _hourly_remaining(r.headers)
            if remaining is not None:
                self.hourly_remaining = remaining
            if r.status_code == 429:
                if remaining == 0:
                    if not self.wait_for_quota or quota_waited >= QUOTA_MAX_WAIT_S:
                        raise EBirdRateLimited(f"429 {path}: hourly eBird quota exhausted")
                    if quota_waited == 0:
                        tqdm.write("  … eBird hourly quota exhausted; waiting for it to recover")
                    time.sleep(QUOTA_POLL_S)
                    quota_waited += QUOTA_POLL_S
                    attempt -= 1
                    continue
                if attempt < 2:
                    time.sleep(_retry_after(r.headers, 2 ** attempt))
                    continue
            if r.status_code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise EBirdError(f"{r.status_code} {path}: {r.text[:200]}")
            return r.json()
        raise EBirdError(f"exhausted retries for {path}")

    # --- reference ---
    def taxonomy(self, locale: str = "en") -> list[dict[str, Any]]:
        """Full eBird taxonomy. ~17K rows, JSON. Cache aggressively."""
        return self._get("/ref/taxonomy/ebird", fmt="json", locale=locale)

    def nearby_hotspots(self, lat: float, lon: float, dist_km: int = 25, back: int = 30) -> list[EBirdHotspot]:
        raw = self._get("/ref/hotspot/geo", lat=lat, lng=lon, dist=dist_km, back=back, fmt="json")
        return [EBirdHotspot.from_api(h) for h in raw]

    # --- observations ---
    def hotspot_recent(self, hotspot_id: str, back: int = 14, max_results: int = 200) -> list[EBirdObservation]:
        raw = self._get(f"/data/obs/{hotspot_id}/recent", back=back, maxResults=max_results)
        return [EBirdObservation.from_api(o) for o in raw]

    def region_historic(
        self, region_code: str, year: int, month: int, day: int, max_results: int = 10000
    ) -> list[EBirdObservation]:
        """All obs in a region on a specific date. Used for seasonality sampling.

        Use a county-level (subnational2) region — state-level can stall server-side
        and trigger ReadTimeout. We give this endpoint extra time anyway.
        """
        raw = self._get(
            f"/data/obs/{region_code}/historic/{year}/{month}/{day}",
            timeout=60.0,
            maxResults=max_results,
            cat="species",
        )
        return [EBirdObservation.from_api(o) for o in raw]

    def region_notable(self, region_code: str, back: int = 7, max_results: int = 200) -> list[EBirdObservation]:
        """Rare-bird alerts for a region (subnational1 like 'US-CA' or country like 'US')."""
        raw = self._get(f"/data/obs/{region_code}/recent/notable", back=back, maxResults=max_results, detail="full")
        return [EBirdObservation.from_api(o) for o in raw]

    def geo_notable(self, lat: float, lon: float, dist_km: int = 50, back: int = 7) -> list[EBirdObservation]:
        """Rare-bird alerts within radius of a point — better than region for an arbitrary location."""
        raw = self._get("/data/obs/geo/recent/notable", lat=lat, lng=lon, dist=dist_km, back=back, detail="full")
        return [EBirdObservation.from_api(o) for o in raw]
