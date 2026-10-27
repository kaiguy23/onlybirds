import json

import httpx
import pytest

from onlybirds import trip


def _mock(handler) -> httpx.Client:
    return httpx.Client(base_url=trip.GITHUB_API, transport=httpx.MockTransport(handler))


def test_geocode_best_hit(monkeypatch):
    monkeypatch.setenv("ONLYBIRDS_CONTACT", "me@example.com")
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=[{"display_name": "Austin, Texas", "lat": "30.27", "lon": "-97.74"}])

    p = trip.geocode("Austin, TX", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert p == trip.Place("Austin, Texas", 30.27, -97.74)
    assert seen[0].url.params["q"] == "Austin, TX"
    assert "me@example.com" in seen[0].headers["user-agent"]


def test_geocode_no_match():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    with pytest.raises(trip.TripError, match="no place"):
        trip.geocode("Nowhereville", client=c)


def test_geocode_blank():
    with pytest.raises(trip.TripError):
        trip.geocode("  ")


def test_dispatch_posts_inputs():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(204)

    trip.dispatch("t", lat=30.267153, lon=-97.743061, radius_km=40, label="Austin", client=_mock(handler))
    assert seen[0].url.path == "/repos/kaiguy23/onlybirds/actions/workflows/trip.yml/dispatches"
    body = json.loads(seen[0].content)
    assert body == {"ref": "main", "inputs": {"lat": "30.26715", "lon": "-97.74306", "radius": "40", "label": "Austin"}}


def test_dispatch_refused():
    with pytest.raises(trip.TripError, match="403"):
        trip.dispatch("t", lat=0, lon=0, radius_km=10, label="x", client=_mock(lambda r: httpx.Response(403, text="nope")))


def test_recent_runs():
    payload = {"workflow_runs": [{"display_title": "trip: Austin", "status": "completed", "conclusion": "success",
                                  "html_url": "https://github.com/x", "created_at": "2026-10-27T00:00:00Z"}]}
    runs = trip.recent_runs("t", client=_mock(lambda r: httpx.Response(200, json=payload)))
    assert runs == [trip.TripRun("trip: Austin", "completed", "success", "https://github.com/x", "2026-10-27T00:00:00Z")]
