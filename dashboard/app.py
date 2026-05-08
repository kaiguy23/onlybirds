"""Streamlit dashboard for onlybirds.

Run via the CLI: `onlybirds serve --db data/onlybirds.db`
or directly:     `streamlit run dashboard/app.py -- --db data/onlybirds.db`
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

# Make `from onlybirds...` work when running via `streamlit run`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from onlybirds import db  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    return parser.parse_args(sys.argv[1:])


@st.cache_data(ttl=60)
def load_data(db_path: str) -> dict[str, pd.DataFrame]:
    with db.session(db_path) as conn:
        targets = pd.read_sql_query(
            """
            SELECT t.species_code,
                   x.common_name, x.sci_name, x.family,
                   t.is_rare, t.rare_seen_at, t.rare_lat, t.rare_lon, t.rare_loc_name,
                   s.summary, s.image_url, s.wiki_url,
                   (SELECT COUNT(DISTINCT ho.hotspot_id)
                    FROM hotspot_obs ho
                    WHERE ho.species_code = t.species_code) AS hotspot_count
            FROM targets t
            JOIN taxonomy x ON x.species_code = t.species_code
            LEFT JOIN species_info s ON s.species_code = t.species_code
            ORDER BY t.is_rare DESC, x.common_name
            """,
            conn,
        )
        seasonality = pd.read_sql_query(
            """
            SELECT ss.species_code, ss.region, ss.months
            FROM species_seasonality ss
            JOIN targets t ON t.species_code = ss.species_code
            """,
            conn,
        )
        hotspots = pd.read_sql_query(
            """
            SELECT h.hotspot_id, h.name, h.lat, h.lon, h.region,
                   COUNT(DISTINCT ho.species_code) FILTER (WHERE ho.species_code IN
                       (SELECT species_code FROM targets)) AS target_count,
                   COUNT(DISTINCT ho.species_code) FILTER (WHERE ho.species_code IN
                       (SELECT species_code FROM targets WHERE is_rare = 1)) AS rare_target_count
            FROM hotspots h
            LEFT JOIN hotspot_obs ho ON ho.hotspot_id = h.hotspot_id
            GROUP BY h.hotspot_id
            """,
            conn,
        )
        hotspot_targets = pd.read_sql_query(
            """
            SELECT ho.hotspot_id, t.species_code, x.common_name, t.is_rare,
                   ho.last_seen, ho.how_many,
                   s.image_url, s.wiki_url
            FROM hotspot_obs ho
            JOIN targets t ON t.species_code = ho.species_code
            JOIN taxonomy x ON x.species_code = t.species_code
            LEFT JOIN species_info s ON s.species_code = t.species_code
            ORDER BY t.is_rare DESC, ho.last_seen DESC
            """,
            conn,
        )
        # Consolidated hotspots: dedupe targets across member hotspots.
        consolidated_hotspots = pd.read_sql_query(
            """
            SELECT ch.consolidated_id, ch.name, ch.lat, ch.lon, ch.member_count,
                   COUNT(DISTINCT t.species_code) AS target_count,
                   COUNT(DISTINCT CASE WHEN t.is_rare = 1 THEN t.species_code END) AS rare_target_count
            FROM consolidated_hotspots ch
            LEFT JOIN consolidated_hotspot_members chm
                   ON chm.consolidated_id = ch.consolidated_id
            LEFT JOIN hotspot_obs ho ON ho.hotspot_id = chm.hotspot_id
            LEFT JOIN targets t ON t.species_code = ho.species_code
            GROUP BY ch.consolidated_id
            """,
            conn,
        )
        consolidated_members = pd.read_sql_query(
            """
            SELECT chm.consolidated_id, h.hotspot_id, h.name, h.lat, h.lon, h.region
            FROM consolidated_hotspot_members chm
            JOIN hotspots h ON h.hotspot_id = chm.hotspot_id
            """,
            conn,
        )
        # Attach a region to each consolidated hotspot — the most common region
        # among its members (members are within ~1.5 km, so usually unanimous).
        if not consolidated_hotspots.empty and not consolidated_members.empty:
            region_per_cid = (
                consolidated_members.groupby("consolidated_id")["region"]
                .agg(
                    lambda s: s.dropna().mode().iat[0]
                    if not s.dropna().empty
                    else None
                )
            )
            consolidated_hotspots["region"] = consolidated_hotspots[
                "consolidated_id"
            ].map(region_per_cid)
        else:
            consolidated_hotspots["region"] = None
        # MAX(last_seen) is fine for ISO-formatted strings; same for how_many.
        consolidated_targets = pd.read_sql_query(
            """
            SELECT chm.consolidated_id, t.species_code, x.common_name, t.is_rare,
                   MAX(ho.last_seen) AS last_seen,
                   MAX(ho.how_many)  AS how_many,
                   s.image_url, s.wiki_url
            FROM consolidated_hotspot_members chm
            JOIN hotspot_obs ho ON ho.hotspot_id = chm.hotspot_id
            JOIN targets t ON t.species_code = ho.species_code
            JOIN taxonomy x ON x.species_code = t.species_code
            LEFT JOIN species_info s ON s.species_code = t.species_code
            GROUP BY chm.consolidated_id, t.species_code
            ORDER BY t.is_rare DESC, last_seen DESC
            """,
            conn,
        )
    return {
        "targets": targets,
        "hotspots": hotspots,
        "hotspot_targets": hotspot_targets,
        "seasonality": seasonality,
        "consolidated_hotspots": consolidated_hotspots,
        "consolidated_members": consolidated_members,
        "consolidated_targets": consolidated_targets,
    }


def _hotspot_url(hotspot_id: str) -> str:
    """URL that triggers the hotspot detail view via Streamlit query params."""
    return f"?hotspot={hotspot_id}"


MAX_COMPARE = 6


def _compare_ids() -> list[str]:
    """Current compare list from `?compare=` (deduped, length-capped)."""
    raw = st.query_params.get("compare")
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for token in raw.split(","):
        t = token.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= MAX_COMPARE:
            break
    return out


def _is_consolidated_id(item_id: str) -> bool:
    return item_id.startswith("cons-")


def _compare_url_with(ids: list[str], *, view: str | None = None) -> str:
    """Build a URL that sets `?compare=` to `ids` (and optional `?view=`).

    Preserves unrelated params like `?region=` so filter state survives a
    compare-tray click; drops the routing params (`hotspot`, `consolidated`)
    since compare actions navigate back to the map view.
    """
    preserved: list[tuple[str, str]] = []
    for key in st.query_params:
        if key in {"compare", "view", "hotspot", "consolidated"}:
            continue
        val = st.query_params.get(key)
        if val:
            preserved.append((key, val))
    parts = [f"{k}={v}" for k, v in preserved]
    if ids:
        parts.append("compare=" + ",".join(ids))
    if view:
        parts.append(f"view={view}")
    return "?" + "&".join(parts) if parts else "./"


def _compare_add_url(item_id: str, current: list[str]) -> str:
    if item_id in current or len(current) >= MAX_COMPARE:
        return _compare_url_with(current)
    return _compare_url_with(current + [item_id])


def _compare_remove_url(item_id: str, current: list[str]) -> str:
    return _compare_url_with([x for x in current if x != item_id])


def _compare_item_meta(
    item_id: str, data: dict[str, pd.DataFrame]
) -> dict | None:
    """Resolve a compare-list ID to {name, lat, lon, target_count, rare, kind}."""
    if _is_consolidated_id(item_id):
        c = data["consolidated_hotspots"]
        m = c[c["consolidated_id"] == item_id]
        if m.empty:
            return None
        r = m.iloc[0]
        return {
            "id": item_id,
            "kind": "consolidated",
            "name": r["name"] or item_id,
            "lat": r["lat"],
            "lon": r["lon"],
            "target_count": int(r.get("target_count") or 0),
            "rare_count": int(r.get("rare_target_count") or 0),
            "url": _consolidated_url(item_id),
        }
    h = data["hotspots"]
    m = h[h["hotspot_id"] == item_id]
    if m.empty:
        return None
    r = m.iloc[0]
    return {
        "id": item_id,
        "kind": "hotspot",
        "name": r["name"] or item_id,
        "lat": r["lat"],
        "lon": r["lon"],
        "target_count": int(r.get("target_count") or 0),
        "rare_count": int(r.get("rare_target_count") or 0),
        "url": _hotspot_url(item_id),
    }


def _render_compare_tray(
    data: dict[str, pd.DataFrame], *, on_compare_view: bool = False
) -> None:
    """Sticky chip strip showing the current compare selection."""
    current = _compare_ids()
    if not current:
        return
    chips: list[str] = []
    for cid in current:
        meta = _compare_item_meta(cid, data)
        name = (meta["name"] if meta else cid)[:36]
        marker = "⊕ " if (meta and meta["kind"] == "consolidated") else ""
        rm = _compare_remove_url(cid, current)
        chips.append(
            f"<span style='background:#1f4f99;color:white;padding:3px 6px 3px 10px;"
            f"border-radius:14px;font-size:12px;font-weight:600;margin:0 6px 4px 0;"
            f"display:inline-flex;align-items:center;gap:6px;'>"
            f"{marker}{name}"
            f"<a href='{rm}' target='_self' title='remove' "
            f"style='color:white;text-decoration:none;opacity:.85;font-weight:700;"
            f"padding:0 4px;'>×</a></span>"
        )
    compare_url = _compare_url_with(current, view="compare")
    clear_url = _compare_url_with([])
    cap_note = (
        f" <span style='color:#999;font-size:11px;'>(max {MAX_COMPARE})</span>"
        if len(current) >= MAX_COMPARE
        else ""
    )
    if on_compare_view:
        btn = ""
    elif len(current) >= 2:
        btn = (
            f"<a href='{compare_url}' target='_self' "
            f"style='background:#27ae60;color:white;padding:4px 12px;border-radius:14px;"
            f"font-size:12px;font-weight:700;margin:0 6px 4px 0;text-decoration:none;'>"
            f"Compare {len(current)} →</a>"
        )
    else:
        btn = (
            f"<span style='color:#999;font-size:11px;margin-right:6px;'>"
            f"add at least one more to compare</span>"
        )
    st.markdown(
        "<div class='onlybirds-compare-tray' "
        "style='background:#fff;padding:6px 10px;border:1px solid #e6ecf5;"
        "border-radius:10px;margin:0 0 8px 0;'>"
        "<span style='color:#666;font-size:11px;font-weight:700;letter-spacing:.06em;"
        "margin-right:8px;text-transform:uppercase;'>Compare</span>"
        + "".join(chips) + cap_note + btn +
        f"<a href='{clear_url}' target='_self' "
        f"style='color:#999;font-size:11px;text-decoration:none;'>clear</a>"
        "</div>",
        unsafe_allow_html=True,
    )


def _compare_toggle_button(item_id: str, *, key: str) -> None:
    """Styled add/remove pill for detail pages — toggles `?compare=` via URL."""
    current = _compare_ids()
    in_list = item_id in current
    if in_list:
        url = _compare_remove_url(item_id, current)
        label = "✓ In compare — remove"
        bg = "#27ae60"
    elif len(current) >= MAX_COMPARE:
        st.markdown(
            f"<div style='display:inline-block;background:#f0f2f6;color:#999;"
            f"padding:6px 14px;border-radius:18px;font-size:13px;font-weight:600;'>"
            f"Compare full ({MAX_COMPARE}) — remove one first</div>",
            unsafe_allow_html=True,
        )
        return
    else:
        url = _compare_add_url(item_id, current)
        label = "+ Add to compare"
        bg = "#1f4f99"
    st.markdown(
        f"<a href='{url}' target='_self' "
        f"style='display:inline-block;background:{bg};color:white;padding:6px 14px;"
        f"border-radius:18px;font-size:13px;font-weight:700;text-decoration:none;"
        f"box-shadow:0 1px 3px rgba(0,0,0,.12);'>{label}</a>",
        unsafe_allow_html=True,
    )
    # Suppress unused-key warning — kept for backward compat with callers.
    _ = key


def _popup_compare_pill(
    item_id: str, current: list[str], *, size: str = "md"
) -> str:
    """`+ compare` / `✓ in compare` pill for use inside a leaflet popup.

    Popups live inside the streamlit-folium iframe whose sandbox blocks
    in-tab top-navigation, so we use the same `window.open(u, '_blank')`
    pattern as the popup title link — clicking opens a new tab navigating
    to the URL with `?compare=` updated.
    """
    pad = "2px 8px" if size == "sm" else "3px 10px"
    fs = "11px" if size == "sm" else "12px"
    if item_id in current:
        target_url = _compare_url_with([x for x in current if x != item_id])
        bg = "#27ae60"
        label = "✓ in compare"
    elif len(current) >= MAX_COMPARE:
        return (
            f"<span style='display:inline-block;background:#f0f2f6;color:#999;"
            f"padding:{pad};border-radius:12px;font-size:{fs};font-weight:700;'>"
            f"compare full</span>"
        )
    else:
        target_url = _compare_url_with(current + [item_id])
        bg = "#1f4f99"
        label = "+ compare"
    qs = target_url[1:] if target_url.startswith("?") else target_url
    js_open = (
        "event.preventDefault();event.stopPropagation();"
        f"var u=window.top.location.pathname+'?{qs}';"
        "window.open(u,'_blank');"
    )
    return (
        f"<a href='{target_url}' target='_blank' onclick=\"{js_open}\" "
        f"style='display:inline-block;background:{bg};color:white;"
        f"padding:{pad};border-radius:12px;font-size:{fs};font-weight:700;"
        f"text-decoration:none;white-space:nowrap;'>{label}</a>"
    )


def _consolidated_url(consolidated_id: str) -> str:
    """URL that triggers the consolidated-hotspot detail view."""
    return f"?consolidated={consolidated_id}"


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


def _tooltip_html(name: str, target_count: int, rare_count: int) -> str:
    """Lightweight hover tooltip — full species list lives in the click popup."""
    rare_str = f" · {rare_count} rare" if rare_count else ""
    plural = "s" if target_count != 1 else ""
    return (
        f"<div>"
        f"<b>{name}</b>"
        f"<div style='color:#666;font-size:11px;margin-top:2px;'>"
        f"{target_count} target{plural}{rare_str}</div>"
        f"</div>"
    )


def _ebird_species_url(species_code: object) -> str | None:
    """eBird species page URL, e.g. /species/cangoo for Canada Goose."""
    if species_code is None or _is_nan(species_code):
        return None
    code = str(species_code).strip()
    return f"https://ebird.org/species/{code}" if code else None


def _popup_html(
    hotspot_id: str,
    name: str,
    hotspot_targets: pd.DataFrame,
    current_compare: list[str] | None = None,
) -> str:
    """Click popup: hotspot title (links to detail) + full species list.

    The title link uses target="_blank" + an onclick handler that constructs the
    parent-page URL via window.top.location.pathname. This works because the
    iframe sandbox has `allow-popups` (new-tab opening is allowed) and
    `allow-same-origin` (we can read the parent's pathname). Setting
    `window.top.location` directly is blocked by the sandbox.
    """
    # Dedupe species — `hotspot_obs` can have multiple rows per (hotspot, species)
    # if the bird was logged on multiple visits. The SQL orders by last_seen DESC,
    # so the first row per species is the most recent.
    if not hotspot_targets.empty and "species_code" in hotspot_targets.columns:
        hotspot_targets = hotspot_targets.drop_duplicates(
            subset=["species_code"], keep="first"
        )
    rows = []
    for _, t in hotspot_targets.iterrows():
        flag = " 🚨" if t["is_rare"] else ""
        # Prefer eBird (richer for birders); fall back to Wikipedia, then plain text.
        href = _ebird_species_url(t.get("species_code")) or _clean_str(t.get("wiki_url"))
        if href:
            link = (
                f'<a href="{href}" target="_blank" '
                f'style="color:#1f4f99;text-decoration:none;font-weight:500;">'
                f'{t["common_name"]}</a>'
            )
        else:
            link = t["common_name"]
        when = _days_ago(t.get("last_seen"))
        when_html = (
            f' <span style="color:#888;font-size:11px;">— {when}</span>' if when else ""
        )
        rows.append(f"<li style='margin:1px 0;'>{link}{flag}{when_html}</li>")
    # Cap the species list height so popups for big hotspots don't grow taller
    # than the map. Beyond ~14 species the inner <ul> scrolls.
    body = (
        "<div style='max-height:340px;overflow-y:auto;margin-top:6px;"
        # Reserve a bit of right padding so the scrollbar doesn't sit on top of
        # the species names.
        "padding-right:4px;'>"
        "<ul style='margin:0;padding-left:18px;font-size:13px;'>"
        + "".join(rows)
        + "</ul></div>"
        if rows
        else "<i style='color:#888;'>no targets here</i>"
    )
    js_open = (
        "event.preventDefault();"
        f"var u=window.top.location.pathname+'?hotspot={hotspot_id}';"
        "window.open(u,'_blank');"
    )
    # The title is the click target. Styled like a link, wraps long names.
    title = (
        f'<a href="?hotspot={hotspot_id}" target="_blank" '
        f'onclick="{js_open}" '
        f'style="display:block;font-size:15px;font-weight:700;'
        f'color:#1f4f99;text-decoration:none;line-height:1.3;'
        f'word-break:break-word;white-space:normal;'
        f'padding-bottom:4px;">'
        f'{name} <span style="font-size:11px;font-weight:600;">↗</span></a>'
    )
    pill = _popup_compare_pill(hotspot_id, current_compare or [])
    actions = (
        "<div style='display:flex;gap:6px;flex-wrap:wrap;align-items:center;"
        "border-bottom:1px solid #e6ecf5;padding-bottom:6px;margin-bottom:4px;'>"
        f"{pill}</div>"
    )
    return (
        f"<div style='min-width:220px;max-width:300px;'>{title}{actions}{body}</div>"
    )


def _consolidated_tooltip_html(
    name: str, target_count: int, rare_count: int, member_count: int
) -> str:
    rare_str = f" · {rare_count} rare" if rare_count else ""
    plural = "s" if target_count != 1 else ""
    return (
        f"<div>"
        f"<b>{name}</b>"
        f"<div style='color:#666;font-size:11px;margin-top:2px;'>"
        f"⊕ {member_count} hotspots · {target_count} unique target{plural}{rare_str}</div>"
        f"</div>"
    )


def _consolidated_popup_html(
    consolidated_id: str,
    name: str,
    member_rows: pd.DataFrame,
    target_rows: pd.DataFrame,
    current_compare: list[str] | None = None,
) -> str:
    """Click popup for a consolidated hotspot.

    Lists the original hotspots (each linking to its own detail page) and the
    deduped target species across them.
    """
    cur_cmp = current_compare or []
    members_html_parts: list[str] = []
    if member_rows is not None and not member_rows.empty:
        for _, m in member_rows.iterrows():
            mname = m.get("name") or m["hotspot_id"]
            href = _hotspot_url(m["hotspot_id"])
            js_open = (
                "event.preventDefault();"
                f"var u=window.top.location.pathname+'?hotspot={m['hotspot_id']}';"
                "window.open(u,'_blank');"
            )
            member_pill = _popup_compare_pill(
                m["hotspot_id"], cur_cmp, size="sm"
            )
            members_html_parts.append(
                f"<li style='margin:2px 0;display:flex;gap:6px;"
                f"align-items:center;flex-wrap:wrap;'>"
                f"<a href='{href}' target='_blank' onclick=\"{js_open}\" "
                f"style='color:#1f4f99;text-decoration:none;font-weight:500;'>"
                f"{mname}</a>{member_pill}</li>"
            )
    members_html = (
        "<div style='margin-top:4px;'>"
        "<div style='font-size:11px;color:#666;text-transform:uppercase;"
        "letter-spacing:.05em;margin-bottom:2px;'>Includes</div>"
        "<ul style='margin:0;padding-left:18px;font-size:12px;'>"
        + "".join(members_html_parts)
        + "</ul></div>"
        if members_html_parts
        else ""
    )

    target_html_parts: list[str] = []
    if target_rows is not None and not target_rows.empty:
        for _, t in target_rows.iterrows():
            flag = " 🚨" if t["is_rare"] else ""
            href = _ebird_species_url(t.get("species_code")) or _clean_str(t.get("wiki_url"))
            if href:
                link = (
                    f'<a href="{href}" target="_blank" '
                    f'style="color:#1f4f99;text-decoration:none;font-weight:500;">'
                    f'{t["common_name"]}</a>'
                )
            else:
                link = t["common_name"]
            when = _days_ago(t.get("last_seen"))
            when_html = (
                f' <span style="color:#888;font-size:11px;">— {when}</span>' if when else ""
            )
            target_html_parts.append(
                f"<li style='margin:1px 0;'>{link}{flag}{when_html}</li>"
            )
    targets_html = (
        "<div style='max-height:280px;overflow-y:auto;margin-top:8px;padding-right:4px;'>"
        "<div style='font-size:11px;color:#666;text-transform:uppercase;"
        "letter-spacing:.05em;margin-bottom:2px;'>Unique species</div>"
        "<ul style='margin:0;padding-left:18px;font-size:13px;'>"
        + "".join(target_html_parts)
        + "</ul></div>"
        if target_html_parts
        else "<div style='margin-top:8px;'><i style='color:#888;'>no targets here</i></div>"
    )

    js_open_title = (
        "event.preventDefault();"
        f"var u=window.top.location.pathname+'?consolidated={consolidated_id}';"
        "window.open(u,'_blank');"
    )
    title = (
        f'<a href="?consolidated={consolidated_id}" target="_blank" '
        f'onclick="{js_open_title}" '
        f'style="display:block;font-size:15px;font-weight:700;'
        f'color:#1f4f99;text-decoration:none;line-height:1.3;'
        f'word-break:break-word;white-space:normal;'
        f'padding-bottom:4px;">'
        f'⊕ {name} <span style="font-size:11px;font-weight:600;">↗</span></a>'
    )
    cons_pill = _popup_compare_pill(consolidated_id, cur_cmp)
    actions = (
        "<div style='display:flex;gap:6px;flex-wrap:wrap;align-items:center;"
        "border-bottom:1px solid #e6ecf5;padding-bottom:6px;margin-bottom:4px;'>"
        f"{cons_pill}</div>"
    )
    return (
        f"<div style='min-width:240px;max-width:320px;'>"
        f"{title}{actions}{members_html}{targets_html}</div>"
    )


# Region-preview hover: a tiny `components.html` iframe runs this script. The
# folium iframe is wrapped by streamlit-folium's React frontend, which builds
# the iframe DOM imperatively — inline `<script>` tags inside `root.html`
# never execute. This iframe (created via `components.html`) IS parsed via
# srcdoc, so its scripts do run. From here we reach `window.parent.document`
# to find the chips and `parent.querySelector('iframe[title^=streamlit_folium]')
# .contentWindow` to drive the folium map directly (same origin).
# `__BBOXES__` is replaced with a JSON object
# {region_key: [min_lat, min_lon, max_lat, max_lon]}.
_REGION_PREVIEW_HTML_TEMPLATE = """
<script>
(function() {
  var bboxes = __BBOXES__;
  var rect = null;
  function getFoliumWin() {
    try {
      var pdoc = window.parent && window.parent.document;
      if (!pdoc) return null;
      var iframe = pdoc.querySelector('iframe[title^="streamlit_folium"]');
      if (!iframe || !iframe.contentWindow) return null;
      return iframe.contentWindow;
    } catch (e) { return null; }
  }
  function clearRect() {
    var w = getFoliumWin();
    if (rect && w && w.map) { try { w.map.removeLayer(rect); } catch(e){} }
    rect = null;
  }
  function showRect(region) {
    clearRect();
    var bb = bboxes[region];
    var w = getFoliumWin();
    if (!bb || !w || !w.map || !w.L) return;
    var bounds = [[bb[0], bb[1]], [bb[2], bb[3]]];
    rect = w.L.rectangle(
      bounds,
      {color:'#1f4f99', weight:2, fillOpacity:0.08, dashArray:'6,4', interactive:false}
    ).addTo(w.map);
  }
  function attach() {
    try {
      var pdoc = window.parent && window.parent.document;
      if (!pdoc) return;
      var chips = pdoc.querySelectorAll('a[data-region]');
      chips.forEach(function(chip) {
        if (chip.dataset.onlybirdsBound === '1') return;
        chip.dataset.onlybirdsBound = '1';
        var key = chip.getAttribute('data-region');
        chip.addEventListener('mouseenter', function() { showRect(key); });
        chip.addEventListener('mouseleave', function() { clearRect(); });
      });
    } catch (e) {}
  }
  // Streamlit re-renders chip DOM on rerun; observe parent body so new chips
  // get bound. Brief polling covers the cold-start case where parent.body
  // isn't ready when the iframe first loads.
  attach();
  try {
    var pdoc = window.parent && window.parent.document;
    if (pdoc && pdoc.body) {
      new MutationObserver(attach).observe(pdoc.body, {childList:true, subtree:true});
    }
  } catch (e) {}
  var ticks = 0;
  function tick() { ticks++; attach(); if (ticks < 50) setTimeout(tick, 100); }
  tick();
})();
</script>
"""


# Color tiers: rare > many targets > some targets > one target > none.
@dataclass(frozen=True)
class _Tier:
    fill: str
    halo: str
    label: str
    min: int = 0  # minimum target_count for this tier (unused for the rare tier)


_TIERS: list[_Tier] = [
    _Tier(min=5, fill="#1f4f99", halo="31,79,153", label="5+ targets"),
    _Tier(min=2, fill="#3498db", halo="52,152,219", label="2–4 targets"),
    _Tier(min=1, fill="#7fb8e0", halo="127,184,224", label="1 target"),
    _Tier(min=0, fill="#bdbdbd", halo="189,189,189", label="no targets"),
]
_RARE = _Tier(fill="#e74c3c", halo="231,76,60", label="rare alert")


def _tier_for(target_count: int, rare_count: int) -> _Tier:
    if rare_count > 0:
        return _RARE
    for t in _TIERS:
        if target_count >= t.min:
            return t
    return _TIERS[-1]


def _marker_diameter(target_count: int, rare_count: int) -> int:
    score = target_count + 4 * rare_count
    return int(min(18 + 1.4 * score, 44))


def _marker_div_html(target_count: int, rare_count: int) -> tuple[str, int]:
    tier = _tier_for(target_count, rare_count)
    d = _marker_diameter(target_count, rare_count)
    if rare_count > 0:
        label = f"🚨{target_count}" if target_count else "🚨"
        font_size = 10
    elif target_count > 0:
        label = str(target_count)
        font_size = 11 if d < 28 else 13
    else:
        label = ""
        font_size = 10
    html = (
        f"<div style=\"background:{tier.fill};color:white;border-radius:50%;"
        f"width:{d}px;height:{d}px;display:flex;align-items:center;justify-content:center;"
        f"font-weight:700;font-size:{font_size}px;border:2px solid white;"
        f"box-shadow:0 0 0 4px rgba({tier.halo},0.32),0 1px 3px rgba(0,0,0,0.3);\">"
        f"{label}</div>"
    )
    return html, d


def _consolidated_marker_html(
    target_count: int, rare_count: int, member_count: int
) -> tuple[str, int]:
    """Square marker with a corner badge — visually distinct from singletons."""
    tier = _tier_for(target_count, rare_count)
    d = _marker_diameter(target_count, rare_count) + 2
    if rare_count > 0:
        label = f"🚨{target_count}" if target_count else "🚨"
        font_size = 10
    elif target_count > 0:
        label = str(target_count)
        font_size = 11 if d < 28 else 13
    else:
        label = "·"
        font_size = 10
    # Wrapper sized to the inner square so folium's icon_anchor centers correctly.
    html = (
        f"<div style=\"position:relative;width:{d}px;height:{d}px;\">"
        f"<div style=\"background:{tier.fill};color:white;border-radius:7px;"
        f"width:100%;height:100%;display:flex;align-items:center;justify-content:center;"
        f"font-weight:700;font-size:{font_size}px;border:2px solid white;"
        f"box-shadow:0 0 0 4px rgba({tier.halo},0.32),0 1px 3px rgba(0,0,0,0.3);\">"
        f"{label}</div>"
        f"<div style=\"position:absolute;top:-7px;right:-9px;background:white;color:#333;"
        f"border-radius:9px;padding:1px 5px;font-size:9px;font-weight:700;"
        f"border:1px solid #888;line-height:1.2;white-space:nowrap;\">"
        f"⊕{member_count}</div>"
        f"</div>"
    )
    return html, d


_CLUSTER_ICON_FN = """
function(cluster) {
    var codes = new Set();
    var hasRare = false;
    cluster.getAllChildMarkers().forEach(function(m) {
        (m.options.targetCodes || []).forEach(function(c) { codes.add(c); });
        if (m.options.hasRare) { hasRare = true; }
    });
    var n = codes.size;
    var bg, halo;
    if (hasRare)        { bg = '#e74c3c'; halo = '231,76,60'; }
    else if (n >= 30)   { bg = '#1f4f99'; halo = '31,79,153'; }
    else if (n >= 10)   { bg = '#3498db'; halo = '52,152,219'; }
    else                { bg = '#7fb8e0'; halo = '127,184,224'; }
    var size = n < 10 ? 36 : (n < 30 ? 44 : 52);
    var label = hasRare ? ('🚨' + n) : n;
    var html = '<div style="background:' + bg + ';color:white;border-radius:50%;'
             + 'width:' + size + 'px;height:' + size + 'px;'
             + 'display:flex;align-items:center;justify-content:center;'
             + 'font-weight:700;font-size:13px;border:2px solid white;'
             + 'box-shadow:0 0 0 5px rgba(' + halo + ',0.32),0 1px 3px rgba(0,0,0,0.3);">'
             + '<span>' + label + '</span></div>';
    return L.divIcon({
        html: html,
        className: 'marker-cluster',
        iconSize: L.point(size, size)
    });
}
"""


_LEGEND_HTML = """
<div id="onlybirds-legend" style="
    position: absolute; bottom: 24px; left: 16px; z-index: 9999;
    background: rgba(255,255,255,0.96); padding: 10px 14px;
    border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.18);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 12px; line-height: 1.7; color: #222;">
  <div style="font-weight:700;margin-bottom:4px;letter-spacing:.02em;">Where to go</div>
  <div style="display:flex;align-items:center;gap:8px;">
    <span style="width:14px;height:14px;border-radius:50%;background:#e74c3c;
                 box-shadow:0 0 0 3px rgba(231,76,60,0.32);display:inline-block;"></span>
    rare alert <span style="color:#888;">(prioritize)</span>
  </div>
  <div style="display:flex;align-items:center;gap:8px;">
    <span style="width:14px;height:14px;border-radius:50%;background:#1f4f99;display:inline-block;"></span>
    5+ targets
  </div>
  <div style="display:flex;align-items:center;gap:8px;">
    <span style="width:12px;height:12px;border-radius:50%;background:#3498db;display:inline-block;"></span>
    2–4 targets
  </div>
  <div style="display:flex;align-items:center;gap:8px;">
    <span style="width:10px;height:10px;border-radius:50%;background:#7fb8e0;display:inline-block;"></span>
    1 target
  </div>
  <div style="display:flex;align-items:center;gap:8px;">
    <span style="width:8px;height:8px;border-radius:50%;background:#bdbdbd;display:inline-block;"></span>
    no targets
  </div>
  <div style="margin-top:6px;color:#777;font-size:11px;">circle size ∝ target count</div>
