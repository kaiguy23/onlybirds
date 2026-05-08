"""Hotspot compare feature: state, tray, popup pill, and the compare view."""

import datetime as dt

import pandas as pd
import streamlit as st

from onlybirds.dashboard.mini_map import _detail_location_map
from onlybirds.dashboard.targets_view import (
    _filter_target_rows,
    _hotspots_by_species,
    _render_target_card,
)
from onlybirds.dashboard.urls import _consolidated_url, _hotspot_url
from onlybirds.dashboard.utils import _days_ago

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
    _ = key  # kept for backward compat with callers


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
