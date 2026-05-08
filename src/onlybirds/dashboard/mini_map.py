"""Compact location map shared by detail pages and the compare view."""

import folium
import streamlit as st

from onlybirds.dashboard.urls import _hotspot_url


def _detail_location_map(
    points: list[dict],
    *,
    height: int = 280,
    zoom: int = 14,
    highlight_id: str | None = None,
) -> None:
    """Compact location map for detail pages.

    `points` is a list of {hotspot_id, name, lat, lon}. A single point centers
    on it; multiple points fit-bounds the map. Markers link to each hotspot's
    detail page (``target="_top"`` so the link escapes the components iframe).
    Embedded via ``components.html`` to bypass the global streamlit_folium
    ``min-height: 78vh`` CSS that's tuned for the main map.
    """
    if not points:
        return
    lats = [float(p["lat"]) for p in points]
    lons = [float(p["lon"]) for p in points]
    center = [sum(lats) / len(lats), sum(lons) / len(lons)]
    m = folium.Map(location=center, zoom_start=zoom, tiles="cartodbpositron")
    if len(points) > 1:
        m.fit_bounds(
            [[min(lats), min(lons)], [max(lats), max(lons)]],
            padding=(30, 30),
        )
    for p in points:
        hid = p["hotspot_id"]
        name = p.get("name") or hid
        is_highlight = highlight_id is not None and hid == highlight_id
        href = _hotspot_url(hid)
        popup_html = (
            "<div style='font-family:ui-sans-serif,system-ui,sans-serif;"
            "min-width:160px;'>"
            f"<a href='{href}' target='_top' "
            "style='font-weight:700;color:#1f4f99;text-decoration:none;'>"
            f"{name}</a></div>"
        )
        folium.Marker(
            location=[float(p["lat"]), float(p["lon"])],
            popup=folium.Popup(popup_html, max_width=240),
            tooltip=name,
            icon=folium.Icon(
                color="red" if is_highlight else "blue",
                icon="binoculars",
                prefix="fa",
            ),
        ).add_to(m)
    st.iframe(m.get_root().render(), height=height)
