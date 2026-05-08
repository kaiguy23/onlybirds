"""Folium marker icons, tooltip/popup HTML, and map-overlay assets."""

from dataclasses import dataclass

import pandas as pd

from onlybirds.dashboard.compare import _popup_compare_pill
from onlybirds.dashboard.urls import _consolidated_url, _hotspot_url
from onlybirds.dashboard.utils import _clean_str, _days_ago, _ebird_species_url


# ---------- Marker color tiers ----------

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


# ---------- Tooltips ----------

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


# ---------- Click popups ----------

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


# ---------- Map overlays (cluster icon JS, legend) ----------

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
