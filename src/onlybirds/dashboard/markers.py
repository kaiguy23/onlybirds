"""Folium marker icons, tooltip/popup HTML, and map-overlay assets."""

import json
from dataclasses import dataclass
from html import escape

import pandas as pd
from branca.element import MacroElement
from jinja2 import Template

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
#
# A region can hold ~2,000 markers, so popups are built once as plain HTML
# strings and shipped in one JSON table (`LazyPopups`); Leaflet only turns one
# into DOM when its marker is tapped. Styling lives in `_MAP_CSS` classes and
# behavior in the `olbOpen`/`olbCmp` helpers from `_MAP_JS`, so each popup
# carries only its own data. (Inline styles and handlers per popup made the
# map ~9 MB, which phones took seconds to download and parse.)


def _species_items(target_rows: pd.DataFrame) -> str:
    items = []
    for t in target_rows.to_dict("records"):
        flag = " 🚨" if t["is_rare"] else ""
        # Prefer eBird (richer for birders); fall back to Wikipedia, then plain text.
        href = _ebird_species_url(t.get("species_code")) or _clean_str(t.get("wiki_url"))
        name = escape(str(t["common_name"]))
        link = f'<a href="{escape(href)}" target="_blank">{name}</a>' if href else name
        when = _days_ago(t.get("last_seen"))
        when_html = f' <span class="olb-when">— {when}</span>' if when else ""
        items.append(f"<li>{link}{flag}{when_html}</li>")
    return "".join(items)


def _open_link(query: str, label: str, cls: str = "olb-title") -> str:
    """Link to an app page (`query` like "?hotspot=L1"); `olbOpen` opens it in
    place on phones and in a new tab on desktop."""
    q = escape(query)
    return f'<a class="{cls}" href="{q}" target="_blank" onclick="return olbOpen(event,\'{q}\')">{label}</a>'


def _compare_pill(item_id: str, small: bool = False) -> str:
    """"+ compare" toggle; `olbCmp` flips it and the label syncs when the popup opens."""
    cls = "olb-cmp olb-cmp--sm" if small else "olb-cmp"
    return f'<button class="{cls}" data-id="{escape(item_id)}" onclick="olbCmp(event,this)">+ compare</button>'


def _popup_html(
    hotspot_id: str,
    name: str,
    hotspot_targets: pd.DataFrame,
) -> str:
    """Click popup: hotspot title (links to detail) + full species list."""
    # Dedupe species — `hotspot_obs` can have multiple rows per (hotspot, species)
    # if the bird was logged on multiple visits. The SQL orders by last_seen DESC,
    # so the first row per species is the most recent.
    if not hotspot_targets.empty and "species_code" in hotspot_targets.columns:
        hotspot_targets = hotspot_targets.drop_duplicates(
            subset=["species_code"], keep="first"
        )
    items = _species_items(hotspot_targets)
    # The list scrolls past ~14 species so big hotspots stay inside the map.
    body = (
        f'<div class="onlybirds-popup-list"><ul>{items}</ul></div>'
        if items
        else '<i class="olb-none">no targets here</i>'
    )
    title = _open_link(f"?hotspot={hotspot_id}", f"{escape(name)} <span>↗</span>")
    actions = f'<div class="olb-actions">{_compare_pill(hotspot_id)}</div>'
    return f'<div class="olb-popup">{title}{actions}{body}</div>'


def _consolidated_popup_html(
    consolidated_id: str,
    name: str,
    member_rows: pd.DataFrame,
    target_rows: pd.DataFrame,
) -> str:
    """Click popup for a consolidated hotspot.

    Lists the original hotspots (each linking to its own detail page) and the
    deduped target species across them.
    """
    members_html = ""
    if member_rows is not None and not member_rows.empty:
        parts = [
            f'<li>{_open_link(f"?hotspot={m["hotspot_id"]}", escape(str(m.get("name") or m["hotspot_id"])), "")}'
            f'{_compare_pill(m["hotspot_id"], small=True)}</li>'
            for m in member_rows.to_dict("records")
        ]
        members_html = (
            '<div class="olb-members"><div class="olb-label">Includes</div>'
            f'<ul>{"".join(parts)}</ul></div>'
        )

    items = _species_items(target_rows) if target_rows is not None else ""
    targets_html = (
        '<div class="onlybirds-popup-list olb-cons-list"><div class="olb-label">Unique species</div>'
        f"<ul>{items}</ul></div>"
        if items
        else '<div class="olb-cons-list"><i class="olb-none">no targets here</i></div>'
    )
    title = _open_link(f"?consolidated={consolidated_id}", f"⊕ {escape(name)} <span>↗</span>")
    actions = f'<div class="olb-actions">{_compare_pill(consolidated_id)}</div>'
    return (
        f'<div class="olb-popup olb-popup--cons">'
        f"{title}{actions}{members_html}{targets_html}</div>"
    )


