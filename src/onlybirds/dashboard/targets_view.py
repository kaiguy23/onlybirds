"""Target species rendering: target cards, the filter/sort bar, target list page."""

import calendar
import datetime as dt
import json
from collections import Counter
from enum import Enum

import pandas as pd
import streamlit as st


class SortMode(str, Enum):
    RARE_THEN_RECENT = "Rare first, then recent"
    RARE_THEN_NAME = "Rare first, then name"
    NAME_A_TO_Z = "Name (A→Z)"
    MOST_HOTSPOTS = "Most hotspots"
    LAST_SEEN = "Last seen (newest)"


class RarityFilter(str, Enum):
    ALL = "All birds"
    RARE_ONLY = "Rare only"
    COMMON_ONLY = "Common only"

from onlybirds.dashboard.regions import (
    _REGION_NONE_SENTINEL,
    _parse_active_regions,
    _region_chips_panel,
    _region_label,
    _region_mask,
)
from onlybirds.dashboard.urls import _hotspot_url
from onlybirds.dashboard.utils import (
    _clean_str,
    _days_ago,
    _ebird_species_url,
    _is_nan,
    _parse_iso_date,
)


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
        count_str = ""
        hm = h.get("how_many")
        if hm is not None and not _is_nan(hm):
            try:
                n_birds = int(hm)
                if n_birds > 0:
                    count_str = f" · count {n_birds}"
            except (TypeError, ValueError):
                pass
        when_html = (
            f"<span style='color:#888;font-size:12px;'> · last seen {when}{count_str}</span>"
            if when
            else (
                f"<span style='color:#888;font-size:12px;'>{count_str}</span>"
                if count_str
                else ""
            )
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
                        count_str = f" · count {n}"
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
    sort_choices = [SortMode.RARE_THEN_RECENT if has_last_seen else SortMode.RARE_THEN_NAME]
    sort_choices += [SortMode.NAME_A_TO_Z]
    if has_last_seen:
        sort_choices += [SortMode.LAST_SEEN]
    if "hotspot_count" in df.columns:
        sort_choices += [SortMode.MOST_HOTSPOTS]

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
            list(RarityFilter),
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
    if rarity == RarityFilter.RARE_ONLY:
        out = out[out["is_rare"] == 1]
    elif rarity == RarityFilter.COMMON_ONLY:
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
    if sort == SortMode.NAME_A_TO_Z:
        out = out.sort_values("common_name", kind="mergesort")
    elif sort == SortMode.MOST_HOTSPOTS and "hotspot_count" in out.columns:
        out = out.sort_values(
            ["is_rare", "hotspot_count"], ascending=[False, False], kind="mergesort"
        )
    elif sort == SortMode.LAST_SEEN and has_last_seen:
        out = out.sort_values(
            ["is_rare", "_last_seen"], ascending=[False, False], kind="mergesort"
        )
    elif sort == SortMode.RARE_THEN_RECENT and has_last_seen:
        out = out.sort_values(
            ["is_rare", "_last_seen"], ascending=[False, False], kind="mergesort"
        )
    else:
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
    region_counts: Counter[str] = Counter()
    if "region" in hotspots.columns:
        region_counts.update(
            r if isinstance(r, str) and r else _REGION_NONE_SENTINEL
            for r in hotspots["region"]
        )
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
    # Honor the active "Last seen" window when building the per-species hotspot
    # list — otherwise the expander leaks hotspots whose obs predate the cutoff.
    # The widget key matches `_filter_target_rows`'s key_prefix ("targets").
    window_label = st.session_state.get("targets_window", "Any time")
    window_days = _LAST_SEEN_WINDOWS.get(window_label)
    ht_for_lookup = ht
    if window_days is not None and not ht_for_lookup.empty:
        cutoff = dt.date.today() - dt.timedelta(days=window_days)

        def _in_window(v: object) -> bool:
            d = _parse_iso_date(v)
            return d is not None and d >= cutoff

        ht_for_lookup = ht_for_lookup[ht_for_lookup["last_seen"].apply(_in_window)]
    by_species = _hotspots_by_species(
        {**data, "hotspot_targets": ht_for_lookup}
    )
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
