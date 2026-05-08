"""Hotspot compare feature: state, tray, popup pill, and the compare view."""

import datetime as dt
import json
from dataclasses import dataclass
from enum import Enum

import altair as alt
import pandas as pd
import streamlit as st

from onlybirds.dashboard.data import DashboardData
from onlybirds.dashboard.compare_client import (
    popup_pill_html,
    render_pill,
    render_tray,
)
from onlybirds.dashboard.mini_map import _detail_location_map
from onlybirds.dashboard.targets_view import (
    _filter_target_rows,
    _hotspots_by_species,
    _render_target_card,
)
from onlybirds.dashboard.types import HotspotKind
from onlybirds.dashboard.urls import _consolidated_url, _hotspot_url
from onlybirds.dashboard.utils import _days_ago

MAX_COMPARE = 6


class SpeciesBucket(str, Enum):
    UNIQUE_TO_THIS = "Unique to this"
    SHARED_WITH_SOME = "Shared with some"
    COMMON_TO_ALL = "Common to all"


@dataclass(frozen=True, slots=True)
class CompareItemMeta:
    id: str
    kind: HotspotKind
    name: str
    lat: float
    lon: float
    target_count: int
    rare_count: int
    url: str


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


def _compare_item_meta(
    item_id: str, data: DashboardData
) -> CompareItemMeta | None:
    if _is_consolidated_id(item_id):
        c = data.consolidated_hotspots
        m = c[c["consolidated_id"] == item_id]
        if m.empty:
            return None
        r = m.iloc[0]
        return CompareItemMeta(
            id=item_id,
            kind=HotspotKind.CONSOLIDATED,
            name=r["name"] or item_id,
            lat=r["lat"],
            lon=r["lon"],
            target_count=int(r.get("target_count") or 0),
            rare_count=int(r.get("rare_target_count") or 0),
            url=_consolidated_url(item_id),
        )
    h = data.hotspots
    m = h[h["hotspot_id"] == item_id]
    if m.empty:
        return None
    r = m.iloc[0]
    return CompareItemMeta(
        id=item_id,
        kind=HotspotKind.HOTSPOT,
        name=r["name"] or item_id,
        lat=r["lat"],
        lon=r["lon"],
        target_count=int(r.get("target_count") or 0),
        rare_count=int(r.get("rare_target_count") or 0),
        url=_hotspot_url(item_id),
    )


def _render_compare_tray(
    data: DashboardData, *, on_compare_view: bool = False
) -> None:
    """Sticky chip strip showing the current compare selection.

    Renders as a localStorage-driven iframe so add/remove ops don't trigger
    a streamlit rerun (which re-mounts the folium map iframe → flash). The
    iframe reads/writes `window.top.localStorage['onlybirds.compare']`.
    """
    render_tray(data, on_compare_view=on_compare_view)


def _compare_toggle_button(item_id: str, *, key: str) -> None:
    """Add/remove pill for detail pages — toggles localStorage in place.

    Implemented as an iframe (`components.html`) so clicking doesn't trigger
    a streamlit rerun. State and label sync via the `storage` event when
    other tabs/iframes change the compare list.
    """
    kind = (
        HotspotKind.CONSOLIDATED
        if _is_consolidated_id(item_id)
        else HotspotKind.HOTSPOT
    )
    render_pill(item_id, kind=kind)
    _ = key  # kept for backward compat with callers


def _compare_species_at_item(
    item_id: str, data: DashboardData
) -> dict[str, dict]:
    """Map species_code -> {last_seen, how_many, is_rare} for one compare item."""
    if _is_consolidated_id(item_id):
        df = data.consolidated_targets
        df = df[df["consolidated_id"] == item_id]
    else:
        df = data.hotspot_targets
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
    metas: list[CompareItemMeta],
    species_at: dict[str, dict[str, dict]],
) -> str:
    """Compact dot strip: one pill per active hotspot, filled if species present."""
    pills: list[str] = []
    for m in metas:
        sp = species_at[m.id].get(species_code)
        if sp:
            when = _days_ago(sp.get("last_seen")) or ""
            tip = f"{m.name} · last seen {when}" if when else m.name
            pills.append(
                f"<span title='{tip}' "
                f"style='background:#1f4f99;color:white;padding:2px 7px;"
                f"border-radius:10px;font-size:11px;font-weight:600;'>"
                f"{_short_label(m.name)}</span>"
            )
        else:
            pills.append(
                f"<span title='{m.name} · not seen here' "
                f"style='background:#f0f2f6;color:#aaa;padding:2px 7px;"
                f"border-radius:10px;font-size:11px;font-weight:500;"
                f"text-decoration:line-through;'>"
                f"{_short_label(m.name)}</span>"
            )
    return (
        "<div style='display:flex;flex-wrap:wrap;gap:4px;margin:2px 0 6px 0;'>"
        + "".join(pills)
        + "</div>"
    )


