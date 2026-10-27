"""Sidebar "Plan a trip" form: start a data refresh for a new city from the dashboard,
or pull your latest eBird life list.

Shown only when GITHUB_TRIP_TOKEN is set (a fine-grained token with "Actions:
read and write" on this repo) and the viewer is allowed by TRIP_ADMINS — a
comma-separated list of emails matched against `st.user.email`, or "*" for
anyone who can open the app. Community Cloud doesn't fill in st.user for
invited viewers (only the app's own st.login does), so "*" with invite-only
sharing is the setup that works there. Both are read from st.secrets first, so a secrets edit
applies on the next rerun, then from env vars (.env locally).

Each run costs up to a couple of hours of eBird quota, so the form never shows
to an unlisted viewer, even on a private app.
"""

import os
import time
from collections.abc import Callable

import streamlit as st

from onlybirds import trip

MAX_RADIUS_KM = 50


def _setting(name: str) -> str:
    # Cloud also exports secrets as env vars, but only when the app starts.
    try:
        value = st.secrets.get(name)
    except FileNotFoundError:  # no secrets.toml locally
        value = None
    return str(value if value is not None else os.environ.get(name) or "").strip()


def _token() -> str | None:
    return _setting("GITHUB_TRIP_TOKEN") or None


def _viewer_allowed() -> bool:
    admins = {a.strip().lower() for a in _setting("TRIP_ADMINS").split(",") if a.strip()}
    if not admins:
        return False
    if "*" in admins:
        return True
    email = st.user.get("email")
    return isinstance(email, str) and email.lower() in admins


@st.cache_data(ttl=3600, show_spinner=False)
def _geocode(query: str) -> trip.Place:
    # Cached so re-runs of the script don't re-hit Nominatim (1 req/s policy).
    return trip.geocode(query)


@st.cache_data(ttl=30, show_spinner=False)
def _recent_runs(token: str) -> list[trip.TripRun]:
    return trip.recent_runs(token)


def _status_label(run: trip.TripRun) -> str:
    if run.status != "completed":
        return "⏳ " + run.status.replace("_", " ")
    return {"success": "✅ done", "cancelled": "⏹ cancelled"}.get(run.conclusion or "", f"❌ {run.conclusion}")


def _resolve_point(place: str, lat: str, lon: str) -> trip.Place | None:
    """Typed coordinates win over the place name; shows the error and returns None on failure."""
    try:
        if lat.strip() and lon.strip():
            return trip.Place(name=place.strip() or f"{lat}, {lon}", lat=float(lat), lon=float(lon))
        return _geocode(place)
    except ValueError:
        st.error("Lat and lon must be numbers.")
    except trip.TripError as e:
        st.error(str(e))
    return None


def render_trip_form() -> None:
    token = _token()
    if not token or not _viewer_allowed():
        return
    with st.sidebar, st.expander("✈️ Plan a trip"):
        _trip_panel(token)


# How long after starting a run to keep the buttons locked before GitHub lists it.
DISPATCH_GRACE_S = 90


@st.fragment(run_every="30s")
def _trip_panel(token: str) -> None:
    # A fragment, so the run status below refreshes itself every 30 s and the
    # buttons unlock when the run finishes, without rerunning the whole app.
    try:
        runs = _recent_runs(token)
    except trip.TripError:
        runs = []
    active = next((r for r in runs if r.status != "completed"), None)
    just_started = time.time() - st.session_state.get("trip_dispatched_at", 0.0) < DISPATCH_GRACE_S
    # Trips and life-list syncs share one eBird key and one DB (and one
    # concurrency group, where a third request would cancel the queued one),
    # so allow one at a time.
    busy = active is not None or just_started

    flash = st.session_state.pop("trip_flash", None)
    if flash:
        st.success(flash)
    if active is not None:
        st.info(f"⏳ Running: [{active.title.removeprefix('trip: ')}]({active.url}). New runs unlock when it finishes.")
    elif just_started:
        st.info("⏳ Starting… new runs unlock once it's underway.")

    st.caption(
        "Pull hotspots around a new place. Takes 1–2 hours for a new city "
        "(eBird allows 500 calls an hour); the app reloads with it when done."
    )
    with st.form("trip", border=False):
        place = st.text_input("City or place", placeholder="Austin, TX")
        st.caption("…or exact coordinates (these win if both are set)")
        c1, c2 = st.columns(2)
        lat = c1.text_input("Lat", placeholder="30.2672")
        lon = c2.text_input("Lon", placeholder="-97.7431")
        radius = st.slider("Radius (km)", 5, MAX_RADIUS_KM, MAX_RADIUS_KM, step=5)
        b1, b2 = st.columns(2)
        show = b1.form_submit_button("Show on map", width="stretch")
        submitted = b2.form_submit_button("Start refresh", type="primary", width="stretch", disabled=busy)

    if show or submitted:
        point = _resolve_point(place, lat, lon)
        if point is not None:
            # The map centers on ?at= and outlines the radius; it lives outside
            # this fragment, so rerun the whole app to move it.
            st.query_params["at"] = f"{point.lat:.4f},{point.lon:.4f},{radius}"
            started = not submitted or _start(
                lambda: trip.dispatch(
                    token,
                    lat=point.lat,
                    lon=point.lon,
                    radius_km=radius,
                    label=(place.strip() or point.name)[:80],
                ),
                f"Started: {point.name} ({point.lat:.3f}, {point.lon:.3f}), {radius} km",
            )
            if started:  # on failure, keep the error on screen
                st.rerun()

    st.divider()
    st.caption("Logged new birds? Pull your eBird life list so they drop off the targets (a few minutes).")
    if st.button("Pull my latest life list", width="stretch", disabled=busy):
        if _start(lambda: trip.dispatch_lifelist(token), "Started a life list sync."):
            st.rerun(scope="fragment")

    if runs:
        st.markdown("**Recent runs**")
        for run in runs:
            st.markdown(f"{_status_label(run)} · [{run.title.removeprefix('trip: ')}]({run.url})")


def _start(dispatch: Callable[[], None], message: str) -> bool:
    try:
        dispatch()
    except trip.TripError as e:
        st.error(str(e))
        return False
    st.session_state["trip_dispatched_at"] = time.time()
    st.session_state["trip_flash"] = message
    _recent_runs.clear()
    return True
