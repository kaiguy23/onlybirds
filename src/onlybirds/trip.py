"""Plan a trip: turn a city into coordinates and start a refresh in GitHub Actions.

The refresh itself is `.github/workflows/trip.yml`, which runs the pipeline for
the given point (waiting out eBird's hourly quota) and commits the DB; Streamlit
Cloud redeploys on that push. This module is the dashboard's and CLI's way in:

- `geocode` resolves a free-text place ("Austin, TX", "Golden Gate Park").
  With GOOGLE_MAPS_API_KEY set it uses Google's Geocoding API, which matches
  landmarks and loose spellings better; otherwise OpenStreetMap's Nominatim
  (no key; its policy asks for an identifying User-Agent and at most one
  request per second — fine for a form you submit by hand).
- `dispatch` and `recent_runs` call the GitHub REST API with a fine-grained
  token that only needs "Actions: read and write" on this repo.
"""

import os
from dataclasses import dataclass
from typing import Any

import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "kaiguy23/onlybirds"
WORKFLOW_FILE = "trip.yml"
LIFELIST_WORKFLOW_FILE = "lifelist.yml"


class TripError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class TripRun:
    title: str
    status: str  # queued | in_progress | completed
    conclusion: str | None  # success | failure | cancelled | … once completed
    url: str
    created_at: str


def _user_agent() -> str:
    """Nominatim rejects generic UAs; include ONLYBIRDS_CONTACT like the Wikipedia client."""
    contact = (os.environ.get("ONLYBIRDS_CONTACT") or "").strip()
    return f"onlybirds/0.1 ({contact})" if contact else "onlybirds/0.1"


def geocode(query: str, client: httpx.Client | None = None, timeout: float = 15.0) -> Place:
    """Best match for a place name, or TripError if nothing matches."""
    query = query.strip()
    if not query:
        raise TripError("enter a city or place name")
    google_key = (os.environ.get("GOOGLE_MAPS_API_KEY") or "").strip()
    if google_key:
        return _geocode_google(query, google_key, client, timeout)
    c = client or httpx.Client(timeout=timeout)
    try:
        r = c.get(
            NOMINATIM_URL,
            params={"q": query, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": _user_agent(), "Accept-Language": "en"},
        )
    finally:
        if client is None:
            c.close()
    if r.status_code >= 400:
        raise TripError(f"geocoding failed ({r.status_code})")
    hits = r.json()
    if not hits:
        raise TripError(f"no place found for {query!r}")
    h = hits[0]
    return Place(name=h.get("display_name") or query, lat=float(h["lat"]), lon=float(h["lon"]))


def _geocode_google(query: str, key: str, client: httpx.Client | None, timeout: float) -> Place:
    c = client or httpx.Client(timeout=timeout)
    try:
        r = c.get(GOOGLE_GEOCODE_URL, params={"address": query, "key": key, "language": "en"})
    finally:
        if client is None:
            c.close()
    if r.status_code >= 400:
        raise TripError(f"geocoding failed ({r.status_code})")
    body = r.json()
    status = body.get("status")
    if status == "ZERO_RESULTS":
        raise TripError(f"no place found for {query!r}")
    if status != "OK":
        # REQUEST_DENIED / OVER_QUERY_LIMIT etc. come back as 200 with an error_message.
        raise TripError(f"Google geocoding: {status} {body.get('error_message', '')}".strip())
    h = body["results"][0]
    loc = h["geometry"]["location"]
    return Place(name=h.get("formatted_address") or query, lat=float(loc["lat"]), lon=float(loc["lng"]))


def _github(token: str, client: httpx.Client | None) -> httpx.Client:
    return client or httpx.Client(
        base_url=GITHUB_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15.0,
    )


def dispatch(
    token: str,
    *,
    lat: float,
    lon: float,
    radius_km: int,
    label: str,
    repo: str = DEFAULT_REPO,
    ref: str = "main",
    client: httpx.Client | None = None,
) -> None:
    """Start the trip workflow for one point."""
    inputs = {"lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "radius": str(radius_km), "label": label}
    _dispatch(token, WORKFLOW_FILE, inputs, repo=repo, ref=ref, client=client)


def dispatch_lifelist(
    token: str, *, repo: str = DEFAULT_REPO, ref: str = "main", client: httpx.Client | None = None
) -> None:
    """Start the workflow that pulls your latest eBird life list into the DB."""
    _dispatch(token, LIFELIST_WORKFLOW_FILE, {}, repo=repo, ref=ref, client=client)


def _dispatch(
    token: str, workflow: str, inputs: dict[str, str], *, repo: str, ref: str, client: httpx.Client | None
) -> None:
    # GitHub answers 204 with no run id; recent_runs() picks the run up a moment later.
    c = _github(token, client)
    try:
        r = c.post(f"/repos/{repo}/actions/workflows/{workflow}/dispatches", json={"ref": ref, "inputs": inputs})
    finally:
        if client is None:
            c.close()
    if r.status_code != 204:
        raise TripError(f"GitHub refused the run ({r.status_code}): {r.text[:200]}")


def recent_runs(
    token: str, repo: str = DEFAULT_REPO, limit: int = 5, client: httpx.Client | None = None
) -> list[TripRun]:
    """Latest manually started runs (trips and life-list pulls), newest first."""
    c = _github(token, client)
    try:
        r = c.get(f"/repos/{repo}/actions/runs", params={"event": "workflow_dispatch", "per_page": limit})
    finally:
        if client is None:
            c.close()
    if r.status_code >= 400:
        raise TripError(f"couldn't list runs ({r.status_code})")
    runs: list[dict[str, Any]] = r.json().get("workflow_runs", [])
    return [
        TripRun(
            title=run.get("display_title") or run.get("name") or "trip",
            status=run.get("status") or "",
            conclusion=run.get("conclusion"),
            url=run.get("html_url") or "",
            created_at=run.get("created_at") or "",
        )
        for run in runs
    ]