</div>
"""


def _region_label(region: str | None) -> str:
    """Compact label for an eBird region code: 'US-CA-059' → 'CA-059'."""
    if not region:
        return "Other"
    parts = region.split("-")
    # Drop the country prefix when present (2-letter ISO code at the front).
    if len(parts) >= 2 and len(parts[0]) == 2:
        return "-".join(parts[1:])
    return region


def _region_chip_html(
    label: str, url: str, active: bool, region_key: str | None = None
) -> str:
    bg = "#1f4f99" if active else "#eef3fb"
    color = "white" if active else "#1f4f99"
    # `data-region` is what the hover-preview JS looks for to draw the bbox
    # outline on the map. Omitted on the "All regions" chip.
    data_attr = f' data-region="{region_key}"' if region_key else ""
    return (
        f'<a href="{url}" target="_self"{data_attr} '
        f'class="onlybirds-region-chip" '
        f'style="background:{bg};color:{color};padding:4px 10px;border-radius:14px;'
        f'font-size:12px;font-weight:600;margin-right:6px;display:inline-block;'
        f'margin-bottom:4px;text-decoration:none;cursor:pointer;">'
        f'{label}</a>'
    )


_REGION_NONE_SENTINEL = "__none__"


def _region_bboxes(hotspots: pd.DataFrame) -> dict[str, list[float]]:
    """Per-region bbox `[min_lat, min_lon, max_lat, max_lon]` from hotspot points.

    Used by the chip-hover preview to outline where each region's hotspots sit
    on the map. Hotspots with NULL/empty region land in the `_REGION_NONE_SENTINEL`
    bucket so the "Other" chip can preview too.
    """
    if hotspots.empty:
        return {}
    df = hotspots[["region", "lat", "lon"]].copy()
    df["region"] = df["region"].fillna("").apply(
        lambda r: r if r else _REGION_NONE_SENTINEL
    )
    out: dict[str, list[float]] = {}
    for region, g in df.groupby("region"):
        out[str(region)] = [
            float(g["lat"].min()),
            float(g["lon"].min()),
            float(g["lat"].max()),
            float(g["lon"].max()),
        ]
    return out


def _parse_active_regions(qp_value: str | None) -> set[str]:
    """`?region=A,B,C` → {"A","B","C"}. Empty/missing → empty set (= All)."""
    if not qp_value:
        return set()
    return {r for r in (s.strip() for s in qp_value.split(",")) if r}


def _region_mask(df: pd.DataFrame, active: set[str]) -> pd.Series:
    """Boolean mask of rows whose `region` is in the active set.

    The `_REGION_NONE_SENTINEL` member matches rows with NULL/empty region.
    """
    none_active = _REGION_NONE_SENTINEL in active
    real = active - {_REGION_NONE_SENTINEL}
    mask = pd.Series(False, index=df.index)
    if real:
        mask = mask | df["region"].isin(real)
    if none_active:
        mask = mask | df["region"].isna() | (df["region"] == "")
    return mask


def _toggle_region_url(active: set[str], key: str) -> str:
    """URL that flips `key` in or out of the active set."""
    new = active.symmetric_difference({key})
    if not new:
        return "./"
    # Sort for stable URLs (so the same selection always has the same URL).
    return "?region=" + ",".join(sorted(new))


def _region_chips_panel(
    singletons: pd.DataFrame, consolidated: pd.DataFrame, active: set[str]
) -> None:
    """Region filter strip. Click chip → ?region=A,B,C rerenders filtered."""
    counts: dict[str, int] = {}
    for df in (singletons, consolidated):
        if df.empty or "region" not in df.columns:
            continue
        for r in df["region"]:
            key = r if isinstance(r, str) and r else _REGION_NONE_SENTINEL
            counts[key] = counts.get(key, 0) + 1
    # No point showing the strip if there's only one region in scope.
    if len(counts) <= 1:
        return
    chips: list[str] = [_region_chip_html("All regions", "./", not active)]
    for key, n in sorted(counts.items(), key=lambda x: -x[1]):
        is_active = key in active
        label = (
            f"Other ({n})"
            if key == _REGION_NONE_SENTINEL
            else f"{_region_label(key)} ({n})"
        )
        chips.append(
            _region_chip_html(label, _toggle_region_url(active, key), is_active, region_key=key)
        )
    st.markdown(
        "<div style='margin:-2px 0 8px 0;'>"
        "<span style='color:#666;font-size:11px;font-weight:700;letter-spacing:.06em;"
        "margin-right:8px;text-transform:uppercase;'>Region</span>"
        + "".join(chips)
        + "</div>",
        unsafe_allow_html=True,
    )


def _top_hotspots_panel(
    singletons: pd.DataFrame, consolidated: pd.DataFrame
) -> None:
    """Render a compact ranked list of best places (singletons + consolidated)."""
    rows: list[dict] = []
    if not singletons.empty:
        for _, h in singletons.iterrows():
            rows.append(
                {
                    "url": _hotspot_url(h["hotspot_id"]),
                    "name": h["name"] or h["hotspot_id"],
                    "target_count": int(h["target_count"] or 0),
                    "rare_count": int(h["rare_target_count"] or 0),
                    "is_consolidated": False,
                }
            )
    if not consolidated.empty:
        for _, c in consolidated.iterrows():
            rows.append(
                {
                    "url": _consolidated_url(c["consolidated_id"]),
                    "name": c["name"] or c["consolidated_id"],
                    "target_count": int(c["target_count"] or 0),
                    "rare_count": int(c["rare_target_count"] or 0),
                    "is_consolidated": True,
                }
            )
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["_score"] = df["rare_count"] * 10 + df["target_count"]
    df = df[df["_score"] > 0].sort_values("_score", ascending=False).head(5)
    if df.empty:
        return
    current_compare = _compare_ids()
    chips = []
    for _, r in df.iterrows():
        rare = int(r["rare_count"])
        tot = int(r["target_count"])
        bg = "#e74c3c" if rare else ("#1f4f99" if tot >= 5 else "#3498db")
        prefix = f"🚨{rare} · " if rare else ""
        marker = "⊕ " if r["is_consolidated"] else ""
        name = str(r["name"])[:48]
        # Each url already encodes the routing (?hotspot= or ?consolidated=);
        # derive the compare-target id from it.
        target_id = r["url"].split("=", 1)[1]
        in_compare = target_id in current_compare
        if in_compare:
            plus_url = _compare_remove_url(target_id, current_compare)
            plus_glyph = "✓"
            plus_title = "remove from compare"
        elif len(current_compare) >= MAX_COMPARE:
            plus_url = ""
            plus_glyph = ""
            plus_title = ""
        else:
            plus_url = _compare_add_url(target_id, current_compare)
            plus_glyph = "+"
            plus_title = "add to compare"
        plus_html = (
            f'<a href="{plus_url}" target="_self" title="{plus_title}" '
            f'style="background:rgba(255,255,255,.22);color:white;padding:0 6px;'
            f'border-radius:10px;font-size:11px;font-weight:800;margin-left:6px;'
            f'text-decoration:none;line-height:1.4;">{plus_glyph}</a>'
            if plus_glyph
            else ""
        )
        chips.append(
            f'<span style="background:{bg};color:white;padding:4px 6px 4px 10px;'
            f'border-radius:14px;font-size:12px;font-weight:600;margin-right:6px;'
            f'display:inline-flex;align-items:center;margin-bottom:4px;">'
            f'<a href="{r["url"]}" target="_self" '
            f'style="color:white;text-decoration:none;cursor:pointer;">'
            f'{prefix}{tot}× {marker}{name}</a>{plus_html}</span>'
        )
    st.markdown(
        "<div style='margin:-4px 0 6px 0;'>"
        "<span style='color:#666;font-size:11px;font-weight:700;letter-spacing:.06em;"
        "margin-right:8px;text-transform:uppercase;'>Top hotspots</span>"
        + "".join(chips)
        + "</div>",
        unsafe_allow_html=True,
    )


def render_map(data: dict[str, pd.DataFrame]) -> None:
    hotspots = data["hotspots"]
    hotspot_targets = data["hotspot_targets"]
    consolidated = data["consolidated_hotspots"]
    consolidated_members = data["consolidated_members"]
    consolidated_targets = data["consolidated_targets"]
    if hotspots.empty:
        st.info("No hotspots loaded yet — run `onlybirds run` first.")
        return

    # Hotspots that are members of a consolidation are hidden from the map —
    # they appear under the consolidated marker's popup instead.
    member_ids: set[str] = (
        set(consolidated_members["hotspot_id"]) if not consolidated_members.empty else set()
    )
    singletons = hotspots[~hotspots["hotspot_id"].isin(member_ids)]

    # Region filter: a chip strip above the top-hotspots strip. Active regions
    # come from `?region=A,B,C` (multi-select) and narrow the map, the
    # top-hotspots panel, and the bounds.
    _render_compare_tray(data)
    current_compare = _compare_ids()
    active_regions = _parse_active_regions(st.query_params.get("region"))
    _region_chips_panel(singletons, consolidated, active_regions)
    if active_regions:
        singletons = singletons[_region_mask(singletons, active_regions)]
        consolidated = consolidated[_region_mask(consolidated, active_regions)]
        # Keep member/target frames in sync so popups for the surviving
        # consolidations still resolve correctly.
        kept_cids = set(consolidated["consolidated_id"])
        consolidated_members = consolidated_members[
            consolidated_members["consolidated_id"].isin(kept_cids)
        ]
        consolidated_targets = consolidated_targets[
            consolidated_targets["consolidated_id"].isin(kept_cids)
        ]
        if singletons.empty and consolidated.empty:
            st.info("No hotspots in the selected regions.")
            return

    _top_hotspots_panel(singletons, consolidated)

    # Compute center & bounds from the *filtered* set so the map zooms to the
    # selected regions.
    lats = list(singletons["lat"]) + list(consolidated["lat"])
    lons = list(singletons["lon"]) + list(consolidated["lon"])
    center = [sum(lats) / len(lats), sum(lons) / len(lons)]
    m = folium.Map(location=center, zoom_start=10, tiles="cartodbpositron")
    if active_regions and len(lats) > 1:
        m.fit_bounds(
            [[min(lats), min(lons)], [max(lats), max(lons)]],
            padding=(40, 40),
        )
    cluster = MarkerCluster(icon_create_function=_CLUSTER_ICON_FN).add_to(m)
    # Group once instead of filtering per hotspot — avoids N² scan.
    targets_by_hotspot = dict(tuple(hotspot_targets.groupby("hotspot_id", sort=False)))
    empty_targets = hotspot_targets.iloc[0:0]
    for _, h in singletons.iterrows():
        rows = targets_by_hotspot.get(h["hotspot_id"], empty_targets)
        target_count = int(h["target_count"] or 0)
        rare_count = int(h["rare_target_count"] or 0)
        has_rare = rare_count > 0
        html, diameter = _marker_div_html(target_count, rare_count)
        name = h["name"] or h["hotspot_id"]
        tooltip = folium.Tooltip(_tooltip_html(name, target_count, rare_count))
        popup = folium.Popup(
            _popup_html(h["hotspot_id"], name, rows, current_compare),
            max_width=320,
        )
        folium.Marker(
            location=[h["lat"], h["lon"]],
            tooltip=tooltip,
            popup=popup,
            icon=folium.DivIcon(
                html=html,
                icon_size=(diameter, diameter),
                icon_anchor=(diameter // 2, diameter // 2),
                class_name="onlybirds-marker",
            ),
            target_codes=rows["species_code"].tolist(),
            has_rare=has_rare,
        ).add_to(cluster)

    # Consolidated markers — squares with a member-count badge. Popup lists
    # members + deduped species; title link goes to the consolidated detail
    # page.
    if not consolidated.empty:
        members_by_cid = (
            dict(tuple(consolidated_members.groupby("consolidated_id", sort=False)))
            if not consolidated_members.empty
            else {}
        )
        ctargets_by_cid = (
            dict(tuple(consolidated_targets.groupby("consolidated_id", sort=False)))
            if not consolidated_targets.empty
            else {}
        )
        empty_members = consolidated_members.iloc[0:0]
        empty_ctargets = consolidated_targets.iloc[0:0]
        for _, c in consolidated.iterrows():
            cid = c["consolidated_id"]
            members = members_by_cid.get(cid, empty_members)
            ctargets = ctargets_by_cid.get(cid, empty_ctargets)
            target_count = int(c["target_count"] or 0)
            rare_count = int(c["rare_target_count"] or 0)
            member_count = int(c["member_count"] or 0)
            has_rare = rare_count > 0
            cons_html, cons_d = _consolidated_marker_html(
                target_count, rare_count, member_count
            )
            name = c["name"] or cid
            tooltip = folium.Tooltip(
                _consolidated_tooltip_html(name, target_count, rare_count, member_count)
            )
            popup = folium.Popup(
                _consolidated_popup_html(
                    cid, name, members, ctargets, current_compare
                ),
                max_width=340,
            )
            folium.Marker(
                location=[c["lat"], c["lon"]],
                tooltip=tooltip,
                popup=popup,
                icon=folium.DivIcon(
                    html=cons_html,
                    icon_size=(cons_d, cons_d),
                    icon_anchor=(cons_d // 2, cons_d // 2),
                    class_name="onlybirds-marker onlybirds-marker--consolidated",
                ),
                target_codes=ctargets["species_code"].tolist(),
                has_rare=has_rare,
            ).add_to(cluster)

    # Legend overlay sits inside the map container so it floats over the tiles.
    # `m.get_root()` returns folium's `Figure`, which has `.html` and `.header`
    # attributes at runtime. ty can't see them through folium's untyped surface,
    # hence the `ty: ignore`s below.
    root = m.get_root()
    root.html.add_child(folium.Element(_LEGEND_HTML))  # ty: ignore[unresolved-attribute]
    # Region-preview rectangle on chip hover. Bboxes come from the *unfiltered*
    # hotspot set so chips for regions outside the current filter still preview
    # correctly.
    #
    # The actual hover handler is emitted via `components.html` *after* this
    # function returns (see render_map's caller), since scripts injected into
    # the folium iframe via `root.html`/`root.script` don't execute under
    # streamlit-folium's React-based DOM construction.
    bboxes = _region_bboxes(data["hotspots"])
    # Leaflet's default `.leaflet-tooltip` rule sets `white-space: nowrap`, so
    # long hotspot names overflow the map. Override inside the iframe so they
    # wrap at word boundaries up to a sensible max width. Avoid `word-break`
    # / `overflow-wrap: anywhere` here — they let the tooltip's min-content
    # collapse to a single character, which produces a 1-char-wide column.
    root.header.add_child(  # ty: ignore[unresolved-attribute]
        folium.Element(
            "<style>"
            ".leaflet-tooltip{"
            "white-space:normal !important;"
            "max-width:260px !important;"
            "width:max-content !important;"
            "}"
            "</style>"
        )
    )

    # Click on a marker opens a popup whose title links to the hotspot detail.
    # We don't need st_folium's return value since navigation happens via the
    # popup link (target="_blank" — opens detail in a new tab; the iframe
    # sandbox blocks in-tab top-navigation from popup HTML).
    st_folium(
        m,
        use_container_width=True,
        height=820,
        returned_objects=[],
        key="hotspot_map",
    )

    # Region-preview hover handler. Emitted as a separate `components.html`
    # iframe (which honors srcdoc and runs scripts) that reaches into the
    # folium iframe's same-origin window to draw/remove the bbox rectangle
    # on chip mouseenter/leave. Height=0 keeps it visually invisible.
    if bboxes:
        preview_html = _REGION_PREVIEW_HTML_TEMPLATE.replace(
            "__BBOXES__", json.dumps(bboxes)
        )
        components.html(preview_html, height=0)


def _months_for_species(seasonality: pd.DataFrame, species_code: str) -> set[int]:
    rows = seasonality[seasonality["species_code"] == species_code]
    months: set[int] = set()
    for m in rows["months"]:
        try:
            months.update(json.loads(m))
        except (TypeError, ValueError):
            continue
    return months


def _months_strip(months: set[int], current_month: int) -> str:
    """Render Jan-Dec strip with hits highlighted and the current month boxed."""
    parts = []
    for i in range(1, 13):
        label = calendar.month_abbr[i]
        if i == current_month:
            parts.append(f"**[{label}]**" if i in months else f"[{label}]")
        else:
            parts.append(f"**{label}**" if i in months else f"<span style='color:#bbb'>{label}</span>")
    return " ".join(parts)


def _render_hotspot_list(hotspots_df: pd.DataFrame, *, current_hotspot_id: str | None = None) -> None:
    """Render an HTML list of hotspots where a species was observed.

    Each row links to the hotspot detail view via `?hotspot=<id>`.
    """
    if hotspots_df is None or hotspots_df.empty:
        st.caption("No hotspot observations recorded.")
        return
    sorted_df = hotspots_df.copy()
    if "last_seen" in sorted_df.columns:
        sorted_df = sorted_df.sort_values("last_seen", ascending=False, na_position="last")
    items = []
    for _, h in sorted_df.iterrows():
        hid = h["hotspot_id"]
        name = h.get("name") or hid
        when = _days_ago(h.get("last_seen"))
        when_html = (
            f"<span style='color:#888;font-size:12px;'> · last seen {when}</span>"
            if when
            else ""
        )
        rare_html = " 🚨" if h.get("is_rare") else ""
        if hid == current_hotspot_id:
            items.append(
                f"<li style='margin:2px 0;'><b>{name}</b> "
                f"<span style='color:#1f4f99;font-size:11px;'>(this hotspot)</span>"
                f"{rare_html}{when_html}</li>"
            )
        else:
            items.append(
                f"<li style='margin:2px 0;'>"
                f"<a href='{_hotspot_url(hid)}' target='_self' "
                f"style='color:#1f4f99;text-decoration:none;font-weight:600;'>"
                f"{name}</a>{rare_html}{when_html}</li>"
            )
    st.markdown(
        "<ul style='margin:4px 0 0 0;padding-left:18px;'>" + "".join(items) + "</ul>",
        unsafe_allow_html=True,
    )


def _render_target_card(
    row: pd.Series,
    seasonality: pd.DataFrame,
    current_month: int,
    *,
    last_seen: str | None = None,
    how_many: int | float | str | None = None,
    hotspots_for_species: pd.DataFrame | None = None,
    current_hotspot_id: str | None = None,
) -> None:
    cols = st.columns([1, 4])
    with cols[0]:
        img = _clean_str(row.get("image_url"))
        if img:
            st.image(img, use_container_width=True)
    with cols[1]:
        badge = " 🚨 **RARE**" if row["is_rare"] else ""
        st.markdown(f"### {row['common_name']}{badge}")
        meta = f"*{row['sci_name']}* — {row['family'] or ''}"
        st.caption(meta)
        when = _days_ago(last_seen)
        if when:
            count_str = ""
            if how_many is not None and not _is_nan(how_many):
                try:
                    n = int(how_many)
                    if n > 0:
                        count_str = f" · {n} bird{'s' if n != 1 else ''}"
                except (TypeError, ValueError):
                    pass
            st.markdown(
                f"<div style='margin:-4px 0 6px 0;'>"
                f"<span style='background:#eef3fb;color:#1f4f99;padding:2px 8px;"
                f"border-radius:10px;font-size:12px;font-weight:600;'>"
                f"last seen {when}{count_str}</span></div>",
                unsafe_allow_html=True,
            )
        if row["is_rare"] and row.get("rare_loc_name"):
            st.markdown(f"**Recently reported at:** {row['rare_loc_name']} ({row['rare_seen_at']})")
        months = _months_for_species(seasonality, row["species_code"])
        if months:
            in_season = current_month in months
            tag = "✅ in season" if in_season else "⚠️ off-season"
            st.markdown(f"{tag} — {_months_strip(months, current_month)}", unsafe_allow_html=True)
        summary = _clean_str(row.get("summary"))
        if summary:
            st.write(summary)
        ebird_url = _ebird_species_url(row.get("species_code"))
        wiki_url = _clean_str(row.get("wiki_url"))
        link_parts = []
        if ebird_url:
            link_parts.append(f"[eBird ↗]({ebird_url})")
        if wiki_url:
            link_parts.append(f"[Wikipedia ↗]({wiki_url})")
        if link_parts:
            st.markdown(" · ".join(link_parts))

        # Where to find this bird — collapsed by default.
        if hotspots_for_species is not None and not hotspots_for_species.empty:
            n = len(hotspots_for_species)
            label = f"📍 seen at {n} hotspot{'s' if n != 1 else ''}"
            with st.expander(label, expanded=False):
                _render_hotspot_list(
                    hotspots_for_species, current_hotspot_id=current_hotspot_id
                )
        else:
            hotspot_count = row.get("hotspot_count")
            if hotspot_count is not None and int(hotspot_count) > 0:
                st.caption(f"📍 seen at {int(hotspot_count)} hotspot(s)")
    st.divider()


_LAST_SEEN_WINDOWS: dict[str, int | None] = {
    "Any time": None,
    "Today": 0,
    "Past 3 days": 3,
    "Past week": 7,
    "Past 2 weeks": 14,
    "Past month": 30,
}


def _filter_target_rows(
    df: pd.DataFrame,
    seasonality: pd.DataFrame,
    *,
    key_prefix: str,
    has_last_seen: bool,
    region_options: list[str] | None = None,
) -> pd.DataFrame:
    """Render the filter/sort bar and return the filtered, sorted DataFrame.

    Expected columns on `df`: species_code, common_name, sci_name, is_rare,
    plus optionally `_last_seen` (ISO string) when has_last_seen=True.

    When `region_options` is non-empty, also render a region multi-select
    bound to the shared `?region=` URL param so it stays in sync with the
    chip strip; the filtering itself happens upstream off the URL.
    """
    # Month-in-season filter: each option means "show species typically present
    # this month". The default ("Any month") doesn't filter by seasonality.
    months_labels = ["Any month (no season filter)"] + [
        f"In season in {calendar.month_name[i]}" for i in range(1, 13)
    ]
    sort_choices = ["Rare first, then recent" if has_last_seen else "Rare first, then name"]
    sort_choices += ["Name (A→Z)"]
    if has_last_seen:
        sort_choices += ["Last seen (newest)"]
    if "hotspot_count" in df.columns:
        sort_choices += ["Most hotspots"]

    show_region = bool(region_options)
    if show_region:
        col_widths = [3, 3, 2, 2, 2, 2] if has_last_seen else [3, 3, 2, 2, 2]
    else:
        col_widths = [3, 2, 2, 2, 2] if has_last_seen else [3, 2, 2, 2]
    cols = st.columns(col_widths)
    q_key = f"{key_prefix}_q"
    # Search input + a small × clear button. Streamlit's text_input is
    # `type="text"`, so the browser's native search-clear button won't show;
    # we render our own. The clear is wired through `on_click` (rather than
    # mutating session_state after the button click) because Streamlit
    # disallows touching a widget's session_state value once that widget has
    # been instantiated in the current run.
    with cols[0]:
        search_cols = st.columns([10, 1])
        with search_cols[0]:
            q = st.text_input(
                "Search",
                placeholder="search name or scientific name…",
                key=q_key,
                label_visibility="collapsed",
            )
        with search_cols[1]:
            st.button(
                "✕",
                key=f"{key_prefix}_q_clear",
                help="Clear search",
                disabled=not q,
                on_click=lambda k=q_key: st.session_state.update({k: ""}),
            )
    next_col = 1
    if show_region:
        # Sync widget state from the URL (the chip strip's source of truth)
        # before instantiating the multiselect, so chip clicks update the
        # widget on the next rerun.
        region_key = f"{key_prefix}_regions"
        url_regions = _parse_active_regions(st.query_params.get("region"))
        valid = [r for r in sorted(url_regions) if r in region_options]
        if region_key not in st.session_state or set(
            st.session_state[region_key]
        ) != set(valid):
            st.session_state[region_key] = valid

        def _on_region_change(k: str = region_key) -> None:
            sel = list(st.session_state.get(k, []))
            if sel:
                st.query_params["region"] = ",".join(sorted(sel))
            elif "region" in st.query_params:
                del st.query_params["region"]

        with cols[next_col]:
            st.multiselect(
                "Regions",
                options=region_options,
                key=region_key,
                on_change=_on_region_change,
                format_func=lambda r: (
                    "Other" if r == _REGION_NONE_SENTINEL else _region_label(r)
                ),
                placeholder="All regions",
                label_visibility="collapsed",
            )
        next_col += 1
    with cols[next_col]:
        rarity = st.selectbox(
            "Rarity",
            ["All birds", "Rare only", "Common only"],
            key=f"{key_prefix}_rarity",
            label_visibility="collapsed",
        )
    with cols[next_col + 1]:
        month = st.selectbox(
            "Month",
            months_labels,
            index=0,  # default: Any month
            key=f"{key_prefix}_month",
            label_visibility="collapsed",
        )
    if has_last_seen:
        with cols[next_col + 2]:
            window = st.selectbox(
                "Last seen",
                list(_LAST_SEEN_WINDOWS.keys()),
                key=f"{key_prefix}_window",
                label_visibility="collapsed",
            )
        with cols[next_col + 3]:
            sort = st.selectbox(
                "Sort",
                sort_choices,
                key=f"{key_prefix}_sort",
                label_visibility="collapsed",
            )
    else:
        window = "Any time"
        with cols[next_col + 2]:
            sort = st.selectbox(
                "Sort",
                sort_choices,
                key=f"{key_prefix}_sort",
                label_visibility="collapsed",
            )

    out = df.copy()
    # Text search.
    if q:
        ql = q.strip().lower()
        out = out[
            out["common_name"].fillna("").str.lower().str.contains(ql, regex=False)
            | out["sci_name"].fillna("").str.lower().str.contains(ql, regex=False)
        ]
    # Rarity.
    if rarity == "Rare only":
        out = out[out["is_rare"] == 1]
    elif rarity == "Common only":
        out = out[out["is_rare"] == 0]
    # Month-in-season. months_labels[0] is the "any" sentinel.
    if month != months_labels[0]:
        m_idx = months_labels.index(month)  # 1..12
        keep = {
            code
            for code in out["species_code"]
            if m_idx in _months_for_species(seasonality, code)
        }
        out = out[out["species_code"].isin(keep)]
    # Last-seen window.
    days = _LAST_SEEN_WINDOWS[window] if has_last_seen else None
    if days is not None:
        cutoff = dt.date.today() - dt.timedelta(days=days)
        def _in_window(v: object) -> bool:
            d = _parse_iso_date(v)
            return d is not None and d >= cutoff
        out = out[out["_last_seen"].apply(_in_window)]

    # Sort.
    if sort == "Name (A→Z)":
        out = out.sort_values("common_name", kind="mergesort")
    elif sort == "Most hotspots" and "hotspot_count" in out.columns:
        out = out.sort_values(
            ["is_rare", "hotspot_count"], ascending=[False, False], kind="mergesort"
        )
    elif sort == "Last seen (newest)" and has_last_seen:
        out = out.sort_values(
            ["is_rare", "_last_seen"], ascending=[False, False], kind="mergesort"
        )
    elif sort == "Rare first, then recent" and has_last_seen:
        out = out.sort_values(
            ["is_rare", "_last_seen"], ascending=[False, False], kind="mergesort"
        )
    else:  # "Rare first, then name"
        out = out.sort_values(
            ["is_rare", "common_name"], ascending=[False, True], kind="mergesort"
        )
    return out


def _hotspots_by_species(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Group hotspot_targets by species_code and join in hotspot names."""
    ht = data["hotspot_targets"]
    if ht.empty:
        return {}
    named = ht.merge(
        data["hotspots"][["hotspot_id", "name"]], on="hotspot_id", how="left"
    )
    # `groupby` returns `Hashable` keys generically — species_code is always
    # a string, so cast for the type signature.
    return {str(code): group for code, group in named.groupby("species_code")}


