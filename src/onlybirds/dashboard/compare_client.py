"""Client-side compare-list state via localStorage.

The compare list normally lives in `?compare=` URL params and adding a hotspot
triggers a full browser navigation — which forces streamlit to rerun and the
folium map iframe to remount, producing a visible flash. To eliminate the
flash on add/remove, we move the compare list to `localStorage` and render
the tray + add-pills as `components.html` iframes that mutate localStorage
directly via JS. Streamlit doesn't see the change; the map iframe stays put.

The compare *view* (`?view=compare`) still reads from URL params — that's
the only navigation that's still URL-driven. On entry to the compare view
we sync URL → localStorage so deep links populate the tray correctly.

Cross-iframe sync uses the `storage` event, which fires on all same-origin
windows except the writer. Each component listens for it and re-renders.
"""


import json

import pandas as pd
import streamlit.components.v1 as components

MAX_COMPARE = 6
_STORAGE_KEY = "onlybirds.compare"


def _all_metas_json(data: dict[str, pd.DataFrame]) -> str:
    """All hotspot/consolidated IDs → display metadata, as JSON for JS."""
    out: dict[str, dict] = {}
    h = data.get("hotspots")
    if h is not None and not h.empty:
        for _, r in h.iterrows():
            out[r["hotspot_id"]] = {
                "name": (r.get("name") or r["hotspot_id"])[:36],
                "kind": "hotspot",
            }
    c = data.get("consolidated_hotspots")
    if c is not None and not c.empty:
        for _, r in c.iterrows():
            out[r["consolidated_id"]] = {
                "name": (r.get("name") or r["consolidated_id"])[:36],
                "kind": "consolidated",
            }
    return json.dumps(out)


_LIB_JS = r"""
const STORAGE_KEY = 'onlybirds.compare';
const MAX = 6;
function _store() {
  try { return window.top.localStorage; } catch (e) { return window.localStorage; }
}
function readIds() {
  const raw = (_store().getItem(STORAGE_KEY) || '');
  return raw.split(',').map(s => s.trim()).filter(Boolean);
}
function writeIds(ids) {
  const dedup = []; const seen = new Set();
  for (const id of ids) {
    if (!seen.has(id)) { seen.add(id); dedup.push(id); }
    if (dedup.length >= MAX) break;
  }
  _store().setItem(STORAGE_KEY, dedup.join(','));
  // 'storage' event only fires on OTHER windows; nudge same-window listeners
  // via a CustomEvent on window.top so siblings can react too.
  try {
    window.top.dispatchEvent(new CustomEvent('onlybirds:compare', {detail: dedup}));
  } catch (e) {}
  return dedup;
}
function toggleId(id) {
  const ids = readIds();
  const i = ids.indexOf(id);
  if (i >= 0) ids.splice(i, 1);
  else if (ids.length < MAX) ids.push(id);
  return writeIds(ids);
}
function clearAll() { return writeIds([]); }

// Subscribe to changes from any frame (storage event for cross-frame writes,
// custom event for same-window writes from sibling components).
function onChange(handler) {
  window.addEventListener('storage', (e) => {
    if (e.key === STORAGE_KEY) handler(readIds());
  });
  try {
    window.top.addEventListener('onlybirds:compare', (e) => handler(e.detail));
  } catch (e) {}
}
"""


