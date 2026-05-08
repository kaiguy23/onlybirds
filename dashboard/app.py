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
    return {
        "targets": targets,
        "hotspots": hotspots,
        "hotspot_targets": hotspot_targets,
        "seasonality": seasonality,
    }


def _hotspot_url(hotspot_id: str) -> str:
    """URL that triggers the hotspot detail view via Streamlit query params."""
    return f"?hotspot={hotspot_id}"


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


def _popup_html(hotspot_id: str, name: str, hotspot_targets: pd.DataFrame) -> str:
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
        f'border-bottom:1px solid #e6ecf5;padding-bottom:6px;margin-bottom:4px;">'
        f'{name} <span style="font-size:11px;font-weight:600;">↗</span></a>'
    )
    return (
        f"<div style='min-width:220px;max-width:300px;'>{title}{body}</div>"
    )


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


def _top_hotspots_panel(hotspots: pd.DataFrame) -> None:
    """Render a compact ranked list of best hotspots above the map."""
    if hotspots.empty:
        return
    scored = hotspots.assign(
        _score=lambda d: d["rare_target_count"] * 10 + d["target_count"]
    ).sort_values("_score", ascending=False)
    top = scored[scored["_score"] > 0].head(5)
    if top.empty:
        return
    chips = []
    for _, h in top.iterrows():
        rare = int(h["rare_target_count"])
        tot = int(h["target_count"])
        bg = "#e74c3c" if rare else ("#1f4f99" if tot >= 5 else "#3498db")
        prefix = f"🚨{rare} · " if rare else ""
        name = (h["name"] or h["hotspot_id"])[:48]
        chips.append(
            f'<a href="{_hotspot_url(h["hotspot_id"])}" target="_self" '
            f'style="background:{bg};color:white;padding:4px 10px;border-radius:14px;'
            f'font-size:12px;font-weight:600;margin-right:6px;display:inline-block;'
            f'margin-bottom:4px;text-decoration:none;cursor:pointer;">'
            f'{prefix}{tot}× {name}</a>'
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
    if hotspots.empty:
        st.info("No hotspots loaded yet — run `onlybirds run` first.")
        return

    _top_hotspots_panel(hotspots)

    center = [hotspots["lat"].mean(), hotspots["lon"].mean()]
    m = folium.Map(location=center, zoom_start=10, tiles="cartodbpositron")
    cluster = MarkerCluster(icon_create_function=_CLUSTER_ICON_FN).add_to(m)
    # Group once instead of filtering hotspot_targets per hotspot — avoids N² scan.
    targets_by_hotspot = dict(tuple(hotspot_targets.groupby("hotspot_id", sort=False)))
    empty_targets = hotspot_targets.iloc[0:0]
    for _, h in hotspots.iterrows():
        rows = targets_by_hotspot.get(h["hotspot_id"], empty_targets)
        target_count = int(h["target_count"] or 0)
        rare_count = int(h["rare_target_count"] or 0)
        has_rare = rare_count > 0
        html, diameter = _marker_div_html(target_count, rare_count)
        rare_str = f" · {rare_count} rare" if rare_count else ""
        name = h["name"] or h["hotspot_id"]
        tooltip = folium.Tooltip(_tooltip_html(name, target_count, rare_count))
        popup = folium.Popup(
            _popup_html(h["hotspot_id"], name, rows), max_width=320
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

    # Legend overlay sits inside the map container so it floats over the tiles.
    # `m.get_root()` returns folium's `Figure`, which has `.html` and `.header`
    # attributes at runtime. ty can't see them through folium's untyped surface,
    # hence the `ty: ignore`s below.
    root = m.get_root()
    root.html.add_child(folium.Element(_LEGEND_HTML))  # ty: ignore[unresolved-attribute]
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
) -> pd.DataFrame:
    """Render the filter/sort bar and return the filtered, sorted DataFrame.

    Expected columns on `df`: species_code, common_name, sci_name, is_rare,
    plus optionally `_last_seen` (ISO string) when has_last_seen=True.
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

    cols = st.columns([3, 2, 2, 2, 2] if has_last_seen else [3, 2, 2, 2])
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
    with cols[1]:
        rarity = st.selectbox(
            "Rarity",
            ["All birds", "Rare only", "Common only"],
            key=f"{key_prefix}_rarity",
            label_visibility="collapsed",
        )
    with cols[2]:
        month = st.selectbox(
            "Month",
            months_labels,
            index=0,  # default: Any month
            key=f"{key_prefix}_month",
            label_visibility="collapsed",
        )
    if has_last_seen:
        with cols[3]:
            window = st.selectbox(
                "Last seen",
                list(_LAST_SEEN_WINDOWS.keys()),
                key=f"{key_prefix}_window",
                label_visibility="collapsed",
            )
        with cols[4]:
            sort = st.selectbox(
                "Sort",
                sort_choices,
                key=f"{key_prefix}_sort",
                label_visibility="collapsed",
            )
    else:
        window = "Any time"
        with cols[3]:
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
    if targets.empty:
        st.warning("No target birds yet. Run the pipeline.")
        return

    # Enrich each target with the most recent observation date across all
    # hotspots, so the filter bar can sort/filter by "last seen" globally.
    ht = data["hotspot_targets"]
    enriched = targets.copy()
    if not ht.empty and "last_seen" in ht.columns:
        last_by_code = ht.groupby("species_code")["last_seen"].max()
        enriched["_last_seen"] = enriched["species_code"].map(last_by_code).fillna("")
    else:
        enriched["_last_seen"] = ""

    filtered = _filter_target_rows(
        enriched, seasonality, key_prefix="targets", has_last_seen=True
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


def render_hotspot_detail(hotspot_id: str, data: dict[str, pd.DataFrame]) -> None:
    """Detail view for a single hotspot: header + filtered target cards."""
    hotspots = data["hotspots"]
    match = hotspots[hotspots["hotspot_id"] == hotspot_id]
    if match.empty:
        st.error(f"Hotspot `{hotspot_id}` not found.")
        st.markdown("[← Back to map](./)")
        return
    h = match.iloc[0]

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
    st.divider()

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

    # Routing: ?hotspot=<id> shows the per-hotspot detail view.
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