def render_targets(data: dict[str, pd.DataFrame]) -> None:
    targets = data["targets"]
    seasonality = data["seasonality"]
    hotspots = data["hotspots"]
    if targets.empty:
        st.warning("No target birds yet. Run the pipeline.")
        return

    # Region filter: shares `?region=` with the map view, so toggles persist
    # across tabs. Counts on chips reflect hotspots in scope, not species.
    active_regions = _parse_active_regions(st.query_params.get("region"))
    _region_chips_panel(hotspots, hotspots.iloc[0:0], active_regions)

    ht = data["hotspot_targets"]
    if active_regions:
        kept_hids = set(hotspots[_region_mask(hotspots, active_regions)]["hotspot_id"])
        ht = ht[ht["hotspot_id"].isin(kept_hids)]
        codes_in_region = set(ht["species_code"]) if not ht.empty else set()
        targets = targets[targets["species_code"].isin(codes_in_region)]
        if targets.empty:
            st.info("No target species in the selected regions.")
            return

    # Enrich each target with the most recent observation date across all
    # hotspots, so the filter bar can sort/filter by "last seen" globally.
    enriched = targets.copy()
    if not ht.empty and "last_seen" in ht.columns:
        last_by_code = ht.groupby("species_code")["last_seen"].max()
        enriched["_last_seen"] = enriched["species_code"].map(last_by_code).fillna("")
    else:
        enriched["_last_seen"] = ""

    # Region multiselect options: every distinct region across all hotspots
    # (not the post-filter set), ordered by descending hotspot count so the
    # most populated regions surface first. Same scope as the chip strip.
    region_counts: dict[str, int] = {}
    if "region" in hotspots.columns:
        for r in hotspots["region"]:
            key = r if isinstance(r, str) and r else _REGION_NONE_SENTINEL
            region_counts[key] = region_counts.get(key, 0) + 1
    region_options = (
        sorted(region_counts.keys(), key=lambda k: (-region_counts[k], k))
        if len(region_counts) > 1
        else []
    )

    filtered = _filter_target_rows(
        enriched,
        seasonality,
        key_prefix="targets",
        has_last_seen=True,
        region_options=region_options,
    )
    total_rare = int((targets["is_rare"] == 1).sum())
    shown_rare = int((filtered["is_rare"] == 1).sum())
    st.caption(
        f"showing {len(filtered)} of {len(targets)} target species "
        f"({shown_rare} rare shown / {total_rare} total)"
    )
    if filtered.empty:
        st.info("No targets match these filters.")
        return
    by_species = _hotspots_by_species(data)
    current_month = dt.date.today().month
    for _, row in filtered.iterrows():
        # Surface the species' freshest sighting on its card.
        last_seen = row.get("_last_seen") or None
        _render_target_card(
            row,
            seasonality,
            current_month,
            last_seen=last_seen,
            hotspots_for_species=by_species.get(row["species_code"]),
        )


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
    components.html(m.get_root().render(), height=height, scrolling=False)


