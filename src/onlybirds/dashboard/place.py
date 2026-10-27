"""Place search: type a city or "lat, lon" and the Map and Target list narrow to
the hotspots around it.

The place lives in the URL (`?at=lat,lon,radius_km&place=Name`), so it's
shared across tabs, survives reloads, and the trip form's "Show on map" sets
the same thing.
"""

import math

import pandas as pd
import streamlit as st

from onlybirds import trip

RADII_KM = (10, 25, 50)
DEFAULT_RADIUS_KM = 25


def parse_focus(raw: str | None) -> tuple[float, float, float] | None:
    """`?at=lat,lon[,radius_km]` → (lat, lon, radius_km), or None if absent/garbled."""
    if not raw:
        return None
    try:
        parts = [float(p) for p in raw.split(",")]
    except ValueError:
        return None
    if len(parts) < 2 or not (-90 <= parts[0] <= 90 and -180 <= parts[1] <= 180):
        return None
    radius = parts[2] if len(parts) > 2 and parts[2] > 0 else float(DEFAULT_RADIUS_KM)
    return parts[0], parts[1], radius


def current_focus() -> tuple[float, float, float] | None:
    return parse_focus(st.query_params.get("at"))


def within_radius(df: pd.DataFrame, focus: tuple[float, float, float]) -> pd.Series:
    """Mask of rows (with lat/lon columns) within the focus radius, by haversine distance."""
    lat0, lon0, radius_km = focus
    lat = df["lat"].astype(float).map(math.radians)
    dlat = lat - math.radians(lat0)
    dlon = (df["lon"].astype(float) - lon0).map(math.radians)
    a = (dlat / 2).map(math.sin) ** 2 + math.cos(math.radians(lat0)) * lat.map(math.cos) * (
        dlon / 2
    ).map(math.sin) ** 2
    return 2 * 6371.0 * a.clip(upper=1).map(math.sqrt).map(math.asin) <= radius_km


def _parse_coords(text: str) -> tuple[float, float] | None:
    """ "30.27, -97.74" → (30.27, -97.74); anything else → None."""
    parts = text.replace(",", " ").split()
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


@st.cache_data(ttl=3600, show_spinner=False)
def geocode(query: str) -> trip.Place:
    # Cached so reruns don't re-hit Nominatim (1 req/s policy).
    return trip.geocode(query)


def set_focus(lat: float, lon: float, radius_km: float, name: str) -> None:
    st.query_params["at"] = f"{lat:.4f},{lon:.4f},{radius_km:g}"
    st.query_params["place"] = name


def clear_focus() -> None:
    for k in ("at", "place"):
        st.query_params.pop(k, None)


def _short_name(name: str) -> str:
    # Nominatim's display_name runs to county/state/zip/country; keep the head.
    return ", ".join(name.split(", ")[:2])


def render_place_search() -> None:
    """Search box above the tabs, plus the "near X" line with radius and clear."""
    focus = current_focus()
    with st.form("place_search", border=False, enter_to_submit=True):
        c1, c2 = st.columns([6, 1])
        text = c1.text_input(
            "Place",
            placeholder="Search a place: Austin, TX or 30.27, -97.74",
            label_visibility="collapsed",
        )
        go = c2.form_submit_button("Go", width="stretch")
    if go and text.strip():
        radius = focus[2] if focus else DEFAULT_RADIUS_KM
        coords = _parse_coords(text)
        if coords:
            set_focus(*coords, radius, text.strip())
            st.rerun()
        try:
            place = geocode(text)
        except trip.TripError as e:
            st.error(str(e))
        else:
            set_focus(place.lat, place.lon, radius, _short_name(place.name))
            st.rerun()

    if focus:
        lat, lon, radius = focus
        name = st.query_params.get("place") or f"{lat:.3f}, {lon:.3f}"
        st.markdown(f"📍 Within **{radius:g} km** of **{name}**")
        # Keyed container: styles.py keeps this row on one line on phones.
        c2, c3 = st.container(key="place_bar").columns([3, 1], vertical_alignment="center")
        options = sorted({*RADII_KM, int(radius)})
        picked = c2.segmented_control(
            "Radius",
            options,
            default=int(radius),
            format_func=lambda r: f"{r} km",
            key=f"place_radius_{lat}_{lon}",
            label_visibility="collapsed",
        )
        if picked and picked != int(radius):
            set_focus(lat, lon, picked, name)
            st.rerun()
        if c3.button("✕ Clear", key="place_clear"):
            clear_focus()
            st.rerun()
