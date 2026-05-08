"""Small leaf helpers — no Streamlit, no folium."""

import datetime as dt

import pandas as pd


def _is_nan(v: object) -> bool:
    """pandas reads SQL NULL as NaN (a truthy float) — check for it explicitly."""
    return isinstance(v, float) and pd.isna(v)


def _clean_str(v: object) -> str | None:
    """Trim v to a non-empty string, or return None for NaN/None/non-string.

    Without this, `if row.get("image_url")` lets NaN through and `st.image(NaN)`
    crashes inside Streamlit's image utils.
    """
    if v is None or _is_nan(v) or not isinstance(v, str):
        return None
    return v.strip() or None


def _parse_iso_date(value: object) -> dt.date | None:
    """Parse eBird-style 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' to a date."""
    if value is None or _is_nan(value):
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace(" ", "T")[:19]).date()
    except ValueError:
        return None


def _days_ago(obs_dt: object) -> str:
    """Human label for how recently an obs happened."""
    seen = _parse_iso_date(obs_dt)
    if seen is None:
        return ""
    delta = (dt.date.today() - seen).days
    if delta < 0:
        return seen.isoformat()
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if delta < 14:
        return f"{delta}d ago"
    return seen.strftime("%b %d")


def _ebird_species_url(species_code: object) -> str | None:
    """eBird species page URL, e.g. /species/cangoo for Canada Goose."""
    if species_code is None or _is_nan(species_code):
        return None
    code = str(species_code).strip()
    return f"https://ebird.org/species/{code}" if code else None