def render_consolidated_detail(
    consolidated_id: str, data: dict[str, pd.DataFrame]
) -> None:
    """Detail view for a consolidated hotspot: deduped species across members."""
    consolidated = data["consolidated_hotspots"]
    match = consolidated[consolidated["consolidated_id"] == consolidated_id]
    if match.empty:
        st.error(f"Consolidated hotspot `{consolidated_id}` not found.")
        st.markdown("[← Back to map](./)")
        return
    c = match.iloc[0]
    _render_compare_tray(data)

    members = data["consolidated_members"]
    members = members[members["consolidated_id"] == consolidated_id]

    back_col, title_col = st.columns([1, 9])
    with back_col:
        st.markdown(
            "<a href='./' target='_self' "
            "style='display:inline-block;padding:6px 12px;border-radius:8px;"
            "background:#f0f2f6;color:#222;text-decoration:none;font-weight:600;'>"
            "← Map</a>",
            unsafe_allow_html=True,
        )
    with title_col:
        target_count = int(c["target_count"] or 0)
        rare_count = int(c["rare_target_count"] or 0)
        member_count = int(c["member_count"] or 0)
        rare_badge = (
            f"<span style='background:#e74c3c;color:white;padding:2px 8px;"
            f"border-radius:10px;font-size:12px;font-weight:700;margin-left:8px;'>"
            f"🚨 {rare_count} rare</span>"
            if rare_count
            else ""
        )
        cons_badge = (
            f"<span style='background:#1f4f99;color:white;padding:2px 8px;"
            f"border-radius:10px;font-size:12px;font-weight:700;margin-left:8px;'>"
            f"⊕ {member_count} hotspots</span>"
        )
        st.markdown(
            f"<h2 style='margin:0;'>{c['name'] or consolidated_id}{cons_badge}{rare_badge}</h2>"
            f"<div style='color:#666;font-size:13px;margin-top:2px;'>"
            f"{target_count} unique target species · "
            f"<a href='https://www.google.com/maps/search/?api=1&query={c['lat']},{c['lon']}' "
            f"target='_blank' style='color:#1f4f99;text-decoration:none;'>open area in Maps ↗</a>"
            f"</div>",
            unsafe_allow_html=True,
        )
    btn_cols = st.columns([2, 3, 7])
    with btn_cols[0]:
        _compare_toggle_button(consolidated_id, key=f"compare_{consolidated_id}")
    member_ids = [mid for mid in members["hotspot_id"]] if not members.empty else []
    if member_ids:
        current = _compare_ids()
        already = [mid for mid in member_ids if mid in current]
        capacity = MAX_COMPARE - len(current)
        addable = [mid for mid in member_ids if mid not in current][:capacity]
        with btn_cols[1]:
            if not addable:
                if len(already) == len(member_ids):
                    st.caption("✓ All members in compare")
                else:
                    st.caption(f"Compare full ({MAX_COMPARE})")
            else:
                label = (
                    f"+ Add all {len(addable)} members"
                    if len(addable) == len(member_ids)
                    else f"+ Add {len(addable)} more members"
                )
                if st.button(label, key=f"compare_members_{consolidated_id}"):
                    new_ids = current + addable
                    st.query_params["compare"] = ",".join(new_ids)
                    if "view" in st.query_params:
                        del st.query_params["view"]
                    st.rerun()
    st.divider()

    # Location map — every member hotspot, fit-bounded.
    if not members.empty and {"lat", "lon"}.issubset(members.columns):
        member_pts = [
            {
                "hotspot_id": mm["hotspot_id"],
                "name": mm.get("name") or mm["hotspot_id"],
                "lat": mm["lat"],
                "lon": mm["lon"],
            }
            for _, mm in members.iterrows()
            if pd.notna(mm.get("lat")) and pd.notna(mm.get("lon"))
        ]
        _detail_location_map(member_pts, height=320)

    # Member hotspot list — link to each individual detail page.
    if not members.empty:
        st.markdown("**Member hotspots**")
        items = []
        for _, m in members.iterrows():
            mname = m.get("name") or m["hotspot_id"]
            items.append(
                f"<li style='margin:2px 0;'>"
                f"<a href='{_hotspot_url(m['hotspot_id'])}' target='_self' "
                f"style='color:#1f4f99;text-decoration:none;font-weight:600;'>"
                f"{mname}</a> "
                f"<span style='color:#888;font-size:11px;'>({m['hotspot_id']})</span>"
                f"</li>"
            )
        st.markdown(
            "<ul style='margin:4px 0 12px 0;padding-left:18px;'>"
            + "".join(items)
            + "</ul>",
            unsafe_allow_html=True,
        )

    # Deduped target species across all members. Rebuild from the full targets
    # table so we get every metadata column (sci_name, family, summary…).
    here = data["consolidated_targets"]
    here = here[here["consolidated_id"] == consolidated_id]
    if here.empty:
        st.info("No target species recorded across these hotspots yet.")
        return

    obs_by_code = {
        r["species_code"]: (r.get("last_seen"), r.get("how_many"))
        for _, r in here.iterrows()
    }
    targets = data["targets"]
    species_codes = set(here["species_code"])
    filtered = targets[targets["species_code"].isin(species_codes)].copy()
    if filtered.empty:
        st.caption(
            f"{len(here)} species observed across these hotspots "
            f"(full target metadata unavailable)."
        )
        return
    filtered["_last_seen"] = filtered["species_code"].map(
        lambda code: obs_by_code.get(code, (None, None))[0] or ""
    )
    sorted_filtered = _filter_target_rows(
        filtered,
        data["seasonality"],
        key_prefix=f"consolidated_{consolidated_id}",
        has_last_seen=True,
    )
    st.caption(
        f"showing {len(sorted_filtered)} of {len(filtered)} unique target species "
        f"across {member_count} hotspots"
    )
    if sorted_filtered.empty:
        st.info("No targets match these filters.")
        return
    by_species = _hotspots_by_species(data)
    current_month = dt.date.today().month
    for _, row in sorted_filtered.iterrows():
        last_seen, how_many = obs_by_code.get(row["species_code"], (None, None))
        _render_target_card(
            row,
            data["seasonality"],
            current_month,
            last_seen=last_seen,
            how_many=how_many,
            hotspots_for_species=by_species.get(row["species_code"]),
        )