def render_compare(data: DashboardData) -> None:
    """Side-by-side compare of selected hotspots/consolidations."""
    current = _compare_ids()
    # Deep links arrive with `?compare=` populated but localStorage empty;
    # mirror the URL list into localStorage so the tray (which now reads
    # from localStorage) stays in sync after the user leaves this view.
    if current:
        sync_js = (
            "<script>try{"
            "var k='onlybirds.compare';"
            f"var v={json.dumps(','.join(current))};"
            "if(window.top.localStorage.getItem(k)!==v){"
            "window.top.localStorage.setItem(k,v);"
            "window.top.dispatchEvent(new CustomEvent('onlybirds:compare',{detail:v.split(',')}));"
            "}}catch(e){}</script>"
        )
        st.iframe(sync_js, height=0)
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
        st.session_state[active_key] = [m.id for m in metas_all]
    label_by_id = {m.id: m.name for m in metas_all}
    active_ids = st.multiselect(
        "Active in comparison",
        options=[m.id for m in metas_all],
        format_func=lambda i: label_by_id.get(i, i),
        key=active_key,
    )
    metas = [m for m in metas_all if m.id in set(active_ids)]
    if len(metas) < 2:
        st.warning("Select at least two hotspots above to compare.")
        return

    # Per-hotspot species filter — narrow the species list to those seen at a
    # specific active hotspot. Default "All" keeps the bucketed view.
    filter_options = ["All hotspots"] + [m.name for m in metas]
    filter_key = f"compare_only_hotspot::{','.join(active_ids)}"
    # Version suffix lets the "✕ clear" button force a fresh chart component
    # (incrementing the version → new key → streamlit remounts the iframe →
    # vega's internal selection state is gone).
    version_key = f"compare_chart_version::{','.join(active_ids)}"
    chart_version = st.session_state.get(version_key, 0)
    chart_key = f"compare_chart::{','.join(active_ids)}::v{chart_version}"
    applied_key = f"compare_chart_applied::{','.join(active_ids)}"
    bucket_key = f"compare_chart_bucket::{','.join(active_ids)}"

    # Sync a prior chart click into the selectbox state before the widget
    # renders. Change-detection on `applied_key` avoids re-forcing the value
    # on every rerun (which would block manual selectbox changes).
    chart_state = st.session_state.get(chart_key, {})
    seg_list = (chart_state.get("selection") or {}).get("seg") or []
    sel_tuple = (
        (seg_list[0].get("iid"), seg_list[0].get("bucket")) if seg_list else None
    )
    if sel_tuple != st.session_state.get(applied_key):
        st.session_state[applied_key] = sel_tuple
        if sel_tuple:
            seg_iid, seg_bucket = sel_tuple
            seg_name = next(
                (m.name for m in metas_all if m.id == seg_iid), None
            )
            if seg_name and seg_name in filter_options:
                st.session_state[filter_key] = seg_name
            st.session_state[bucket_key] = seg_bucket
        else:
            st.session_state[bucket_key] = None

    only_choice = st.selectbox(
        "Show only species at",
        filter_options,
        key=filter_key,
    )
    only_iid: str | None = None
    if only_choice != "All hotspots":
        only_iid = next(
            (m.id for m in metas if m.name == only_choice), None
        )

    # Combined location map.
    pts: list[dict] = []
    for m in metas:
        if pd.notna(m.lat) and pd.notna(m.lon):
            pts.append(
                {
                    "hotspot_id": m.id,
                    "name": m.name,
                    "lat": float(m.lat),
                    "lon": float(m.lon),
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
                f"🚨 {m.rare_count}</span>"
                if m.rare_count
                else ""
            )
            cons_badge = (
                "<span style='background:#1f4f99;color:white;padding:1px 6px;"
                "border-radius:8px;font-size:11px;font-weight:700;margin-left:6px;'>"
                "⊕</span>"
                if m.kind == HotspotKind.CONSOLIDATED
                else ""
            )
            st.markdown(
                f"<div style='border:1px solid #e6ecf5;border-radius:10px;padding:8px 10px;'>"
                f"<div style='font-weight:700;font-size:13px;line-height:1.25;'>"
                f"<a href='{m.url}' target='_self' "
                f"style='color:#1f4f99;text-decoration:none;'>{m.name}</a>"
                f"{cons_badge}{rare_badge}</div>"
                f"<div style='color:#666;font-size:12px;margin-top:2px;'>"
                f"{m.target_count} target species</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    # Collect species per item (needed for the bucket chart below).
    species_at: dict[str, dict[str, dict]] = {
        m.id: _compare_species_at_item(m.id, data) for m in metas
    }
    species_sets = {iid: set(sp.keys()) for iid, sp in species_at.items()}
    union_codes: set[str] = set().union(*species_sets.values()) if species_sets else set()

    # Bucket helpers used by both the chart (rendered after filters) and
    # the bucket-filter logic that narrows the species list below.
    n_active = len(metas)
    _BUCKET_ORDER = [b.value for b in SpeciesBucket]
    _present_count_by_code = {
        code: sum(1 for s in species_sets.values() if code in s)
        for code in union_codes
    }

    def _bucket_for(code: str) -> SpeciesBucket:
        pc = _present_count_by_code.get(code, 0)
        if pc >= n_active:
            return SpeciesBucket.COMMON_TO_ALL
        if pc <= 1:
            return SpeciesBucket.UNIQUE_TO_THIS
        return SpeciesBucket.SHARED_WITH_SOME

    selected_bucket: str | None = st.session_state.get(bucket_key)

    st.divider()

    # Build a target frame for the union, with _last_seen = MAX across active items.
    targets = data.targets
    union_df = targets[targets["species_code"].isin(union_codes)].copy()
    if union_df.empty:
        st.info("No target species recorded at any of the selected hotspots.")
        return

    def _max_last_seen(code: str) -> str:
        vals = [
            species_at[m.id].get(code, {}).get("last_seen")
            for m in metas
            if code in species_at[m.id]
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
        data.seasonality,
        key_prefix=f"compare_{','.join(current)}",
        has_last_seen=True,
    )
    if sorted_union.empty:
        st.info("No species match these filters.")
        return

    # Stacked-bar overview — counts reflect the filters above. Clicking a
    # segment narrows the species list to that bucket (the chart itself
    # always shows the full breakdown so other segments stay reachable).
    filtered_codes = set(sorted_union["species_code"])
    code_to_name = dict(zip(sorted_union["species_code"], sorted_union["common_name"]))
    bucket_rows: list[dict] = []
    for m in metas:
        species_by_bucket: dict[str, list[str]] = {b: [] for b in _BUCKET_ORDER}
        for code in species_sets[m.id]:
            if code in filtered_codes:
                species_by_bucket[_bucket_for(code).value].append(
                    code_to_name.get(code, code)
                )
        for b in _BUCKET_ORDER:
            names = sorted(species_by_bucket[b])
            # Cap the tooltip list — Vega-Lite tooltips truncate very long
            # strings, and a 100-bird list isn't useful anyway.
            shown = names[:30]
            suffix = (
                f"\n…and {len(names) - 30} more" if len(names) > 30 else ""
            )
            bucket_rows.append(
                {
                    "hotspot": _short_label(m.name),
                    "iid": m.id,
                    "bucket": b,
                    "count": len(names),
                    "species": "\n".join(shown) + suffix if shown else "",
                }
            )
    bucket_df = pd.DataFrame(bucket_rows)
    hotspot_order = [_short_label(m.name) for m in metas]
    _bucket_order_idx = {b: i for i, b in enumerate(_BUCKET_ORDER)}
    bucket_df["_order"] = bucket_df["bucket"].map(_bucket_order_idx)

    seg_param = alt.selection_point(
        name="seg", fields=["iid", "bucket"], empty=True, on="click"
    )
    base = alt.Chart(bucket_df).encode(
        y=alt.Y(
            "hotspot:N",
            sort=hotspot_order,
            axis=alt.Axis(title=None, labelLimit=200),
            scale=alt.Scale(paddingInner=0.3, paddingOuter=0.2),
        ),
        x=alt.X(
            "count:Q",
            stack="zero",
            axis=alt.Axis(title="target species"),
        ),
        order=alt.Order("_order:Q"),
    )
    bars = base.mark_bar(cursor="pointer").encode(
        color=alt.Color(
            "bucket:N",
            scale=alt.Scale(
                domain=_BUCKET_ORDER,
                range=["#1f4f99", "#7fb8e0", "#bdbdbd"],
            ),
            legend=alt.Legend(orient="top", title=None),
            sort=_BUCKET_ORDER,
        ),
        tooltip=[
            alt.Tooltip("hotspot:N", title="hotspot"),
            alt.Tooltip("bucket:N", title="bucket"),
            alt.Tooltip("count:Q", title="count"),
            alt.Tooltip("species:N", title="species"),
        ],
        opacity=alt.condition(seg_param, alt.value(1.0), alt.value(0.55)),
    ).add_params(seg_param)
    labels = base.mark_text(
        align="center",
        baseline="middle",
        color="white",
        fontWeight="bold",
        fontSize=12,
    ).encode(
        text=alt.condition("datum.count > 0", alt.Text("count:Q"), alt.value("")),
    ).transform_filter("datum.count > 0")
    chart = (bars + labels).properties(height=max(60 * len(metas) + 80, 200))
    st.markdown(
        "<div style='font-weight:600;font-size:13px;margin:8px 0 4px 0;color:#666;'>"
        "Species breakdown — click a segment to filter</div>"
        # Vega-tooltip collapses '\n' into spaces by default; pre-line keeps
        # the species list one-per-line. Scoped to vega-tooltip's value cell.
        "<style>#vg-tooltip-element .value, #vg-tooltip-element .key "
        "{ white-space: pre-line !important; }</style>",
        unsafe_allow_html=True,
    )
    st.altair_chart(
        chart,
        width="stretch",
        on_select="rerun",
        key=chart_key,
    )

    if selected_bucket and only_choice != "All hotspots":
        # Callback fires before widget re-instantiation on the next rerun,
        # which is the only window where streamlit lets us mutate widget-
        # owned session state (chart_key) without raising.
        def _clear_seg(
            _ck: str = chart_key,
            _bk: str = bucket_key,
            _ak: str = applied_key,
            _vk: str = version_key,
            _fk: str = filter_key,
        ) -> None:
            st.session_state[_bk] = None
            st.session_state[_ak] = None
            st.session_state.pop(_ck, None)
            st.session_state[_fk] = "All hotspots"
            st.session_state[_vk] = st.session_state.get(_vk, 0) + 1

        cb_cols = st.columns([6, 1])
        with cb_cols[0]:
            st.markdown(
                f"<div style='background:#eef3fb;color:#1f4f99;padding:6px 10px;"
                f"border-radius:8px;font-size:13px;font-weight:600;display:inline-block;'>"
                f"Filtering to <b>{selected_bucket.lower()}</b> at "
                f"<b>{only_choice}</b></div>",
                unsafe_allow_html=True,
            )
        with cb_cols[1]:
            st.button(
                "✕ clear", key=f"clear_seg_{chart_key}", on_click=_clear_seg
            )

    # Bar-chart segment click narrows the species list to that bucket. Only
    # meaningful when a single hotspot is active (otherwise "unique to this"
    # has no anchor).
    if only_iid is not None and selected_bucket:
        if selected_bucket == SpeciesBucket.UNIQUE_TO_THIS:
            sorted_union = sorted_union[sorted_union["_present_count"] <= 1]
        elif selected_bucket == SpeciesBucket.COMMON_TO_ALL:
            sorted_union = sorted_union[sorted_union["_present_count"] >= n_active]
        else:
            sorted_union = sorted_union[
                (sorted_union["_present_count"] >= 2)
                & (sorted_union["_present_count"] < n_active)
            ]
        if sorted_union.empty:
            st.info("No species match these filters.")
            return

    # Bucket by presence.
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
                sp = species_at[m.id].get(row["species_code"])
                if sp and (sp.get("last_seen") or "") > best_when:
                    best_when = sp.get("last_seen") or ""
                    best_iid = m.id
            sp_meta = (
                species_at[best_iid].get(row["species_code"])
                if best_iid
                else None
            )
            _render_target_card(
                row,
                data.seasonality,
                current_month,
                last_seen=(sp_meta.get("last_seen") if sp_meta else None),
                how_many=(sp_meta.get("how_many") if sp_meta else None),
                hotspots_for_species=by_species.get(row["species_code"]),
            )

    if only_iid is not None:
        only_meta = next(m for m in metas if m.id == only_iid)
        _render_bucket(
            f"Species at {only_meta.name}", flat_rows, show_strip=True
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
        rows_by_iid: dict[str, list[pd.Series]] = {m.id: [] for m in metas}
        for _, row in unique_rows.iterrows():
            for m in metas:
                if row["species_code"] in species_sets[m.id]:
                    rows_by_iid[m.id].append(row)
                    break
        for m in metas:
            owned = rows_by_iid[m.id]
            if not owned:
                continue
            with st.expander(
                f"{m.name} — {len(owned)} unique", expanded=False
            ):
                for row in owned:
                    sp_meta = species_at[m.id].get(row["species_code"], {})
                    _render_target_card(
                        row,
                        data.seasonality,
                        current_month,
                        last_seen=sp_meta.get("last_seen"),
                        how_many=sp_meta.get("how_many"),
                        hotspots_for_species=by_species.get(row["species_code"]),
                        current_hotspot_id=(
                            m.id if m.kind == HotspotKind.HOTSPOT else None
                        ),
                    )

    if common_rows.empty and shared_rows.empty and unique_rows.empty:
        st.info("No species match these filters.")
