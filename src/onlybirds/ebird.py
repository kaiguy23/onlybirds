import os
import time
from typing import Any

import httpx

BASE_URL = "https://api.ebird.org/v2"


class EBirdError(RuntimeError):
    pass


class EBirdClient:
    def __init__(self, api_key: str | None = None, timeout: float = 30.0) -> None:
        key = api_key or os.environ.get("EBIRD_API_KEY")
        if not key:
            raise EBirdError("EBIRD_API_KEY not set. Get one at https://ebird.org/api/keygen")
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"X-eBirdApiToken": key},
            timeout=timeout,
        )

    def __enter__(self) -> "EBirdClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self._client.close()

    def _get(self, path: str, *, timeout: float | None = None, **params: Any) -> Any:
        # crude retry for transient 5xx + 429 + read timeouts
        clean = {k: v for k, v in params.items() if v is not None}
        for attempt in range(3):
            try:
                r = self._client.get(path, params=clean, timeout=timeout) if timeout is not None \
                    else self._client.get(path, params=clean)
            except httpx.ReadTimeout:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise EBirdError(f"read timeout after 3 attempts: {path}")
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
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

    def nearby_hotspots(self, lat: float, lon: float, dist_km: int = 25, back: int = 30) -> list[dict[str, Any]]:
        return self._get("/ref/hotspot/geo", lat=lat, lng=lon, dist=dist_km, back=back, fmt="json")

    # --- observations ---
    def hotspot_recent(self, hotspot_id: str, back: int = 14, max_results: int = 200) -> list[dict[str, Any]]:
        return self._get(f"/data/obs/{hotspot_id}/recent", back=back, maxResults=max_results)

    def region_historic(
        self, region_code: str, year: int, month: int, day: int, max_results: int = 10000
    ) -> list[dict[str, Any]]:
        """All obs in a region on a specific date. Used for seasonality sampling.

        Use a county-level (subnational2) region — state-level can stall server-side
        and trigger ReadTimeout. We give this endpoint extra time anyway.
        """
        return self._get(
            f"/data/obs/{region_code}/historic/{year}/{month}/{day}",
            timeout=60.0,
            maxResults=max_results,
            cat="species",
        )

    def region_notable(self, region_code: str, back: int = 7, max_results: int = 200) -> list[dict[str, Any]]:
        """Rare-bird alerts for a region (subnational1 like 'US-CA' or country like 'US')."""
        return self._get(f"/data/obs/{region_code}/recent/notable", back=back, maxResults=max_results, detail="full")

    def geo_notable(self, lat: float, lon: float, dist_km: int = 50, back: int = 7) -> list[dict[str, Any]]:
        """Rare-bird alerts within radius of a point — better than region for an arbitrary location."""
        return self._get("/data/obs/geo/recent/notable", lat=lat, lng=lon, dist=dist_km, back=back, detail="full")