class LazyPopups(MacroElement):
    """Popup HTML for every marker, keyed by id, bound when a marker is tapped.

    Add it to the map *before* the markers: its script registers a Leaflet
    init hook, so markers created afterwards with a `popup_key` option get a
    popup that's built only when it opens.
    """

    _template = Template("""
        {% macro script(this, kwargs) %}
        {{ this.js }}
        window.olbPopups = {{ this.popups_json() }};
        {% endmacro %}
    """)

    def __init__(self) -> None:
        super().__init__()
        self._name = "LazyPopups"
        self.js = _MAP_JS
        self.popups: dict[str, str] = {}

    def add(self, key: str, html: str) -> str:
        self.popups[key] = html
        return key

    def popups_json(self) -> str:
        # "</" would end the <script> block early.
        return json.dumps(self.popups, ensure_ascii=False).replace("</", "<\\/")


# Runs once in the map iframe (st_folium injects the map script as a <script>
# element, so these are globals the popup HTML can call).
_MAP_JS = """
// Open an app page. The iframe sandbox blocks navigating the parent but allows
// same-origin access and popups: phones click an anchor in the parent's own
// document so the app navigates in place (no stack of Safari tabs); desktop
// opens a new tab.
window.olbOpen = function (ev, q) {
  ev.preventDefault();
  try {
    if (window.matchMedia('(pointer: coarse)').matches) {
      var d = window.parent.document, a = d.createElement('a');
      a.href = window.parent.location.pathname + q; a.target = '_self';
      d.body.appendChild(a); a.click(); return false;
    }
  } catch (e) {}
  window.open(window.top.location.pathname + q, '_blank');
  return false;
};

// Compare list: ids in localStorage "onlybirds.compare" (max 6), shared with
// the compare tray, which listens for the "onlybirds:compare" event.
function olbStore() { try { return window.top.localStorage; } catch (e) { return window.localStorage; } }
function olbIds() {
  return (olbStore().getItem('onlybirds.compare') || '').split(',')
    .map(function (x) { return x.trim(); }).filter(Boolean);
}
function olbPaint(b, ids) {
  var inList = ids.indexOf(b.dataset.id) >= 0, full = !inList && ids.length >= 6;
  b.classList.toggle('olb-cmp--in', inList);
  b.classList.toggle('olb-cmp--full', full);
  b.disabled = full;
  b.textContent = inList ? '✓ in compare' : full ? 'compare full' : '+ compare';
}
window.olbCmp = function (ev, b) {
  ev.preventDefault(); ev.stopPropagation();
  var ids = olbIds(), i = ids.indexOf(b.dataset.id);
  if (i >= 0) ids.splice(i, 1); else if (ids.length < 6) ids.push(b.dataset.id);
  olbStore().setItem('onlybirds.compare', ids.join(','));
  try { window.top.dispatchEvent(new CustomEvent('onlybirds:compare', {detail: ids})); } catch (e) {}
  document.querySelectorAll('.olb-cmp').forEach(function (x) { olbPaint(x, ids); });
};

// Markers carry only a popup key; the popup is built the first time it opens.
L.Marker.addInitHook(function () {
  var key = this.options.popupKey;
  if (!key) return;
  var cons = key.indexOf('consolidated:') === 0;
  this.bindPopup(function () { return window.olbPopups[key] || ''; },
                 {maxWidth: cons ? 340 : 320, keepInView: true,
                  // clear the "Where to go" legend in the bottom-left corner
                  autoPanPaddingTopLeft: [12, 12], autoPanPaddingBottomRight: [12, 56]});
  this.on('popupopen', function (e) {
    var ids = olbIds();
    e.popup.getElement().querySelectorAll('.olb-cmp').forEach(function (b) { olbPaint(b, ids); });
  });
});
"""


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
  <input type="checkbox" id="onlybirds-legend-toggle" style="display:none;">
  <label for="onlybirds-legend-toggle" class="onlybirds-legend-title"
         style="display:block;font-weight:700;margin-bottom:4px;letter-spacing:.02em;">Where to go</label>
  <div class="onlybirds-legend-body">
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
</div>
"""

# CSS injected into the folium iframe's <head>. Scripts in the header don't run
# inside st_folium (only the map script does), so the legend toggle is CSS-only.
_MAP_CSS = """
<style>
/* Leaflet's default `.leaflet-tooltip` rule sets `white-space: nowrap`, so long
   hotspot names overflow the map. Let them wrap at word boundaries up to a
   sensible width. Avoid `word-break` / `overflow-wrap: anywhere` here — they
   let the tooltip's min-content collapse to a one-character column. */
