"""Streamlit dashboard for onlybirds.

Run via the CLI: `onlybirds serve --db data/onlybirds.db`
or directly:     `streamlit run src/onlybirds/dashboard/app.py -- --db data/onlybirds.db`
"""

import argparse
import sys

import streamlit as st

from onlybirds import db
from onlybirds.dashboard.compare import render_compare
from onlybirds.dashboard.data import load_data
from onlybirds.dashboard.styles import _PAGE_CSS
from onlybirds.dashboard.targets_view import render_targets
from onlybirds.dashboard.views import (
    render_consolidated_detail,
    render_hotspot_detail,
    render_map,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    return parser.parse_args(sys.argv[1:])


def main() -> None:
    args = _parse_args()
    st.set_page_config(page_title="onlybirds", page_icon="🐦", layout="wide")
    st.markdown(_PAGE_CSS, unsafe_allow_html=True)
    st.markdown(
        f"<div class='onlybirds-header'><h1>🐦 onlybirds</h1>"
        f"<span class='db'>reading <code>{args.db}</code></span></div>",
        unsafe_allow_html=True,
    )

    data = load_data(args.db)

    # Routing: ?view=compare for the compare view, ?hotspot=<id> or
    # ?consolidated=<id> for detail views.
    if st.query_params.get("view") == "compare":
        render_compare(data)
        return
    consolidated_id = st.query_params.get("consolidated")
    if consolidated_id:
        render_consolidated_detail(consolidated_id, data)
        return
    hotspot_id = st.query_params.get("hotspot")
    if hotspot_id:
        render_hotspot_detail(hotspot_id, data)
        return

    tab_map, tab_list = st.tabs(["Map", "Target list"])
    with tab_map:
        render_map(data)
    with tab_list:
        render_targets(data)


if __name__ == "__main__":
    main()
else:
    main()
