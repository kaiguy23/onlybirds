"""Global page CSS for the Streamlit app."""

_PAGE_CSS = """
<style>
/* Compact main container so the map dominates the screen. */
.block-container {padding-top: 1.2rem !important; padding-bottom: 0.5rem !important; max-width: 100% !important;}
header[data-testid="stHeader"] {background: transparent;}

/* Title row sits inline with the DB caption. */
.onlybirds-header {display:flex; align-items:baseline; gap:14px; margin: 0 0 8px 0;}
.onlybirds-header h1 {font-size: 1.6rem; margin: 0; line-height: 1;}
.onlybirds-header .db {color:#888; font-size:12px;}

/* The folium iframe streamlit-folium injects — let it stretch. */
iframe[title^="streamlit_folium"] {min-height: 78vh;}

/* Overlay the × clear button INSIDE the search input. We pick the inner
   stHorizontalBlock that wraps the search input + clear button (it's the
   only one that has the search input but no selectbox). */
[data-testid="stHorizontalBlock"]:has(input[placeholder*="search name or"]):not(:has([data-testid="stSelectbox"])) {
    position: relative !important;
    flex-wrap: nowrap !important;
    gap: 0 !important;
}
[data-testid="stHorizontalBlock"]:has(input[placeholder*="search name or"]):not(:has([data-testid="stSelectbox"])) > [data-testid="stColumn"]:first-child {
    flex: 1 1 100% !important;
    width: 100% !important;
}
[data-testid="stHorizontalBlock"]:has(input[placeholder*="search name or"]):not(:has([data-testid="stSelectbox"])) input[type="text"] {
    padding-right: 34px !important;
}
[data-testid="stHorizontalBlock"]:has(input[placeholder*="search name or"]):not(:has([data-testid="stSelectbox"])) > [data-testid="stColumn"]:nth-child(2) {
    position: absolute !important;
    right: 2px !important;
    top: 0 !important;
    bottom: 0 !important;
    width: auto !important;
    flex: 0 0 auto !important;
    display: flex !important;
    align-items: center !important;
    z-index: 10 !important;
}
[data-testid="stHorizontalBlock"]:has(input[placeholder*="search name or"]):not(:has([data-testid="stSelectbox"])) > [data-testid="stColumn"]:nth-child(2) button {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    color: #aaa !important;
    padding: 0 6px !important;
    min-height: 28px !important;
    height: 28px !important;
    width: 26px !important;
    min-width: 26px !important;
    line-height: 1 !important;
    font-size: 14px !important;
}
[data-testid="stHorizontalBlock"]:has(input[placeholder*="search name or"]):not(:has([data-testid="stSelectbox"])) > [data-testid="stColumn"]:nth-child(2) button:hover:not(:disabled) {
    color: #555 !important;
}
[data-testid="stHorizontalBlock"]:has(input[placeholder*="search name or"]):not(:has([data-testid="stSelectbox"])) > [data-testid="stColumn"]:nth-child(2) button:disabled {
    visibility: hidden !important;
}

/* On mobile, Streamlit columns stay side-by-side and squeeze the search
   input down to ~20% of the viewport. Stack the filter row vertically.
   The inner search + × clear block keeps its own `flex-wrap: nowrap` and
   per-column overrides above (higher specificity), so it stays inline. */
@media (max-width: 640px) {
    [data-testid="stHorizontalBlock"] {
        flex-wrap: wrap !important;
        row-gap: 6px !important;
    }
    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
        flex: 1 1 100% !important;
        width: 100% !important;
        min-width: 0 !important;
    }
}
</style>
"""
