"""Page views: map, hotspot detail, consolidated detail, and the top-hotspots strip."""

import datetime as dt
import json

import folium
import pandas as pd
import streamlit as st
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

from onlybirds.dashboard.compare import (
    MAX_COMPARE,
    _compare_ids,
    _compare_toggle_button,
    _render_compare_tray,
)
from onlybirds.dashboard.compare_client import render_top_hotspots_strip
from onlybirds.dashboard.data import (
    DashboardData,
    load_consolidated_all_species,
    load_hotspot_all_species,
)
from onlybirds.dashboard.markers import (
    _CLUSTER_ICON_FN,
    _LEGEND_HTML,
    _consolidated_marker_html,
    _consolidated_popup_html,
    _consolidated_tooltip_html,
    _marker_div_html,
    _popup_html,
    _tooltip_html,
)
from onlybirds.dashboard.mini_map import _detail_location_map
from onlybirds.dashboard.regions import (
    _REGION_PREVIEW_HTML_TEMPLATE,
    _parse_active_regions,
    _region_bboxes,
    _region_chips_panel,
    _region_mask,
)
from onlybirds.dashboard.semantic_widget import (
    apply_semantic_search,
    current_description,
    render_semantic_search,
)
from onlybirds.dashboard.targets_view import (
    _filter_target_rows,
    _hotspots_by_species,
    _render_target_card,
)
from onlybirds.dashboard.urls import _consolidated_url, _hotspot_url
from onlybirds.dashboard.utils import _clean_str, _days_ago


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
    rows_payload = []
    for _, r in df.iterrows():
        # Each url already encodes the routing (?hotspot= or ?consolidated=);
        # derive the compare-target id from it.
        target_id = r["url"].split("=", 1)[1]
        rows_payload.append(
            {
                "id": target_id,
                "name": str(r["name"])[:48],
                "tot": int(r["target_count"]),
                "rare": int(r["rare_count"]),
                "cons": bool(r["is_consolidated"]),
                "url": r["url"],
            }
        )
    render_top_hotspots_strip(rows_payload)


def _build_places_by_code(
    hotspot_targets: pd.DataFrame,
    hotspots: pd.DataFrame,
    consolidated_targets: pd.DataFrame,
    consolidated: pd.DataFrame,
) -> dict[str, list[dict]]:
    """Build `{species_code: [{type, id, name}, …]}` for the in-view places.

    The chat widget stores this on each assistant bubble so its top-K matches
    render with clickable links into the visible hotspots / consolidated
    areas. Cap each species at 4 places to keep bubbles compact.
    """
    hotspot_names = dict(zip(hotspots["hotspot_id"], hotspots["name"], strict=False))
    cons_names = dict(
        zip(consolidated["consolidated_id"], consolidated["name"], strict=False)
    )
    by_code: dict[str, list[dict]] = {}
    if not hotspot_targets.empty:
        for _, row in hotspot_targets[["species_code", "hotspot_id"]].iterrows():
            code = row["species_code"]
            hid = row["hotspot_id"]
            bucket = by_code.setdefault(code, [])
            if len(bucket) >= 4:
                continue
            bucket.append(
                {"type": "hotspot", "id": hid, "name": hotspot_names.get(hid) or hid}
            )
    if not consolidated_targets.empty:
        for _, row in consolidated_targets[["species_code", "consolidated_id"]].iterrows():
            code = row["species_code"]
            cid = row["consolidated_id"]
            bucket = by_code.setdefault(code, [])
            if len(bucket) >= 4:
                continue
            bucket.append(
                {"type": "area", "id": cid, "name": cons_names.get(cid) or cid}
            )
    return by_code