def render_hotspot_detail(hotspot_id: str, data: dict[str, pd.DataFrame]) -> None:
    """Detail view for a single hotspot: header + filtered target cards."""
    hotspots = data["hotspots"]
    match = hotspots[hotspots["hotspot_id"] == hotspot_id]
    if match.empty:
        st.error(f"Hotspot `{hotspot_id}` not found.")
        st.markdown("[← Back to map](./)")
        return
    h = match.iloc[0]
    _render_compare_tray(data)

    # Header row: back link + hotspot name + stats
    back_col, title_col = st.columns([1, 9])
    with back_col:
        st.markdown(
            "<a href='./' target='_self' "
            "style='display:inline-block;padding:6px 12px;border-radius:8px;"
            "background:#f0f2f6;color:#222;text-decoration:none;font-weight:600;'>"
            "← Map</a>",
            unsafe_allow_html=True,
        )
    with title_col:
        target_count = int(h["target_count"] or 0)
        rare_count = int(h["rare_target_count"] or 0)
        rare_badge = (
            f"<span style='background:#e74c3c;color:white;padding:2px 8px;"
            f"border-radius:10px;font-size:12px;font-weight:700;margin-left:8px;'>"
            f"🚨 {rare_count} rare</span>"
            if rare_count
            else ""
        )
        st.markdown(
            f"<h2 style='margin:0;'>{h['name'] or hotspot_id}{rare_badge}</h2>"
            f"<div style='color:#666;font-size:13px;margin-top:2px;'>"
            f"{target_count} target species · "
            f"<a href='https://ebird.org/hotspot/{hotspot_id}' target='_blank' "
            f"style='color:#1f4f99;text-decoration:none;'>view on eBird ↗</a> · "
            f"<a href='https://www.google.com/maps/search/?api=1&query={h['lat']},{h['lon']}' "
            f"target='_blank' style='color:#1f4f99;text-decoration:none;'>open in Maps ↗</a>"
            f"</div>",
            unsafe_allow_html=True,
        )
    _compare_toggle_button(hotspot_id, key=f"compare_{hotspot_id}")
    st.divider()

    # Location map — single marker at this hotspot.
    if pd.notna(h.get("lat")) and pd.notna(h.get("lon")):
        _detail_location_map(
            [
                {
                    "hotspot_id": hotspot_id,
                    "name": h.get("name") or hotspot_id,
                    "lat": h["lat"],
                    "lon": h["lon"],
                }
            ],
            height=280,
            highlight_id=hotspot_id,
        )

    # Filter targets to species seen at this hotspot
    here = data["hotspot_targets"]
    here = here[here["hotspot_id"] == hotspot_id]
    if here.empty:
        st.info("No target species recorded at this hotspot yet.")
        return

    # Map species_code -> (last_seen, how_many) for this hotspot
    obs_by_code = {
        r["species_code"]: (r.get("last_seen"), r.get("how_many"))
        for _, r in here.iterrows()
    }

    targets = data["targets"]
    species_codes = set(here["species_code"])
    filtered = targets[targets["species_code"].isin(species_codes)].copy()
    if filtered.empty:
        # Targets table is empty but obs exist — fall back to bare hotspot_targets
        st.caption(f"{len(here)} species observed here (full target metadata unavailable).")
        for _, t in here.iterrows():
            flag = " 🚨" if t["is_rare"] else ""
            when = _days_ago(t.get("last_seen"))
            when_str = f" — *{when}*" if when else ""
            wiki_url = _clean_str(t.get("wiki_url"))
            link = f"[{t['common_name']}]({wiki_url})" if wiki_url else t["common_name"]
            st.markdown(f"- {link}{flag}{when_str}")
        return

    # Attach last_seen so filter/sort can use it.
    filtered["_last_seen"] = filtered["species_code"].map(
        lambda c: obs_by_code.get(c, (None, None))[0] or ""
    )

    sorted_filtered = _filter_target_rows(
        filtered,
        data["seasonality"],
        key_prefix=f"hotspot_{hotspot_id}",
        has_last_seen=True,
    )
    st.caption(
        f"showing {len(sorted_filtered)} of {len(filtered)} target species at this hotspot"
    )
    if sorted_filtered.empty:
        st.info("No targets match these filters.")
        return
    by_species = _hotspots_by_species(data)
    current_month = dt.date.today().month
    for _, row in sorted_filtered.iterrows():
        last_seen, how_many = obs_by_code.get(row["species_code"], (None, None))
        _render_target_card(
            row,
            data["seasonality"],
            current_month,
            last_seen=last_seen,
            how_many=how_many,
            hotspots_for_species=by_species.get(row["species_code"]),
            current_hotspot_id=hotspot_id,
        )


