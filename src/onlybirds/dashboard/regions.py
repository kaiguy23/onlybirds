"""Region filter chips, bbox helpers, and the chip-hover preview JS."""

from collections import Counter

import pandas as pd
import streamlit as st


_REGION_NONE_SENTINEL = "__none__"


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
    counts: Counter[str] = Counter()
    for df in (singletons, consolidated):
        if df.empty or "region" not in df.columns:
            continue
        counts.update(
            r if isinstance(r, str) and r else _REGION_NONE_SENTINEL
            for r in df["region"]
        )
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