def render_map(data: DashboardData, db_path: str) -> None:
    hotspots = data.hotspots
    hotspot_targets = data.hotspot_targets
    consolidated = data.consolidated_hotspots
    consolidated_members = data.consolidated_members
    consolidated_targets = data.consolidated_targets
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

    # "Describe the bird" chat — only re-ranks when a region is selected
    # (otherwise the corpus is the entire DB and the answer isn't view-
    # specific). The matches snapshot stored on each assistant bubble carries
    # the in-view hotspot + consolidated-area links, so the user sees
    # clickable "where to go" results inside the chat itself rather than in a
    # separate main-column panel.
    if active_regions:
        kept_hids = set(singletons["hotspot_id"])
        ht_filtered = hotspot_targets[hotspot_targets["hotspot_id"].isin(kept_hids)]
        species_in_view = pd.concat(
            [
                ht_filtered[["species_code", "common_name", "is_rare"]],
                consolidated_targets[["species_code", "common_name", "is_rare"]],
            ],
            ignore_index=True,
        ).drop_duplicates(subset="species_code")
        species_df = species_in_view.merge(
            data.targets[["species_code", "summary", "image_url", "wiki_url"]],
            on="species_code",
            how="left",
        )
        places_by_code = _build_places_by_code(
            ht_filtered, singletons, consolidated_targets, consolidated
        )
        apply_semantic_search(
            species_df,
            db_path=db_path,
            places_by_code=places_by_code,
        )

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
            _popup_html(h["hotspot_id"], name, rows),
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
                _consolidated_popup_html(cid, name, members, ctargets),
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
    bboxes = _region_bboxes(data.hotspots)
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
        st.iframe(preview_html, height=1)


def render_consolidated_detail(
    consolidated_id: str, data: DashboardData, db_path: str
) -> None:
    """Detail view for a consolidated hotspot: deduped species across members."""
    consolidated = data.consolidated_hotspots
    match = consolidated[consolidated["consolidated_id"] == consolidated_id]
    if match.empty:
        st.error(f"Consolidated hotspot `{consolidated_id}` not found.")
        st.markdown("[← Back to map](./)")
        return
    c = match.iloc[0]
    _render_compare_tray(data)

    members = data.consolidated_members
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

    show_all = st.toggle(
        "Show all birds across these hotspots (including ones I've seen)",
        value=False,
        key=f"show_all_{consolidated_id}",
    )
    # When the sidebar chat has a description, widen the pool to every species
    # observed here so the bird-ID flow can rank against life-list birds too —
    # "describe what I saw" naturally includes already-seen species.
    use_all = show_all or bool(current_description())

    if use_all:
        filtered = load_consolidated_all_species(db_path, consolidated_id).copy()
        if filtered.empty:
            st.info("No species recorded across these hotspots yet.")
            return
        obs_by_code = {
            r["species_code"]: (r.get("last_seen"), r.get("how_many"))
            for _, r in filtered.iterrows()
        }
        filtered["_last_seen"] = filtered["last_seen"].fillna("")
        total_label = (
            f"{len(filtered)} unique species across {member_count} hotspots"
        )
    else:
        # Deduped target species across all members. Rebuild from the full targets
        # table so we get every metadata column (sci_name, family, summary…).
        here = data.consolidated_targets
        here = here[here["consolidated_id"] == consolidated_id]
        if here.empty:
            st.info("No target species recorded across these hotspots yet.")
            return

        obs_by_code = {
            r["species_code"]: (r.get("last_seen"), r.get("how_many"))
            for _, r in here.iterrows()
        }
        targets = data.targets
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
        total_label = (
            f"{len(filtered)} unique target species across {member_count} hotspots"
        )

    sorted_filtered = _filter_target_rows(
        filtered,
        data.seasonality,
        key_prefix=f"consolidated_{consolidated_id}_{'all' if use_all else 'targets'}",
        has_last_seen=True,
    )
    sorted_filtered = render_semantic_search(
        sorted_filtered,
        key_prefix=f"consolidated_{consolidated_id}_{'all' if use_all else 'targets'}",
        db_path=db_path,
    )
    st.caption(f"showing {len(sorted_filtered)} of {total_label}")
    if sorted_filtered.empty:
        st.info("No targets match these filters.")
        return
    by_species = _hotspots_by_species(data)
    current_month = dt.date.today().month
    for _, row in sorted_filtered.iterrows():
        last_seen, how_many = obs_by_code.get(row["species_code"], (None, None))
        _render_target_card(
            row,
            data.seasonality,
            current_month,
            last_seen=last_seen,
            how_many=how_many,
            hotspots_for_species=by_species.get(row["species_code"]),
        )