def _compare_species_at_item(
    item_id: str, data: dict[str, pd.DataFrame]
) -> dict[str, dict]:
    """Map species_code -> {last_seen, how_many, is_rare} for one compare item."""
    if _is_consolidated_id(item_id):
        df = data["consolidated_targets"]
        df = df[df["consolidated_id"] == item_id]
    else:
        df = data["hotspot_targets"]
        df = df[df["hotspot_id"] == item_id]
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        code = r["species_code"]
        if code in out:
            continue
        out[code] = {
            "last_seen": r.get("last_seen"),
            "how_many": r.get("how_many"),
            "is_rare": int(r.get("is_rare") or 0),
        }
    return out


_HOT_LABEL_MAX = 22


def _short_label(name: str) -> str:
    return name if len(name) <= _HOT_LABEL_MAX else name[: _HOT_LABEL_MAX - 1] + "…"


def _presence_strip_html(
    species_code: str,
    metas: list[dict],
    species_at: dict[str, dict[str, dict]],
) -> str:
    """Compact dot strip: one pill per active hotspot, filled if species present."""
    pills: list[str] = []
    for m in metas:
        sp = species_at[m["id"]].get(species_code)
        if sp:
            when = _days_ago(sp.get("last_seen")) or ""
            tip = f"{m['name']} · last seen {when}" if when else m["name"]
            pills.append(
                f"<span title='{tip}' "
                f"style='background:#1f4f99;color:white;padding:2px 7px;"
                f"border-radius:10px;font-size:11px;font-weight:600;'>"
                f"{_short_label(m['name'])}</span>"
            )
        else:
            pills.append(
                f"<span title='{m['name']} · not seen here' "
                f"style='background:#f0f2f6;color:#aaa;padding:2px 7px;"
                f"border-radius:10px;font-size:11px;font-weight:500;"
                f"text-decoration:line-through;'>"
                f"{_short_label(m['name'])}</span>"
            )
    return (
        "<div style='display:flex;flex-wrap:wrap;gap:4px;margin:2px 0 6px 0;'>"
        + "".join(pills)
        + "</div>"
    )


