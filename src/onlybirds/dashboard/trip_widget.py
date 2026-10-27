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


def render_trip_form() -> None:
    token = _token()
    if not token or not _viewer_allowed():
        return

    with st.sidebar, st.expander("✈️ Plan a trip"):
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
            submitted = st.form_submit_button("Start refresh", width="stretch")

        if submitted:
            try:
                if lat.strip() and lon.strip():
                    point = trip.Place(name=place.strip() or f"{lat}, {lon}", lat=float(lat), lon=float(lon))
                else:
                    point = _geocode(place)
                trip.dispatch(
                    token,
                    lat=point.lat,
                    lon=point.lon,
                    radius_km=radius,
                    label=(place.strip() or point.name)[:80],
                )
            except ValueError:
                st.error("Lat and lon must be numbers.")
            except trip.TripError as e:
                st.error(str(e))
            else:
                _recent_runs.clear()
                st.success(f"Started: {point.name} ({point.lat:.3f}, {point.lon:.3f}), {radius} km")

        st.divider()
        st.caption("Logged new birds? Pull your eBird life list so they drop off the targets (a few minutes).")
        if st.button("Pull my latest life list", width="stretch"):
            try:
                trip.dispatch_lifelist(token)
            except trip.TripError as e:
                st.error(str(e))
            else:
                _recent_runs.clear()
                st.success("Started a life list sync.")

        try:
            runs = _recent_runs(token)
        except trip.TripError:
            runs = []
        if runs:
            st.markdown("**Recent runs**")
            for run in runs:
                st.markdown(f"{_status_label(run)} · [{run.title.removeprefix('trip: ')}]({run.url})")