def render_hotspot_detail(
    hotspot_id: str, data: DashboardData, db_path: str
) -> None:
    """Detail view for a single hotspot: header + filtered target cards."""
    hotspots = data.hotspots
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

    # Toggle: by default show only target species (unseen). When on, show every
    # species observed at this hotspot — seen birds get card metadata via the
    # taxonomy + species_info join in load_hotspot_all_species.
    show_all = st.toggle(
        "Show all birds at this hotspot (including ones I've seen)",
        value=False,
        key=f"show_all_{hotspot_id}",
    )
    # When the sidebar chat has a description, widen the pool to every species
    # observed here so the bird-ID flow can rank against life-list birds too —
    # "describe what I saw" naturally includes already-seen species.
    use_all = show_all or bool(current_description())

    if use_all:
        filtered = load_hotspot_all_species(db_path, hotspot_id).copy()
        if filtered.empty:
            st.info("No species recorded at this hotspot yet.")
            return
        obs_by_code = {
            r["species_code"]: (r.get("last_seen"), r.get("how_many"))
            for _, r in filtered.iterrows()
        }
        filtered["_last_seen"] = filtered["last_seen"].fillna("")
        total_label = f"{len(filtered)} species observed at this hotspot"
    else:
        here = data.hotspot_targets
        here = here[here["hotspot_id"] == hotspot_id]
        if here.empty:
            st.info("No target species recorded at this hotspot yet.")
            return

        obs_by_code = {
            r["species_code"]: (r.get("last_seen"), r.get("how_many"))
            for _, r in here.iterrows()
        }

        targets = data.targets
        species_codes = set(here["species_code"])
        filtered = targets[targets["species_code"].isin(species_codes)].copy()
        if filtered.empty:
            st.caption(f"{len(here)} species observed here (full target metadata unavailable).")
            for _, t in here.iterrows():
                flag = " 🚨" if t["is_rare"] else ""
                when = _days_ago(t.get("last_seen"))
                when_str = f" — *{when}*" if when else ""
                wiki_url = _clean_str(t.get("wiki_url"))
                link = f"[{t['common_name']}]({wiki_url})" if wiki_url else t["common_name"]
                st.markdown(f"- {link}{flag}{when_str}")
            return

        filtered["_last_seen"] = filtered["species_code"].map(
            lambda c: obs_by_code.get(c, (None, None))[0] or ""
        )
        total_label = f"{len(filtered)} target species at this hotspot"

    sorted_filtered = _filter_target_rows(
        filtered,
        data.seasonality,
        key_prefix=f"hotspot_{hotspot_id}_{'all' if use_all else 'targets'}",
        has_last_seen=True,
    )
    sorted_filtered = render_semantic_search(
        sorted_filtered,
        key_prefix=f"hotspot_{hotspot_id}_{'all' if use_all else 'targets'}",
        db_path=db_path,
    )
    st.caption(f"showing {len(sorted_filtered)} of {total_label}")
    if sorted_filtered.empty:
        st.info("No targets match these filters.")
        return
    by_species = _hotspots_by_species(data)
    current_month = dt.date.today().month
    for _, row in sorted_filtered.iterrows():
        last_seen, how_many = obs_by_code.get(row["species_code"], (None, None))
        _render_target_card(
            row,
            data.seasonality,
            current_month,
            last_seen=last_seen,
            how_many=how_many,
            hotspots_for_species=by_species.get(row["species_code"]),
            current_hotspot_id=hotspot_id,
        )
