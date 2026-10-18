"""CARTO Positron tile URL includes the API key."""

import pytest

from onlybirds.dashboard.basemap import positron_map


def test_positron_map_embeds_carto_key(monkeypatch):
    monkeypatch.setenv("CARTO_API_KEY", "test-key")
    html = positron_map(location=[33.8, -118.4], zoom_start=10).get_root().render()
    assert (
        "https://basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}.png?key=test-key"
        in html
    )
    assert "carto.com/attributions" in html


def test_positron_map_quotes_key(monkeypatch):
    monkeypatch.setenv("CARTO_API_KEY", "a+b/c")
    html = positron_map(location=[0, 0], zoom_start=2).get_root().render()
    assert "key=a%2Bb%2Fc" in html
    assert "key=a+b/c" not in html


def test_positron_map_requires_key(monkeypatch):
    monkeypatch.delenv("CARTO_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CARTO_API_KEY"):
        positron_map(location=[0, 0], zoom_start=2)