def render_compare(data: dict[str, pd.DataFrame]) -> None:
    """Side-by-side compare of selected hotspots/consolidations."""
    current = _compare_ids()
    metas_all = [m for m in (_compare_item_meta(i, data) for i in current) if m]
    if len(metas_all) < 2:
        st.info(
            "Add at least two hotspots to compare. Use the **+** on top hotspot "
            "chips, or the **+ Add to compare** button on a hotspot detail page."
        )
        st.markdown("[← Back to map](./)")
        return

    # Header.
    back_col, title_col = st.columns([1, 9])
    with back_col:
        st.markdown(
            "<a href='./' target='_self' "
            "style='display:inline-block;padding:6px 12px;border-radius:8px;"
            "background:#f0f2f6;color:#222;text-decoration:none;font-weight:600;'>"
            "← Map</a>",
            unsafe_allow_html=True,
        )
    with title_col:
        st.markdown(
            f"<h2 style='margin:0;'>Compare {len(metas_all)} hotspots</h2>"
            f"<div style='color:#666;font-size:13px;margin-top:2px;'>"
            f"common species across selected · unique to each · filterable</div>",
            unsafe_allow_html=True,
        )

    _render_compare_tray(data, on_compare_view=True)

    # Sub-selection: which compare items are "active" in the analysis below.
    active_key = f"compare_active::{','.join(current)}"
    if active_key not in st.session_state:
        st.session_state[active_key] = [m["id"] for m in metas_all]
    label_by_id = {m["id"]: m["name"] for m in metas_all}
    active_ids = st.multiselect(
        "Active in comparison",
        options=[m["id"] for m in metas_all],
        format_func=lambda i: label_by_id.get(i, i),
        key=active_key,
    )
    metas = [m for m in metas_all if m["id"] in set(active_ids)]
    if len(metas) < 2:
        st.warning("Select at least two hotspots above to compare.")
        return

    # Per-hotspot species filter — narrow the species list to those seen at a
    # specific active hotspot. Default "All" keeps the bucketed view.
    filter_options = ["All hotspots"] + [m["name"] for m in metas]
    filter_key = f"compare_only_hotspot::{','.join(active_ids)}"
    only_choice = st.selectbox(
        "Show only species at",
        filter_options,
        key=filter_key,
    )
    only_iid: str | None = None
    if only_choice != "All hotspots":
        only_iid = next(
            (m["id"] for m in metas if m["name"] == only_choice), None
        )

    # Combined location map.
    pts: list[dict] = []
    for m in metas:
        if pd.notna(m.get("lat")) and pd.notna(m.get("lon")):
            pts.append(
                {
                    "hotspot_id": m["id"],
                    "name": m["name"],
                    "lat": float(m["lat"]),
                    "lon": float(m["lon"]),
                }
            )
    if pts:
        _detail_location_map(pts, height=320, zoom=11)

    # Stats row.
    stat_cols = st.columns(len(metas))
    for col, m in zip(stat_cols, metas):
        with col:
            rare_badge = (
                f"<span style='background:#e74c3c;color:white;padding:1px 6px;"
                f"border-radius:8px;font-size:11px;font-weight:700;margin-left:6px;'>"
                f"🚨 {m['rare_count']}</span>"
                if m["rare_count"]
                else ""
            )
            cons_badge = (
                "<span style='background:#1f4f99;color:white;padding:1px 6px;"
                "border-radius:8px;font-size:11px;font-weight:700;margin-left:6px;'>"
                "⊕</span>"
                if m["kind"] == "consolidated"
                else ""
            )
            st.markdown(
                f"<div style='border:1px solid #e6ecf5;border-radius:10px;padding:8px 10px;'>"
                f"<div style='font-weight:700;font-size:13px;line-height:1.25;'>"
                f"<a href='{m['url']}' target='_self' "
                f"style='color:#1f4f99;text-decoration:none;'>{m['name']}</a>"
                f"{cons_badge}{rare_badge}</div>"
                f"<div style='color:#666;font-size:12px;margin-top:2px;'>"
                f"{m['target_count']} target species</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    st.divider()

    # Collect species per item.
    species_at: dict[str, dict[str, dict]] = {
        m["id"]: _compare_species_at_item(m["id"], data) for m in metas
    }
    species_sets = {iid: set(sp.keys()) for iid, sp in species_at.items()}
    union_codes: set[str] = set().union(*species_sets.values()) if species_sets else set()
    common_codes = (
        set.intersection(*species_sets.values()) if species_sets else set()
    )

    # Build a target frame for the union, with _last_seen = MAX across active items.
    targets = data["targets"]
    union_df = targets[targets["species_code"].isin(union_codes)].copy()
    if union_df.empty:
        st.info("No target species recorded at any of the selected hotspots.")
        return

    def _max_last_seen(code: str) -> str:
        vals = [
            species_at[m["id"]].get(code, {}).get("last_seen")
            for m in metas
            if code in species_at[m["id"]]
        ]
        vals = [v for v in vals if v]
        return max(vals) if vals else ""

    union_df["_last_seen"] = union_df["species_code"].map(_max_last_seen)
    union_df["_present_count"] = union_df["species_code"].map(
        lambda c: sum(1 for iid in species_sets if c in species_sets[iid])
    )

    # When the per-hotspot filter is active, restrict to that hotspot's species.
    scoped_df = (
        union_df[union_df["species_code"].isin(species_sets[only_iid])]
        if only_iid is not None
        else union_df
    )

    sorted_union = _filter_target_rows(
        scoped_df,
        data["seasonality"],
        key_prefix=f"compare_{','.join(current)}",
        has_last_seen=True,
    )
    if sorted_union.empty:
        st.info("No species match these filters.")
        return

    # Bucket by presence.
    n_active = len(metas)
    if only_iid is not None:
        # Single-hotspot filter: skip bucketing, show all matching species in
        # one flat section.
        common_rows = sorted_union.iloc[0:0]
        shared_rows = sorted_union.iloc[0:0]
        unique_rows = sorted_union.iloc[0:0]
        flat_rows = sorted_union
    else:
        common_rows = sorted_union[sorted_union["_present_count"] == n_active]
        unique_rows = sorted_union[sorted_union["_present_count"] == 1]
        shared_rows = sorted_union[
            (sorted_union["_present_count"] >= 2)
            & (sorted_union["_present_count"] < n_active)
        ]
        flat_rows = sorted_union.iloc[0:0]

    by_species = _hotspots_by_species(data)
    current_month = dt.date.today().month

    def _render_bucket(
        title: str, rows: pd.DataFrame, *, show_strip: bool
    ) -> None:
        if rows.empty:
            return
        st.markdown(
            f"<h3 style='margin:8px 0 4px 0;'>{title} "
            f"<span style='color:#888;font-size:13px;font-weight:500;'>"
            f"({len(rows)})</span></h3>",
            unsafe_allow_html=True,
        )
        for _, row in rows.iterrows():
            if show_strip:
                st.markdown(
                    _presence_strip_html(row["species_code"], metas, species_at),
                    unsafe_allow_html=True,
                )
            # Pick a single last_seen/how_many to display — use the most recent
            # active item's data for this species.
            best_iid: str | None = None
            best_when = ""
            for m in metas:
                sp = species_at[m["id"]].get(row["species_code"])
                if sp and (sp.get("last_seen") or "") > best_when:
                    best_when = sp.get("last_seen") or ""
                    best_iid = m["id"]
            sp_meta = (
                species_at[best_iid].get(row["species_code"])
                if best_iid
                else None
            )
            _render_target_card(
                row,
                data["seasonality"],
                current_month,
                last_seen=(sp_meta.get("last_seen") if sp_meta else None),
                how_many=(sp_meta.get("how_many") if sp_meta else None),
                hotspots_for_species=by_species.get(row["species_code"]),
            )

    if only_iid is not None:
        only_meta = next(m for m in metas if m["id"] == only_iid)
        _render_bucket(
            f"Species at {only_meta['name']}", flat_rows, show_strip=True
        )
    else:
        _render_bucket(
            f"✅ Common to all {n_active}", common_rows, show_strip=False
        )
        _render_bucket("🔀 Shared by some", shared_rows, show_strip=True)

    # Unique-to-each: break out per hotspot. Skipped when filtering to a
    # single hotspot, since the flat bucket above already covers it.
    if only_iid is None and not unique_rows.empty:
        st.markdown(
            f"<h3 style='margin:12px 0 4px 0;'>⭐ Unique to one hotspot "
            f"<span style='color:#888;font-size:13px;font-weight:500;'>"
            f"({len(unique_rows)})</span></h3>",
            unsafe_allow_html=True,
        )
        # Group by which hotspot owns each unique species.
        rows_by_iid: dict[str, list[pd.Series]] = {m["id"]: [] for m in metas}
        for _, row in unique_rows.iterrows():
            for m in metas:
                if row["species_code"] in species_sets[m["id"]]:
                    rows_by_iid[m["id"]].append(row)
                    break
        for m in metas:
            owned = rows_by_iid[m["id"]]
            if not owned:
                continue
            with st.expander(
                f"{m['name']} — {len(owned)} unique", expanded=False
            ):
                for row in owned:
                    sp_meta = species_at[m["id"]].get(row["species_code"], {})
                    _render_target_card(
                        row,
                        data["seasonality"],
                        current_month,
                        last_seen=sp_meta.get("last_seen"),
                        how_many=sp_meta.get("how_many"),
                        hotspots_for_species=by_species.get(row["species_code"]),
                        current_hotspot_id=(
                            m["id"] if m["kind"] == "hotspot" else None
                        ),
                    )

    if common_rows.empty and shared_rows.empty and unique_rows.empty:
        st.info("No species match these filters.")


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
</style>
"""


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
