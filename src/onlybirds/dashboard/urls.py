"""In-app URL builders for `?...` query-param routing."""


def _hotspot_url(hotspot_id: str) -> str:
    """URL that triggers the hotspot detail view via Streamlit query params."""
    return f"?hotspot={hotspot_id}"


def _consolidated_url(consolidated_id: str) -> str:
    """URL that triggers the consolidated-hotspot detail view."""
    return f"?consolidated={consolidated_id}"