.leaflet-tooltip {white-space: normal !important; max-width: 260px !important; width: max-content !important;}

/* Popups (see _popup_html). */
.olb-popup {min-width: 220px; max-width: 300px;}
.olb-popup--cons {min-width: 240px; max-width: 320px;}
.olb-popup a {color: #1f4f99; text-decoration: none; font-weight: 500;}
.olb-popup a.olb-title {display: block; font-size: 15px; font-weight: 700; line-height: 1.3;
  word-break: break-word; white-space: normal; padding-bottom: 4px;}
.olb-title span {font-size: 11px; font-weight: 600;}
.olb-actions {display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
  border-bottom: 1px solid #e6ecf5; padding-bottom: 6px; margin-bottom: 4px;}
.olb-popup ul {margin: 0; padding-left: 18px; font-size: 13px;}
.olb-popup li {margin: 1px 0;}
.onlybirds-popup-list {max-height: 340px; overflow-y: auto; margin-top: 6px; padding-right: 4px;}
.olb-cons-list {max-height: 280px; margin-top: 8px;}
.olb-members {margin-top: 4px;}
.olb-members ul {font-size: 12px;}
.olb-members li {margin: 2px 0; display: flex; gap: 6px; align-items: center; flex-wrap: wrap;}
.olb-label {font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 2px;}
.olb-when {color: #888; font-size: 11px;}
.olb-none {color: #888;}
.olb-cmp {display: inline-block; background: #1f4f99; color: #fff; padding: 3px 10px; border-radius: 12px;
  font-size: 12px; font-weight: 700; border: 0; cursor: pointer; white-space: nowrap;}
.olb-cmp--sm {padding: 2px 8px; font-size: 11px;}
.olb-cmp--in {background: #27ae60;}
.olb-cmp--full {background: #f0f2f6; color: #999; cursor: default;}

/* Touch screens: a tap opens the popup, and the hover tooltip lingers on top
   of it saying the same thing. */
@media (pointer: coarse) {
  .leaflet-tooltip {display: none !important;}
}

/* Phones: the map is ~60% of the screen, so keep popups to a scrollable
   strip that fits, and fold the legend into a "Where to go" pill that opens
   on tap. (vh here is the map iframe's height.) */
@media (max-width: 640px) {
  .leaflet-popup-content {margin: 10px 12px !important; font-size: 13px;}
  .olb-popup {min-width: 0; max-width: 72vw;}
  .onlybirds-popup-list {max-height: 34vh !important;}
  #onlybirds-legend {bottom: 12px !important; left: 10px !important; padding: 4px 10px !important;}
  .onlybirds-legend-title {margin: 0 !important; cursor: pointer;}
  .onlybirds-legend-title::after {content: " ▸"; color: #888;}
  .onlybirds-legend-body {display: none;}
  #onlybirds-legend-toggle:checked ~ .onlybirds-legend-body {display: block;}
  #onlybirds-legend-toggle:checked ~ .onlybirds-legend-title::after {content: " ▾";}
}
</style>
"""