_TRAY_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"><style>
  body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: transparent; }}
  .tray {{ background:#fff; padding:6px 10px; border:1px solid #e6ecf5; border-radius:10px; min-height:30px; display:flex; flex-wrap:wrap; align-items:center; gap:4px; }}
  .label {{ color:#666; font-size:11px; font-weight:700; letter-spacing:.06em; margin-right:8px; text-transform:uppercase; }}
  .chip {{ background:#1f4f99; color:white; padding:3px 6px 3px 10px; border-radius:14px; font-size:12px; font-weight:600; display:inline-flex; align-items:center; gap:6px; }}
  .chip .x {{ color:white; opacity:.85; font-weight:700; padding:0 4px; cursor:pointer; user-select:none; background:none; border:0; }}
  .chip .x:hover {{ opacity:1; }}
  .btn-cmp {{ background:#27ae60; color:white; padding:4px 12px; border-radius:14px; font-size:12px; font-weight:700; text-decoration:none; cursor:pointer; border:0; }}
  .btn-cmp[disabled] {{ background:#cfd6e1; cursor:not-allowed; }}
  .clear {{ color:#999; font-size:11px; text-decoration:none; cursor:pointer; background:none; border:0; padding:0 4px; }}
  .hint {{ color:#999; font-size:11px; }}
  .cap {{ color:#999; font-size:11px; }}
  :host {{ display:block; }}
</style></head>
<body>
<div id="tray" class="tray" style="display:none;">
  <span class="label">Compare</span>
  <span id="chips"></span>
  <span id="cap" class="cap"></span>
  <span id="cta"></span>
  <button id="clear" class="clear">clear</button>
</div>
<script>
{lib_js}
const META = {metas_json};
const ON_COMPARE_VIEW = {on_compare_view};
const root = document.getElementById('tray');
const chipsEl = document.getElementById('chips');
const ctaEl = document.getElementById('cta');
const capEl = document.getElementById('cap');

function render() {{
  const ids = readIds();
  if (!ids.length) {{ root.style.display = 'none'; return; }}
  root.style.display = 'flex';
  chipsEl.innerHTML = '';
  for (const id of ids) {{
    const m = META[id] || {{name: id, kind: 'hotspot'}};
    const chip = document.createElement('span');
    chip.className = 'chip';
    const marker = m.kind === 'consolidated' ? '⊕ ' : '';
    chip.appendChild(document.createTextNode(marker + m.name));
    const rm = document.createElement('button');
    rm.className = 'x'; rm.textContent = '×'; rm.title = 'remove';
    rm.onclick = () => {{ toggleId(id); render(); }};
    chip.appendChild(rm);
    chipsEl.appendChild(chip);
  }}
  capEl.textContent = ids.length >= MAX ? ' (max ' + MAX + ')' : '';
  ctaEl.innerHTML = '';
  if (ON_COMPARE_VIEW) {{
    // No CTA when already on the compare view.
  }} else if (ids.length >= 2) {{
    const btn = document.createElement('button');
    btn.className = 'btn-cmp';
    btn.textContent = 'Compare ' + ids.length + ' →';
    btn.onclick = () => {{
      // The component iframe's sandbox blocks both `_top` and `_self`
      // navigation of the top frame (no `allow-top-navigation`). Opening
      // in a new tab is allowed via `allow-popups` and avoids any flash
      // on the current tab.
      const u = (window.top.location.pathname || '/') + '?view=compare&compare=' + encodeURIComponent(ids.join(','));
      window.open(u, '_blank');
    }};
    ctaEl.appendChild(btn);
  }} else {{
    const hint = document.createElement('span');
    hint.className = 'hint';
    hint.textContent = 'add at least one more to compare';
    ctaEl.appendChild(hint);
  }}
}}

document.getElementById('clear').onclick = () => {{ clearAll(); render(); }};
onChange(() => render());
render();

// Auto-resize the iframe to the tray's content height.
function syncHeight() {{
  const h = document.documentElement.scrollHeight;
  window.parent.postMessage({{type:'streamlit:setFrameHeight', height: h}}, '*');
}}
new ResizeObserver(syncHeight).observe(document.body);
syncHeight();
</script></body></html>
"""


_PILL_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"><style>
  body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: transparent; }}
  button {{ display:inline-block; color:white; padding:6px 14px; border-radius:18px; font-size:13px; font-weight:700; border:0; cursor:pointer; box-shadow:0 1px 3px rgba(0,0,0,.12); }}
  button.full {{ background:#f0f2f6; color:#999; cursor:not-allowed; box-shadow:none; }}
  button.in {{ background:#27ae60; }}
  button.add {{ background:#1f4f99; }}
</style></head>
<body>
<button id="pill" class="add">+ Add to compare</button>
<script>
{lib_js}
const ITEM = {item_json};
const btn = document.getElementById('pill');
function render() {{
  const ids = readIds();
  const inList = ids.includes(ITEM.id);
  if (inList) {{
    btn.className = 'in';
    btn.textContent = '✓ In compare — remove';
    btn.disabled = false;
  }} else if (ids.length >= MAX) {{
    btn.className = 'full';
    btn.textContent = 'Compare full (' + MAX + ') — remove one first';
    btn.disabled = true;
  }} else {{
    btn.className = 'add';
    btn.textContent = '+ Add to compare';
    btn.disabled = false;
  }}
}}
btn.onclick = () => {{ toggleId(ITEM.id); render(); }};
onChange(() => render());
render();
function syncHeight() {{
  const h = document.documentElement.scrollHeight;
  window.parent.postMessage({{type:'streamlit:setFrameHeight', height: h}}, '*');
}}
new ResizeObserver(syncHeight).observe(document.body);
syncHeight();
</script></body></html>
"""


def render_tray(data: dict[str, pd.DataFrame], *, on_compare_view: bool = False) -> None:
    """Render the chip tray as a localStorage-driven iframe."""
    html = _TRAY_HTML.format(
        lib_js=_LIB_JS,
        metas_json=_all_metas_json(data),
        on_compare_view=str(on_compare_view).lower(),
    )
    components.html(html, height=60)


def render_pill(item_id: str, kind: str = "hotspot") -> None:
    """Render the +/✓ toggle pill for a detail page."""
    html = _PILL_HTML.format(
        lib_js=_LIB_JS,
        item_json=json.dumps({"id": item_id, "kind": kind}),
    )
    components.html(html, height=44)


_TOP_STRIP_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"><style>
  body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: transparent; }}
  .strip {{ display:flex; flex-wrap:wrap; align-items:center; gap:6px; }}
  .label {{ color:#666; font-size:11px; font-weight:700; letter-spacing:.06em; margin-right:4px; text-transform:uppercase; }}
  .chip {{ color:white; padding:4px 6px 4px 10px; border-radius:14px; font-size:12px; font-weight:600; display:inline-flex; align-items:center; }}
  .chip a.body {{ color:white; text-decoration:none; cursor:pointer; }}
  .chip .toggle {{ background:rgba(255,255,255,.22); color:white; padding:0 6px; margin-left:6px; border-radius:10px; font-size:11px; font-weight:800; line-height:1.4; border:0; cursor:pointer; }}
  .chip .toggle[disabled] {{ opacity:.4; cursor:not-allowed; }}
</style></head>
<body>
<div class="strip"><span class="label">Top hotspots</span><span id="chips"></span></div>
<script>
{lib_js}
const ROWS = {rows_json};
const chipsEl = document.getElementById('chips');
function tierBg(rare, tot) {{
  if (rare > 0) return '#e74c3c';
  if (tot >= 5) return '#1f4f99';
  return '#3498db';
}}
function render() {{
  const ids = readIds();
  chipsEl.innerHTML = '';
  for (const row of ROWS) {{
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.style.background = tierBg(row.rare, row.tot);
    const body = document.createElement('a');
    body.className = 'body';
    body.href = row.url;
    body.target = '_self';
    body.textContent = (row.rare ? ('🚨' + row.rare + ' · ') : '') + row.tot + '× ' + (row.cons ? '⊕ ' : '') + row.name;
    chip.appendChild(body);
    const btn = document.createElement('button');
    btn.className = 'toggle';
    const inList = ids.includes(row.id);
    if (inList) {{
      btn.textContent = '✓';
      btn.title = 'remove from compare';
    }} else if (ids.length >= MAX) {{
      btn.textContent = '+';
      btn.disabled = true;
      btn.title = 'compare full';
    }} else {{
      btn.textContent = '+';
      btn.title = 'add to compare';
    }}
    btn.onclick = (e) => {{ e.preventDefault(); e.stopPropagation(); toggleId(row.id); render(); }};
    chip.appendChild(btn);
    chipsEl.appendChild(chip);
  }}
}}
onChange(() => render());
render();
function syncHeight() {{
  const h = document.documentElement.scrollHeight;
  window.parent.postMessage({{type:'streamlit:setFrameHeight', height: h}}, '*');
}}
new ResizeObserver(syncHeight).observe(document.body);
syncHeight();
</script></body></html>
"""


def render_top_hotspots_strip(rows: list[dict]) -> None:
    """Render the top-hotspots chip strip as a localStorage-aware iframe.

    `rows` is a list of dicts with keys: id, name, tot, rare, cons (bool), url.
    Clicking the chip body navigates to `url`; the `+`/`✓` button toggles
    compare in localStorage with no streamlit rerun.
    """
    html = _TOP_STRIP_HTML.format(
        lib_js=_LIB_JS,
        rows_json=json.dumps(rows),
    )
    components.html(html, height=80)


def popup_pill_html(item_id: str, *, size: str = "md") -> str:
    """HTML+JS pill for use inside leaflet popups.

    The button's `onclick` is fully self-contained: it reads/writes
    `window.top.localStorage` and updates its own label in place — no
    streamlit rerun, no navigation. A separate `<svg onload>` shim runs
    once on insert to set the *initial* label based on whether the item
    is already in the compare list; if the shim fails for any reason
    the button still toggles correctly on first click.
    """
    pad = "2px 8px" if size == "sm" else "3px 10px"
    fs = "11px" if size == "sm" else "12px"
    btn_id = f"olbcmp_{abs(hash(item_id)) % (10**9)}"
    item_js = json.dumps(item_id)
    # Self-contained click handler — works without the init shim.
    click_js = (
        "event.preventDefault();event.stopPropagation();"
        "var s=function(){try{return window.top.localStorage;}catch(e){return window.localStorage;}};"
        "var raw=(s().getItem('onlybirds.compare')||'');"
        "var ids=raw.split(',').map(function(x){return x.trim();}).filter(Boolean);"
        f"var id={item_js};var i=ids.indexOf(id);"
        "if(i>=0){ids.splice(i,1);}else if(ids.length<6){ids.push(id);}"
        "s().setItem('onlybirds.compare',ids.join(','));"
        "try{window.top.dispatchEvent(new CustomEvent('onlybirds:compare',{detail:ids}));}catch(e){}"
        "var inL=ids.indexOf(id)>=0;"
        "if(inL){this.style.background='#27ae60';this.style.color='#fff';this.textContent='✓ in compare';}"
        "else{this.style.background='#1f4f99';this.style.color='#fff';this.textContent='+ compare';}"
    )
    # Init shim — sets the correct initial label on insert.
    init_js = (
        "(function(){"
        f"var b=document.getElementById('{btn_id}');if(!b)return;"
        "var s=function(){try{return window.top.localStorage;}catch(e){return window.localStorage;}};"
        "var raw=(s().getItem('onlybirds.compare')||'');"
        "var ids=raw.split(',').map(function(x){return x.trim();}).filter(Boolean);"
        f"var inL=ids.indexOf({item_js})>=0;"
        "if(inL){b.style.background='#27ae60';b.style.color='#fff';b.textContent='✓ in compare';}"
        "else if(ids.length>=6){b.style.background='#f0f2f6';b.style.color='#999';b.textContent='compare full';b.disabled=true;}"
        "})();"
    )
    click_attr = click_js.replace('"', "&quot;")
    init_attr = init_js.replace('"', "&quot;")
    return (
        f"<button id='{btn_id}' onclick=\"{click_attr}\" "
        f"style='display:inline-block;background:#1f4f99;color:white;"
        f"padding:{pad};border-radius:12px;font-size:{fs};font-weight:700;"
        f"border:0;cursor:pointer;white-space:nowrap;'>+ compare</button>"
        f"<svg xmlns='http://www.w3.org/2000/svg' width='0' height='0' "
        f"style='display:none' onload=\"this.remove();{init_attr}\"></svg>"
    )
