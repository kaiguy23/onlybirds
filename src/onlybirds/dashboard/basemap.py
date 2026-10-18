"""CARTO Positron basemap.

CARTO basemaps require a key on every tile URL. The key is a client-side
credential: the browser requests the tiles, so it ends up in the map HTML.
"""

import os
from urllib.parse import quote

import folium

# Positron is `light_all`. Zoom 0–20, matching CARTO's raster tiles.
_TILE_URL = "https://basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}.png?key="
_ATTR = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
    'contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
)


def positron_map(location: list[float], zoom_start: int, **kwargs) -> folium.Map:
    """Folium map on CARTO Positron, authenticated with ``CARTO_API_KEY``."""
    key = (os.environ.get("CARTO_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "CARTO_API_KEY not set. Request one at https://carto.com/basemaps/apikey/"
        )
    return folium.Map(
        location=location,
        zoom_start=zoom_start,
        tiles=_TILE_URL + quote(key, safe=""),
        attr=_ATTR,
        max_zoom=20,
        **kwargs,
    )
