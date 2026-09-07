/* cartofab front-end: Leaflet region picker + live SVG preview. */
'use strict';

const $ = (id) => document.getElementById(id);
const STORE = 'mapcontours.v1';

/* ---------------------------------------------------------------- projection
   A local ENU approximation, accurate to well under a metre over the tens of
   km we deal with. Used only to draw the capture footprint on the map; the
   server does the real pyproj transverse-mercator for the geometry. */
function metresPerDegree(lat) {
  const p = lat * Math.PI / 180;
  return {
    lat: 111132.92 - 559.82 * Math.cos(2 * p) + 1.175 * Math.cos(4 * p),
    lon: 111412.84 * Math.cos(p) - 93.5 * Math.cos(3 * p),
  };
}

function footprint(lat, lon, w, h, rotDeg) {
  const m = metresPerDegree(lat);
  const a = rotDeg * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
  const hw = w / 2, hh = h / 2;
  const corners = [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]];
  const pts = [];
  for (let i = 0; i < 4; i++) {
    const [ax, ay] = corners[i], [bx, by] = corners[(i + 1) % 4];
    for (let k = 0; k < 12; k++) {
      const t = k / 12;
      const x = ax + (bx - ax) * t, y = ay + (by - ay) * t;
      const rx = x * c - y * s, ry = x * s + y * c;
      pts.push([lat + ry / m.lat, lon + rx / m.lon]);
    }
  }
  return pts;
}

/* --------------------------------------------------------------------- state */
const state = {
  lat: 45.8326, lon: 6.8652,
  trip: new Set(),
  lastSpec: null,
  busy: false,
};

/* ----------------------------------------------------------------------- map */
const map = L.map('map', { zoomControl: true }).setView([state.lat, state.lon], 12);
const bases = {
  'OpenTopoMap': L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    { maxZoom: 17, attribution: '© OpenTopoMap, © OpenStreetMap contributors' }),
  'OSM': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    { maxZoom: 19, attribution: '© OpenStreetMap contributors' }),
};
bases['OpenTopoMap'].addTo(map);
L.control.layers(bases, {}, { position: 'topright' }).addTo(map);

const rect = L.polygon([], {
  color: '#6fb3e0', weight: 2, fillColor: '#6fb3e0', fillOpacity: 0.08,
}).addTo(map);
const handle = L.marker([state.lat, state.lon], {
  draggable: true, title: 'drag to move the capture area',
}).addTo(map);

handle.on('drag', (e) => {
  const p = e.target.getLatLng();
  state.lat = p.lat; state.lon = p.lng;
  $('lat').value = p.lat.toFixed(5); $('lon').value = p.lng.toFixed(5);
  drawRect();
});
handle.on('dragend', () => { save(); checkCoverage(); refreshOsmData(false); });
function stopFollowing() {
  if (!$('followView').checked) return;
  $('followView').checked = false;         // a hand-placed region stays put
  status('follow view off — the region is pinned');
  save();
}
handle.on('dragstart', stopFollowing);
map.on('dblclick', (e) => {
  state.lat = e.latlng.lat; state.lon = e.latlng.lng;
  syncCentreInputs(); handle.setLatLng(e.latlng); drawRect(); save(); checkCoverage();
});
map.doubleClickZoom.disable();

function dims() {
  const w = parseFloat($('widthKm').value) * 1000;
  const a = $('aspect').value;
  const h = a === 'custom' ? parseFloat($('heightKm').value) * 1000 : w / parseFloat(a);
  return { w: Math.max(200, w || 8000), h: Math.max(200, h || 8000) };
}

/* Corner + edge handles. Everything is computed in the page frame (the
   capture rectangle un-rotated), which is also the frame the server draws in,
   so a handle drag means the same thing here as it does in the SVG. */
const HANDLES = [
  ['nw', -1, 1], ['ne', 1, 1], ['se', 1, -1], ['sw', -1, -1],
  ['n', 0, 1], ['s', 0, -1], ['e', 1, 0], ['w', -1, 0],
];

function pageToLatLng(px, py) {
  const a = (+$('rot').value) * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
  const lx = px * c - py * s, ly = px * s + py * c;
  const m = metresPerDegree(state.lat);
  return [state.lat + ly / m.lat, state.lon + lx / m.lon];
}

/* Page-frame offset of a lat/lon from an arbitrary origin. */
function toPageAxes(lat, lon, oLat, oLon) {
  const m = metresPerDegree(oLat);
  const ex = (lon - oLon) * m.lon, ey = (lat - oLat) * m.lat;
  const a = -(+$('rot').value) * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
  return [ex * c - ey * s, ex * s + ey * c];
}

function latLngToPage(lat, lon) {
  return toPageAxes(lat, lon, state.lat, state.lon);
}

/* Alt/Option switches to resizing symmetrically about the centre. Tracked
   globally as a fallback, but the drag event's own modifier state wins when
   Leaflet passes it through. */
let altHeld = false;
window.addEventListener('keydown', (e) => { if (e.key === 'Alt') altHeld = true; });
window.addEventListener('keyup', (e) => { if (e.key === 'Alt') altHeld = false; });
window.addEventListener('blur', () => { altHeld = false; });

const handles = HANDLES.map(([id, sx, sy]) => {
  const kind = (sx && sy) ? 'corner' : 'edge';
  const mk = L.marker([0, 0], {
    draggable: true, keyboard: false, zIndexOffset: 900,
    icon: L.divIcon({ className: `rhandle ${kind}`, iconSize: [11, 11] }),
    title: 'drag to resize (hold Option for symmetric)',
  }).addTo(map);
  mk.on('dragstart', () => beginResize(sx, sy));
  mk.on('drag', (e) => {
    const oe = e.originalEvent;
    const sym = oe && typeof oe.altKey === 'boolean' ? oe.altKey : altHeld;
    resizeFrom(sx, sy, e.target.getLatLng(), sym);
  });
  mk.on('dragend', () => {
    dragCtx = null; positionHandles(); save(); checkCoverage();
  });
  return { id, sx, sy, mk };
});

/* Resizing anchors the opposite corner (or edge) by default, so you can set
   one corner and drag the other out to frame the area — which means the centre
   moves. Hold Option to keep the centre fixed and grow symmetrically instead.

   The anchor is captured as a lat/lon at dragstart, because the page frame is
   defined relative to the centre and the centre is exactly what moves. */
let dragCtx = null;

function beginResize(sx, sy) {
  stopFollowing();                 // resizing by hand pins the region
  const { w, h } = dims();
  const hw = w / 2, hh = h / 2;
  const [alat, alon] = pageToLatLng(-sx * hw, -sy * hh);
  dragCtx = { anchorLat: alat, anchorLon: alon, offX: sx * hw, offY: sy * hh };
}

function resizeFrom(sx, sy, ll, symmetric) {
  if (!symmetric && !dragCtx) beginResize(sx, sy);
  const MIN = 100;
  const anchored = !symmetric;
  const oLat = anchored ? dragCtx.anchorLat : state.lat;
  const oLon = anchored ? dragCtx.anchorLon : state.lon;
  const [px, py] = toPageAxes(ll.lat, ll.lng, oLat, oLon);
  // symmetric: the pointer marks a half-dimension; anchored: a full one
  const k = symmetric ? 1 : 0.5;

  const { w, h } = dims();
  let hw = w / 2, hh = h / 2;
  const a = $('aspect').value;
  if (a === 'custom') {
    if (sx) hw = Math.max(MIN, k * Math.abs(px));
    if (sy) hh = Math.max(MIN, k * Math.abs(py));
  } else {
    const ar = parseFloat(a);
    if (sx && sy) hw = Math.max(MIN, k * Math.abs(px), k * Math.abs(py) * ar);
    else if (sx) hw = Math.max(MIN, k * Math.abs(px));
    else hw = Math.max(MIN, k * Math.abs(py) * ar);
    hh = hw / ar;
  }
  $('widthKm').value = (hw * 2 / 1000).toFixed(2);
  $('heightKm').value = (hh * 2 / 1000).toFixed(2);

  if (anchored) {
    // Put the centre where it has to be for the anchor to stay put. Axes the
    // handle does not drive keep the offset they had when the drag began.
    const cx = sx ? Math.sign(px || 1) * hw : dragCtx.offX;
    const cy = sy ? Math.sign(py || 1) * hh : dragCtx.offY;
    const rot = (+$('rot').value) * Math.PI / 180;
    const c = Math.cos(rot), sn = Math.sin(rot);
    const ex = cx * c - cy * sn, ey = cx * sn + cy * c;
    const m = metresPerDegree(dragCtx.anchorLat);
    state.lat = dragCtx.anchorLat + ey / m.lat;
    state.lon = dragCtx.anchorLon + ex / m.lon;
    syncCentreInputs();
    handle.setLatLng([state.lat, state.lon]);
  }
  drawRect();
}

function positionHandles() {
  const { w, h } = dims();
  handles.forEach(({ sx, sy, mk }) => {
    mk.setLatLng(pageToLatLng(sx * w / 2, sy * h / 2));
  });
}

function drawRect() {
  const { w, h } = dims();
  rect.setLatLngs(footprint(state.lat, state.lon, w, h, +$('rot').value));
  positionHandles();
}

function syncCentreInputs() {
  $('lat').value = state.lat.toFixed(5);
  $('lon').value = state.lon.toFixed(5);
}

/* ------------------------------------------------------------------ controls */
function bindOut(id, outId, fmt) {
  const el = $(id), out = $(outId);
  const upd = () => { out.textContent = fmt(el.value); };
  el.addEventListener('input', upd); upd();
}
bindOut('rot', 'rotOut', (v) => `${v}°`);
bindOut('blur', 'blurOut', (v) => v);
bindOut('simplify', 'simplifyOut', (v) => `${v} m`);
bindOut('smooth', 'smoothOut', (v) => v);
bindOut('hatchAngle', 'hatchAngleOut', (v) => `${v}\u00b0`);
bindOut('meshN', 'meshNOut', (v) => v);
bindOut('zExag', 'zExagOut', (v) => `${v}\u00d7`);

$('backplate').addEventListener('change', () => {
  $('plateSizeRow').classList.toggle('hidden', !$('backplate').checked);
  save();
});

$('indexOn').addEventListener('change', () => {
  $('indexEvery').disabled = !$('indexOn').checked;
  save();
});

$('aspect').addEventListener('change', () => {
  $('heightRow').classList.toggle('hidden', $('aspect').value !== 'custom');
  if ($('aspect').value !== 'custom') {
    $('heightKm').value = (dims().h / 1000).toFixed(2);
  }
  drawRect(); save();
});
/* Keep the hidden height field in step with a locked aspect, so switching to
   `custom…` starts from what is actually on screen rather than a stale value. */
function syncHeightField() {
  if ($('aspect').value !== 'custom') {
    $('heightKm').value = (dims().h / 1000).toFixed(2);
  }
}

['widthKm', 'heightKm', 'rot'].forEach((id) =>
  $(id).addEventListener('input', () => { syncHeightField(); drawRect(); save(); }));
['lat', 'lon'].forEach((id) => $(id).addEventListener('change', () => {
  state.lat = parseFloat($('lat').value); state.lon = parseFloat($('lon').value);
  handle.setLatLng([state.lat, state.lon]);
  map.panTo([state.lat, state.lon]); drawRect(); save(); checkCoverage();
  refreshOsmData(false);
}));

function fitToView() {
  const b = map.getBounds(), c = b.getCenter();
  state.lat = c.lat; state.lon = c.lng;
  const m = metresPerDegree(c.lat);
  const w = (b.getEast() - b.getWest()) * m.lon * 0.9;
  const h = (b.getNorth() - b.getSouth()) * m.lat * 0.9;
  $('widthKm').value = (w / 1000).toFixed(2);
  if ($('aspect').value === 'custom') $('heightKm').value = (h / 1000).toFixed(2);
  else {
    // keep the chosen aspect, use the dimension that fits
    const a = parseFloat($('aspect').value);
    const fitW = Math.min(w, h * a);
    $('widthKm').value = (fitW / 1000).toFixed(2);
  }
  syncCentreInputs(); handle.setLatLng([state.lat, state.lon]);
  drawRect(); save(); checkCoverage(); refreshOsmData(false);
}

$('fit').addEventListener('click', fitToView);

/* Follow keeps the capture inside whatever the map is showing. It only reads
   the map, never moves it, so there is no feedback loop with panTo. */
$('followView').addEventListener('change', () => {
  if ($('followView').checked) fitToView();
  save();
});
map.on('moveend', () => { if ($('followView').checked) fitToView(); });

/* ------------------------------------------------------------------- search */
async function search() {
  const q = $('q').value.trim();
  if (!q) return;
  status('searching…');
  try {
    const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const list = await r.json();
    const ul = $('results');
    ul.innerHTML = '';
    if (!list.length) { status('no matches'); ul.classList.add('hidden'); return; }
    list.forEach((d) => {
      const li = document.createElement('li');
      li.textContent = d.name;
      li.addEventListener('click', () => {
        state.lat = d.lat; state.lon = d.lon;
        syncCentreInputs(); handle.setLatLng([d.lat, d.lon]);
        map.setView([d.lat, d.lon], 13);
        drawRect(); ul.classList.add('hidden'); save(); checkCoverage();
        refreshOsmData(false);
      });
      ul.appendChild(li);
    });
    ul.classList.remove('hidden'); status('');
  } catch (e) { status('search failed', 'err'); }
}
$('qgo').addEventListener('click', search);
$('q').addEventListener('keydown', (e) => { if (e.key === 'Enter') search(); });

/* Suggest a sensible sampling distance for wherever the user just moved to,
   but never overrule a value they typed themselves. Replies can arrive out of
   order while the user drags, so only the newest request is allowed to win. */
let resTouched = false;
let coverageSeq = 0;
$('res').addEventListener('input', () => { resTouched = true; });

async function checkCoverage() {
  const seq = ++coverageSeq;
  try {
    const { w, h } = dims();
    const r = await fetch(`/api/coverage?lat=${state.lat}&lon=${state.lon}`
      + `&width_m=${Math.round(w)}&height_m=${Math.round(h)}`);
    const d = await r.json();
    if (seq !== coverageSeq) return;            // a newer check already landed
    const best = d.best_resolution_m;
    const have = (d.sources || []).filter((x) => x.available !== false);
    const partial = d.partial || [];
    $('demNote').innerHTML = `<b>finest available here: ${best} m</b>`
      + ` <button class="mini" id="useBest">use it</button>`;

    // The rundown of which sources cover this area is reference material, so
    // it sits behind the (i) rather than growing the panel on every map move.
    let info = have.length
      ? '<b>Covering this area:</b> ' + have.map((x) => x.label).join(', ')
      : '<b>No source covers this area.</b>';
    // A source that only clips a corner is not "available here": saying so
    // would send you sampling at 1 m for a map that is 30 m nearly everywhere.
    for (const x of partial) {
      info += `||${x.label} reaches ${x.native_m} m but covers only `
            + `${Math.round(x.share * 100)}% of this area, so it is not the`
            + ' limit that matters.';
    }
    info += '||Going finer than the source changes the drawing but adds no'
          + ' information. The Result panel reports what each source actually'
          + ' contributed.';
    $('resInfo').dataset.help = info;
    const ub = $('useBest');
    if (ub) ub.addEventListener('click', () => {
      $('res').value = best;
      resTouched = true;
      status(`sampling set to ${best} m`);
    });
    if (!resTouched) $('res').value = best;
  } catch (e) { /* offline is fine */ }
}

/* ------------------------------------------------------------------ generate */
function spec() {
  const { w, h } = dims();
  const layers = [...document.querySelectorAll('#layers input:checked')]
    .map((c) => c.value).filter((v) => v !== 'contours');
  const num = (id) => { const v = parseFloat($(id).value); return isNaN(v) ? null : v; };
  return {
    lat: state.lat, lon: state.lon, width_m: w, height_m: h,
    rotation_deg: +$('rot').value,
    resolution_m: +$('res').value,
    dem_source: $('demSource').value,
    interval: +$('interval').value,
    index_every: $('indexOn').checked ? +$('indexEvery').value : 0,
    level_min: num('levelMin'), level_max: num('levelMax'),
    blur_sigma_px: +$('blur').value,
    simplify_m: +$('simplify').value,
    smooth_iters: +$('smooth').value,
    min_length_m: +$('minLen').value,
    sea_fill: $('seaFill').checked,
    layers,
    contours_on: document.querySelector('#layers input[value=contours]').checked,
    width_mm: +$('widthMm').value,
    height_mm: SIZES[0].locked ? null : +$('heightMm').value,
    model_w_mm: +$('modelW').value,
    model_h_mm: SIZES[1].locked ? null : +$('modelH').value,
    mesh_smooth_m: +$('meshSmooth').value,
    mesh_trip: $('meshTrip').checked,
    trip_mm: +$('tripMm').value,
    trip_w_mm: +$('tripWmm').value,
    water_style: $('waterStyle').value,
    margin_mm: +$('marginMm').value,
    frame: $('frame').checked,
    labels: $('labelsOn').checked,
    trip_ids: [...state.trip],
    water_hatch: $('waterHatch').value,
    hatch_spacing_mm: +$('hatchSpacing').value,
    hatch_angle_deg: +$('hatchAngle').value,
    water_mask: $('waterMask').checked,
    include_sea: $('includeSea').checked,
    sea_level: +$('seaLevel').value,
    sea_source: $('seaSource').value,
    selectable: [...document.querySelectorAll('#selectable input:checked')]
      .map((c) => c.value),
    osm_source: $('osmSource').value,
    output: outputMode(),
    mesh_n: +$('meshN').value,
    z_exaggeration: +$('zExag').value,
    base_mm: +$('baseMm').value,
    nozzle_mm: +$('nozzleMm').value,
    mesh_water: $('meshWater').checked,
    water_mm: +$('waterMm').value,
    flat_tolerance: +$('flatTol').value,
    max_height_mm: +$('maxHeight').value,
    backplate: $('backplate').checked,
    backplate_w_mm: +$('backplateW').value,
    backplate_h_mm: +$('backplateH').value,
    backplate_mm: +$('backplateMm').value,
    mesh_buildings: $('meshBuildings').checked,
    mesh_roads: $('meshRoads').checked,
    buildings_mm: +$('buildingsMm').value,
    roads_mm: +$('roadsMm').value,
  };
}

function status(msg, cls = '') {
  const el = $('status');
  el.textContent = msg;
  el.className = 'status' + (cls ? ' ' + cls : '');
}

/* FastAPI reports its own errors as a string in `detail`, but a request that
   fails validation returns an *array* of error objects instead. Stringifying
   that blindly is where "[object Object]" came from. */
const FIELD_NAMES = {
  resolution_m: 'Sampling', interval: 'Interval', index_every: 'Index every',
  blur_sigma_px: 'Terrain blur', simplify_m: 'Simplify', smooth_iters: 'Curve',
  min_length_m: 'Drop under', width_mm: 'Page width', margin_mm: 'Margin',
  width_m: 'Width', height_m: 'Height', level_min: 'Clamp min',
  level_max: 'Clamp max', rotation_deg: 'Rotation',
};

function describeError(payload, status) {
  const d = payload && payload.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d.map((e) => {
      const raw = (e.loc || []).filter((x) => x !== 'body').join('.');
      const name = FIELD_NAMES[raw] || raw;
      let msg = (e.msg || 'is not valid').replace(/^Input should be/, 'must be');
      if (e.input !== undefined && e.input !== null) msg += ` (you gave ${e.input})`;
      return name ? `${name} ${msg}` : msg;
    }).join('; ');
  }
  return `HTTP ${status}`;
}

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let payload = null;
    try { payload = await r.json(); } catch (e) { /* not JSON */ }
    throw new Error(describeError(payload, r.status));
  }
  return r;
}

let tickTimer = null;

function startTicker(label) {
  const t0 = performance.now();
  clearInterval(tickTimer);
  const paint = () => status(`${label} ${((performance.now() - t0) / 1000).toFixed(1)}s`);
  paint();
  tickTimer = setInterval(paint, 200);
  return () => { clearInterval(tickTimer); tickTimer = null; };
}

async function generate() {
  if (state.busy) return;
  state.busy = true;
  $('generate').disabled = true;
  if (outputMode() === 'mesh') {
    try { await generateMesh(); }
    finally { state.busy = false; $('generate').disabled = false; save(); }
    return;
  }
  const stop = startTicker('generating…');
  const t0 = performance.now();
  try {
    const s = spec();
    const data = await (await post('/api/generate', s)).json();
    state.lastSpec = s;
    $('preview').innerHTML = data.svg;
    wirePreview();
    showStats(data);
    $('export').disabled = false;
    stop();
    const secs = ((performance.now() - t0) / 1000).toFixed(1);
    status(data.warnings.length ? data.warnings.join(' · ') : `done in ${secs}s`,
      data.warnings.length ? 'warn' : '');
    const shaky = data.osm && (data.osm.failed || data.osm.degraded);
    refreshOsmData(!!shaky, data.osm && data.osm.failed
      ? 'Overpass is unavailable for this area.'
      : 'Overpass only partly answered for this area.');
  } catch (e) {
    stop(); status(e.message, 'err');
  } finally {
    stop();
    state.busy = false; $('generate').disabled = false; save();
  }
}
$('generate').addEventListener('click', generate);

$('export').addEventListener('click', async () => {
  if (outputMode() === 'mesh') {
    const fmt = $('meshFormat').value;
    status('writing mesh…');
    try {
      const r = await post(`/api/mesh/export?format=${fmt}`, spec());
      const blob = await r.blob();
      const cd = r.headers.get('Content-Disposition') || '';
      const name = (cd.match(/filename="([^"]+)"/) || [, `mesh.${fmt}`])[1];
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = name; a.click();
      URL.revokeObjectURL(url);
      status(`saved ${name} (also written to exports/)`);
    } catch (e) { status(e.message, 'err'); }
    return;
  }
  if (!state.lastSpec) return;
  status('rendering export…');
  try {
    const s = { ...state.lastSpec, trip_ids: [...state.trip] };
    const r = await post('/api/export', s);
    const blob = await r.blob();
    const cd = r.headers.get('Content-Disposition') || '';
    const name = (cd.match(/filename="([^"]+)"/) || [, 'contours.svg'])[1];
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
    status(`saved ${name} (also written to exports/)`);
  } catch (e) { status(e.message, 'err'); }
});

/* --------------------------------------------------------- trip selection */
function applyTrip() {
  document.querySelectorAll('#preview path.sel').forEach((p) => {
    p.classList.toggle('trip', state.trip.has(p.dataset.id));
  });
  const n = state.trip.size;
  $('tripHint').textContent = n
    ? `${n} segment${n === 1 ? '' : 's'} in the trip layer`
    : 'click or drag over paths to build a trip';
}

function wirePreview() {
  const svg = $('preview').querySelector('svg');
  if (!svg) return;
  let painting = false, mode = true;
  const hit = (t) => (t && t.classList && t.classList.contains('sel')) ? t : null;

  svg.addEventListener('mousedown', (e) => {
    const p = hit(e.target);
    if (!p) return;
    e.preventDefault();
    painting = true;
    mode = !state.trip.has(p.dataset.id);
    toggle(p, mode);
  });
  svg.addEventListener('mouseover', (e) => {
    if (!painting) return;
    const p = hit(e.target);
    if (p) toggle(p, mode);
  });
  window.addEventListener('mouseup', () => { painting = false; });

  function toggle(p, on) {
    if (on) state.trip.add(p.dataset.id); else state.trip.delete(p.dataset.id);
    applyTrip();
  }
  applyTrip();
}
$('clearTrip').addEventListener('click', () => { state.trip.clear(); applyTrip(); });
document.querySelectorAll('[data-view]').forEach((b) =>
  b.addEventListener('click', () => window.MeshViewer
    && window.MeshViewer.setView(b.dataset.view)));
$('zoomFit').addEventListener('click', () => {
  const pane = $('preview');
  pane.classList.toggle('actual');
  $('zoomFit').textContent = pane.classList.contains('actual') ? 'fit' : '1:1';
});

/* ---------------------------------------------------------------------- help
   Small popovers on the settings that are hard to remember. Clicking pins one
   open; it closes on the next click elsewhere, Escape, or the close button. */
const helpBox = $('helpBox');

function showHelp(btn) {
  document.querySelectorAll('button.help.on').forEach((b) => b.classList.remove('on'));
  btn.classList.add('on');
  $('helpTitle').textContent = btn.dataset.title;
  $('helpBody').innerHTML = btn.dataset.help
    .split('||').map((para) => `<p>${para}</p>`).join('');
  helpBox.classList.remove('hidden');

  const r = btn.getBoundingClientRect();
  const bw = helpBox.offsetWidth, bh = helpBox.offsetHeight;
  let left = r.right + 10;
  if (left + bw > window.innerWidth - 8) left = Math.max(8, r.left - bw - 10);
  let top = r.top - 6;
  if (top + bh > window.innerHeight - 8) top = Math.max(8, window.innerHeight - bh - 8);
  helpBox.style.left = `${left}px`;
  helpBox.style.top = `${top}px`;
}

function hideHelp() {
  helpBox.classList.add('hidden');
  document.querySelectorAll('button.help.on').forEach((b) => b.classList.remove('on'));
}

// delegated: help and info dots are built dynamically too (the download
// offer, for one), and binding once at load would leave those dead
document.addEventListener('click', (e) => {
  const btn = e.target.closest && e.target.closest('button.help');
  if (!btn) return;
  e.preventDefault(); e.stopPropagation();
  if (btn.classList.contains('on')) hideHelp(); else showHelp(btn);
});
document.addEventListener('click', (e) => {
  // both handlers sit on document, so stopPropagation in the opener does not
  // reach this one — without the guard every popover closed as it opened
  if (e.target.closest && e.target.closest('button.help')) return;
  if (!helpBox.contains(e.target)) hideHelp();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideHelp(); });
$('helpClose').addEventListener('click', hideHelp);

/* ------------------------------------------------------------------ osm data
   Local Geofabrik extracts. Downloading one is hundreds of megabytes, so it
   only ever happens when the user asks — normally in response to Overpass
   having just failed on the area they are looking at. */
let osmSeq = 0;
let osmCovering = null;
let osmImporting = [];

// an import already running makes every download/extend button a mistake
function markImporting() {
  if (!osmImporting.length) return;
  document.querySelectorAll('#osmOffer [data-install], #osmOffer [data-extend]')
    .forEach((b) => { b.disabled = true; b.title = 'an import is already running'; });
}

// "local only" does not fall back, so it must not be described as if it does
function fallbackNote() {
  return $('osmSource').value === 'local'
    ? 'and the source is set to local only, so no OSM layers will be drawn.'
    : 'using Overpass.';
}

function mb(v) { return v == null ? '?' : (v >= 1000 ? `${(v / 1000).toFixed(1)} GB` : `${Math.round(v)} MB`); }

async function refreshOsmData(offerIfMissing = false, reason) {
  if (reason) offerReason = reason;
  const seq = ++osmSeq;
  const { w, h } = dims();
  try {
    const r = await fetch(`/api/osmdata?lat=${state.lat}&lon=${state.lon}`
      + `&width_m=${Math.round(w)}&height_m=${Math.round(h)}`
      + `&candidates=${offerIfMissing ? 'true' : 'false'}`);
    const d = await r.json();
    if (seq !== osmSeq) return;
    osmCovering = d.covering;

    if (d.covering) {
      $('osmStatus').innerHTML =
        `local extract covers this area: ${d.covering.name} `
        + `(${d.covering.features.toLocaleString()} features, ${mb(d.covering.mb)})`;
    } else if (d.extendable) {
      // Having the right country but the wrong clip is not the same as having
      // nothing, and it is fixed by widening rather than starting over.
      const x = d.extendable;
      $('osmStatus').innerHTML =
        `${x.name} is installed but was cut down to a smaller area, `
        + `so it has nothing here — ${fallbackNote()} `
        + `<button class="mini" id="wantExtend">extend it${x.has_source ? '' : ' (needs re-download)'}</button>`;
      $('wantExtend').addEventListener('click', () => showExtend(x));
    } else {
      $('osmStatus').innerHTML =
        `no local extract covers this area — ${fallbackNote()} `
        + '<button class="mini" id="wantLocal">download one</button>';
      const b = $('wantLocal');
      if (b) b.addEventListener('click', () => refreshOsmData(true));
    }

    $('osmInstalled').innerHTML = (d.installed || []).map((i) => `
      <div class="store"><span>${i.name}${i.clipped ? ' <b class="tag">clipped</b>' : ''}</span>
        <em>${i.features.toLocaleString()} features · ${mb(i.mb)}</em>
        ${i.has_source ? `<button class="mini" data-dropsrc="${i.id}" title="delete the kept .pbf, keeping the imported data">drop source</button>` : ''}
        <button class="mini" data-remove="${i.id}">remove</button></div>`).join('');
    $('osmInstalled').querySelectorAll('[data-remove]').forEach((b) =>
      b.addEventListener('click', () => removeStore(b.dataset.remove)));
    $('osmInstalled').querySelectorAll('[data-dropsrc]').forEach((b) =>
      b.addEventListener('click', () => dropSource(b.dataset.dropsrc)));

    osmImporting = d.importing || [];
    if (osmImporting.length) {
      const j = osmImporting[0];
      const eta = j.eta_s ? ` — about ${Math.round(j.eta_s / 60) || 1} min left` : '';
      $('osmStatus').innerHTML +=
        `<br><b>importing ${j.extract} now${eta}.</b> `
        + 'It covers this area only once that finishes.';
    }
    if (offerIfMissing && !d.covering && d.extendable) showExtend(d.extendable);
    else if (offerIfMissing && !d.covering) showOffer(d.candidates || [], d.error);
    else if (!offerIfMissing) $('osmOffer').classList.add('hidden');
  } catch (e) { $('osmStatus').textContent = ''; }
}

let offerReason = 'Download this area for offline use.';

const EXTEND_HELP = 'Extending re-imports the same extract with its clip widened to cover both the area you already had and this one, keeping every layer the store was imported with. Nothing you have is lost.||If the source file was kept it re-imports straight away; otherwise it downloads once more first. Re-importing takes about as long as the first import did.||Extracts are clipped on import to keep them small, which is why a country-sized download can still miss a town 40 km away.';

function showOffer(cands, error) {
  openOsmDetails();
  const box = $('osmOffer');
  if (error) {
    box.innerHTML = `<b>Overpass is unavailable here.</b><br>${error}`;
    box.classList.remove('hidden');
    return;
  }
  if (!cands.length) {
    box.innerHTML = '<b>Overpass is unavailable here</b><br>'
      + 'and no Geofabrik extract matched this area.';
    box.classList.remove('hidden');
    return;
  }
  box.innerHTML = `<b>${offerReason}</b><br>`
    + 'Download the region once and it works offline from then on.'
    + '<div class="opts">'
    + '<label><input type="checkbox" id="dlBuildings"> include buildings</label>'
    + '<label><input type="checkbox" id="dlClip" checked> only within '
    + '<input type="number" id="dlClipKm" value="40" min="5" max="500" step="5"> km of here</label>'
    + '<label><input type="checkbox" id="dlKeep"> keep the downloaded file '
    + 'so the area can be widened later without downloading again</label>'
    + '<div class="hintline">Buildings are usually over half the data, so '
    + 'leaving them out roughly halves the store. Import time is dominated by '
    + 'reading the file, so a big region takes several minutes either way.</div>'
    + '</div>'
    + '<div class="cands">' + cands.map((c) => `
        <div class="cand"><span>${c.name}</span><em>${mb(c.mb)}</em>
          <button class="mini" data-install="${c.id}">download</button></div>`).join('')
    + '</div>';
  box.classList.remove('hidden');
  box.querySelectorAll('[data-install]').forEach((b) =>
    b.addEventListener('click', () => installStore(b.dataset.install, b)));
  markImporting();
}

function openOsmDetails() {
  const d = document.getElementById('osmMore');
  if (d) d.open = true;
}

function showExtend(x) {
  openOsmDetails();
  const box = $('osmOffer');
  const has = new Set(x.layers || []);
  const all = x.layers == null;
  box.innerHTML = `<b>Widen ${x.name} to cover this area</b>`
    + `<button type="button" class="help info" data-title="Extending an extract"`
    + ` data-help="${EXTEND_HELP}" aria-label="What does extending do?">i</button><br>`
    + (x.has_source
      ? 'The source file was kept, so this re-imports without downloading anything.'
      : 'The source file was not kept, so it has to be downloaded again first.')
    + '<div class="opts">'
    + '<label><input type="checkbox" id="dlBuildings"'
    + `${all || has.has('buildings') ? ' checked' : ''}> include buildings</label>`
    + '<label><input type="checkbox" id="dlClip" checked> covering '
    + '<input type="number" id="dlClipKm" value="40" min="5" max="500" step="5">'
    + ' km around here</label>'
    + `<label><input type="checkbox" id="dlKeep"${x.has_source ? ' checked' : ''}>`
    + ' keep the source file for future extends</label>'
    + '</div>'
    + `<div class="cands"><div class="cand"><span>${x.name}</span>`
    + `<em>currently ${mb(x.mb)}</em>`
    + `<button class="mini" data-extend="${x.id}">extend</button></div></div>`;
  box.classList.remove('hidden');
  box.querySelector('[data-extend]')
     .addEventListener('click', (e) => installStore(x.id, e.target, true));
  markImporting();
}

async function installStore(id, btn, extend = false) {
  const box = $('osmOffer');
  btn.disabled = true;
  box.insertAdjacentHTML('beforeend',
    '<div id="osmProg">starting…<div class="bar"><i></i></div></div>');
  const prog = $('osmProg'), bar = prog.querySelector('i');
  try {
    const layerSet = ['roads', 'paths', 'rivers', 'water', 'coastline',
                      'glaciers', 'railways', 'peaks', 'places'];
    if ($('dlBuildings') && $('dlBuildings').checked) layerSet.push('buildings');
    const body = { extract_id: id, layers: layerSet, extend };
    if ($('dlKeep') && $('dlKeep').checked) body.keep_source = true;
    if ($('dlClip') && $('dlClip').checked) {
      body.clip_km = +$('dlClipKm').value || 40;
      body.lat = state.lat; body.lon = state.lon;
    }
    // not post(): a 409 here is an expected answer ("already running"), not
    // an error to throw
    const r = await fetch('/api/osmdata/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.status === 409) {
      const d = (await r.json()).detail || {};
      const eta = d.eta_s ? ` — about ${Math.round(d.eta_s / 60) || 1} min left` : '';
      prog.firstChild.textContent =
        `already importing this extract${eta}; leave it running`;
      return;
    }
    if (!r.ok) throw new Error(`install refused (HTTP ${r.status})`);
    const { job } = await r.json();
    for (;;) {
      await new Promise((res) => setTimeout(res, 700));
      const jr = await fetch(`/api/osmdata/job/${job}`);
      if (jr.status === 404) {
        // jobs live in the server's memory, so a restart loses them; without
        // this the poll just spins on 404s and the UI never says anything
        throw new Error('the server restarted and lost this import — '
                        + 'start it again (any downloaded file is reused)');
      }
      const j = await jr.json();
      if (j.phase === 'downloading') {
        const pct = j.total ? (j.downloaded / j.total) * 100 : 0;
        bar.style.width = `${pct}%`;
        prog.firstChild.textContent =
          `downloading ${j.name || id} — ${mb((j.downloaded || 0) / 1e6)} of ${mb((j.total || 0) / 1e6)}`;
      } else if (j.phase === 'counting') {
        bar.style.width = '100%';
        prog.firstChild.textContent = j.reused_source
          ? `reusing the ${j.pbf_mb} MB source — no download needed…`
          : `sizing up ${j.pbf_mb} MB…`;
      } else if (j.phase === 'importing') {
        const est = j.total_objects_est || 0;
        const seen = j.scanned || 0;
        const pct = est ? Math.min(99, (seen / est) * 100) : 0;
        bar.style.width = `${pct}%`;
        const eta = j.eta_s != null
          ? ` · about ${j.eta_s > 90 ? Math.round(j.eta_s / 60) + ' min' : j.eta_s + 's'} left`
          : '';
        prog.firstChild.textContent = est
          ? `importing ${j.pbf_mb} MB — ${seen.toLocaleString()} of ~${est.toLocaleString()} objects${eta}`
          : `importing — ${seen.toLocaleString()} objects scanned`;
      }
      if (j.done) {
        if (j.error) throw new Error(j.error);
        prog.firstChild.textContent = `done — ${mb(j.store_mb)} store`;
        status(`local OSM data for ${j.name || id} installed`);
        break;
      }
    }
    await refreshOsmData(false);
    $('osmOffer').classList.add('hidden');
  } catch (e) {
    status(`download failed: ${e.message}`, 'err');
    btn.disabled = false;
  }
}

async function dropSource(id) {
  try {
    const r = await fetch(`/api/osmdata/${encodeURIComponent(id)}/source`,
                          { method: 'DELETE' });
    const d = await r.json();
    status(`source file removed — ${d.freed_mb} MB freed`);
  } catch (e) { status('could not remove the source file', 'err'); }
  refreshOsmData(false);
}

async function removeStore(id) {
  try {
    const r = await fetch(`/api/osmdata/${encodeURIComponent(id)}`, { method: 'DELETE' });
    const d = await r.json();
    status(`removed ${id} — ${d.freed_mb} MB freed`);
  } catch (e) { status('could not remove that extract', 'err'); }
  refreshOsmData(false);
}

$('osmSource').addEventListener('change', () => { save(); refreshOsmData(false); });

/* --------------------------------------------------------------------- cache */
async function refreshCache() {
  try {
    const d = await (await fetch('/api/cache')).json();
    $('cacheSize').textContent = `${d.cache_mb} MB on disk`;
  } catch (e) { $('cacheSize').textContent = '—'; }
}

$('clearCache').addEventListener('click', async () => {
  const btn = $('clearCache');
  btn.disabled = true; btn.textContent = 'clearing…';
  try {
    const d = await (await fetch('/api/cache', { method: 'DELETE' })).json();
    status(`cache cleared — ${d.freed_mb} MB freed from ${d.removed} files`);
  } catch (e) {
    status('could not clear the cache', 'err');
  } finally {
    btn.disabled = false; btn.textContent = 'clear';
    refreshCache();
  }
});

/* -------------------------------------------------------------- output mode
   Collection is identical for both outputs; only the rendering differs, so
   switching mode just changes which controls are relevant and which pane the
   result lands in. */
function outputMode() {
  const r = document.querySelector('#outputMode input:checked');
  return r ? r.value : 'svg';
}

function applyMode() {
  const m = outputMode();
  // The two tab bodies are different heights, so swapping them shifts
  // everything below and the panel appears to jump. Pin the tab bar where it
  // is on screen and put the scroll back afterwards.
  const panel = $('panel');
  const bar = document.querySelector('.outputs');
  const before = bar ? bar.getBoundingClientRect().top : null;
  document.querySelectorAll('[data-mode]').forEach((el) => {
    el.classList.toggle('hidden', el.dataset.mode !== m);
  });
  $('preview').classList.toggle('hidden', m !== 'svg');
  $('mesh3d').classList.toggle('hidden', m !== 'mesh');
  $('tripHint').textContent = m === 'mesh'
    ? 'drag orbit · ⇧ or ⌘ drag pan · scroll zoom · double-click to re-centre'
    : 'click or drag over paths to build a trip';
  $('export').textContent = m === 'mesh' ? 'Download mesh' : 'Download SVG';
  if (m === 'mesh' && window.MeshViewer) setTimeout(() => window.MeshViewer.resize(), 60);
  if (before !== null) {
    const after = bar.getBoundingClientRect().top;
    panel.scrollTop += after - before;
  }
  meshEstimate();
  save();
}

document.querySelectorAll('#outputMode input').forEach(
  (r) => r.addEventListener('change', applyMode));

/* Triangles ≈ 2·(n−1)² for the surface plus ~1% for walls and base; binary STL
   is 50 bytes each. Worth showing live — it is the number that decides whether
   a setting is usable. */
function meshEstimate() {
  const n = +$('meshN').value;
  const { w, h } = dims();
  const long = Math.max(w, h), short = Math.min(w, h);
  const nl = n, ns = Math.max(2, Math.round(n * short / long));
  const tris = Math.round(2 * (nl - 1) * (ns - 1) * 1.01);
  const stl = tris * 50 / 1e6;
  const cell = (long / (nl - 1)) * (+$('modelW').value / w);
  $('meshEstimate').innerHTML =
    `${nl} × ${ns} → <b>${tris.toLocaleString()}</b> triangles · `
    + `STL ~${stl.toFixed(1)} MB · cell ${cell.toFixed(2)} mm`
    + (cell < +$('nozzleMm').value
        ? ' <span style="color:#ddb257">· finer than the nozzle</span>' : '');
}
['meshN', 'modelW', 'nozzleMm', 'widthKm', 'heightKm', 'aspect'].forEach((id) =>
  $(id).addEventListener('input', meshEstimate));

async function generateMesh() {
  const stop = startTicker('building mesh…');
  const t0 = performance.now();
  try {
    const r = await post('/api/mesh', spec());
    const buf = await r.arrayBuffer();
    const stats = window.MeshViewer.show(buf);
    stop();
    showMeshStats(stats);
    $('export').disabled = false;
    status(stats.warnings.length ? stats.warnings.join(' · ')
      : `done in ${((performance.now() - t0) / 1000).toFixed(1)}s`,
      stats.warnings.length ? 'warn' : '');
    const shaky = stats.osm && (stats.osm.failed || stats.osm.degraded);
    refreshOsmData(!!shaky, stats.osm && stats.osm.failed
      ? 'Overpass is unavailable for this area.'
      : 'Overpass only partly answered for this area.');
  } catch (e) {
    stop(); status(e.message, 'err');
  }
}

function showMeshStats(s) {
  const rows = [];
  if (s.dem && s.dem.source) {
    rows.push(['hd', 'Elevation', '']);
    (s.dem.sources || []).forEach((x) => rows.push(
      ['', x.label, `${Math.round(x.share * 100)}%${x.native_m ? ` @ ${x.native_m} m` : ''}`]));
  }
  rows.push(['hd', 'Model', `${s.size_mm[0]} × ${s.size_mm[1]} × ${s.size_mm[2]} mm`]);
  rows.push(['', 'grid', `${s.grid[0]} × ${s.grid[1]}`]);
  rows.push(['', 'cell', `${s.cell_mm} mm`]);
  rows.push(['', 'exaggeration', s.exaggeration_asked !== s.exaggeration
    ? `${s.exaggeration}× (asked ${s.exaggeration_asked}×)`
    : `${s.exaggeration}×`]);
  if (s.backplate) {
    rows.push(['', 'backplate', `${s.backplate[0]} × ${s.backplate[1]} × ${s.backplate[2]} mm`]);
  }
  if (s.watertight) {
    rows.push(['', 'watertight', 'yes']);
  } else {
    const bad = Object.entries(s.mesh_check || {})
      .filter(([, v]) => v.boundary || v.nonmanifold || v.degenerate);
    rows.push(['', 'watertight', 'no']);
    bad.forEach(([k, v]) => rows.push(
      ['', `↳ ${k}`, `${v.boundary + v.nonmanifold + v.degenerate} bad edges`]));
  }
  rows.push(['hd', 'Objects', `${s.triangles.toLocaleString()} tris`]);
  s.objects.forEach((o) => rows.push(['', o.name, o.triangles.toLocaleString()]));
  (s.water || []).forEach((w) => rows.push(
    ['', `${w.what} level`, `${w.level_m} m · ${Math.round(w.share * 100)}%`]));
  const t = s.timings || {};
  const shown = [['elevation', 'elevation'], ['osm', 'OSM layers'], ['mesh', 'mesh']]
    .filter(([k]) => t[k] != null && t[k] >= 0.05);
  if (shown.length) {
    rows.push(['hd', 'Time', `${s.seconds}s total`]);
    shown.forEach(([k, l]) => rows.push(['', l, `${t[k]}s`]));
  }
  $('cacheSize').textContent = `${s.cache_mb} MB on disk`;
  $('stats').innerHTML = '<table>' + rows.map(
    ([c, a, b]) => `<tr class="${c}"><td>${a}</td><td>${b}</td></tr>`).join('') + '</table>';
  $('statsBox').classList.remove('hidden');
}

/* --------------------------------------------------------------------- stats */
function showStats(d) {
  const rows = [];
  if (d.dem.source) {
    rows.push(['hd', 'Elevation', '']);
    (d.dem.sources || []).forEach((s) => {
      const at = s.native_m ? ` @ ${s.native_m} m` : '';
      rows.push(['', s.label, `${Math.round(s.share * 100)}%${at}`]);
    });
    rows.push(['', 'output grid', `${d.dem.resolution_m} m`]);
    rows.push(['', 'grid', d.dem.grid ? d.dem.grid.join(' × ') : '—']);
    if (d.elevation.min != null) {
      rows.push(['', 'range', `${Math.round(d.elevation.min)}–${Math.round(d.elevation.max)} m`]);
    }
  }
  const t = d.timings || {};
  const order = [['elevation', 'elevation'], ['contours', 'contours'],
                 ['osm', 'OSM layers'], ['water', 'water'], ['svg', 'svg']];
  const shown = order.filter(([k]) => t[k] != null && t[k] >= 0.05);
  if (shown.length) {
    rows.push(['hd', 'Time', `${d.seconds}s total`]);
    shown.forEach(([k, label]) => rows.push(['', label, `${t[k]}s`]));
  }
  rows.push(['hd', 'Page', '']);
  rows.push(['', 'size', `${d.page.width_mm} × ${d.page.height_mm} mm`]);
  rows.push(['', 'scale', d.page.scale]);
  rows.push(['hd', 'Layers', 'paths / pen']);
  Object.entries(d.layers).sort().forEach(([k, v]) => {
    rows.push(['', k, `${v.count} / ${v.pen_m} m`]);
  });
  $('cacheSize').textContent = `${d.cache_mb} MB on disk`;
  $('stats').innerHTML = '<table>' + rows.map(
    ([cls, a, b]) => `<tr class="${cls}"><td>${a}</td><td>${b}</td></tr>`).join('') + '</table>';
  $('statsBox').classList.remove('hidden');
}

/* ------------------------------------------------------------------- layout
   Three panes: controls, map, preview. Each can be hidden, and the two
   dividers resize. Horizontal space is often the scarce resource here, so the
   sizes and visibility persist. */
const LAYOUT_KEY = 'mapcontours.layout.v1';
const layout = { panel: 330, split: 0.5, hidden: {} };

function applyLayout() {
  $('panel').style.flex = `0 0 ${layout.panel}px`;
  $('panel').style.width = `${layout.panel}px`;
  // grow factors must sum to at least 1: below that CSS distributes only that
  // fraction of the free space, so a hidden sibling leaves an empty gap
  $('map').style.flex = `${layout.split * 100} 1 0`;
  $('previewPane').style.flex = `${(1 - layout.split) * 100} 1 0`;
  ['panel', 'map', 'preview'].forEach((k) => {
    const el = $(k === 'preview' ? 'previewPane' : k);
    el.classList.toggle('hidden', !!layout.hidden[k]);
    const btn = document.querySelector(`.railbtn[data-pane="${k}"]`);
    if (btn) btn.classList.toggle('on', !layout.hidden[k]);
  });
  // both dividers are pointless if what they separate is gone
  $('splitA').style.display = layout.hidden.panel ? 'none' : '';
  $('splitB').style.display =
    (layout.hidden.map || layout.hidden.preview) ? 'none' : '';
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch (e) { }
  if (map) setTimeout(() => map.invalidateSize(), 60);
}

function togglePane(k) {
  const others = ['panel', 'map', 'preview'].filter((x) => x !== k);
  // never let the last visible pane disappear
  if (!layout.hidden[k] && others.every((x) => layout.hidden[x])) return;
  layout.hidden[k] = !layout.hidden[k];
  applyLayout();
}

document.querySelectorAll('.railbtn').forEach((b) =>
  b.addEventListener('click', () => togglePane(b.dataset.pane)));
window.addEventListener('keydown', (e) => {
  if (!e.altKey || e.metaKey || e.ctrlKey) return;
  const k = { 1: 'panel', 2: 'map', 3: 'preview' }[e.key];
  if (k) { e.preventDefault(); togglePane(k); }
});

function dragSplitter(el, onMove) {
  el.addEventListener('mousedown', (e) => {
    e.preventDefault();
    el.classList.add('dragging');
    document.body.classList.add('resizing');
    const move = (ev) => onMove(ev.clientX);
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      el.classList.remove('dragging');
      document.body.classList.remove('resizing');
      applyLayout();
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });
}

dragSplitter($('splitA'), (x) => {
  const left = $('rail').getBoundingClientRect().right;
  layout.panel = Math.max(210, Math.min(620, x - left));
  $('panel').style.flex = `0 0 ${layout.panel}px`;
  $('panel').style.width = `${layout.panel}px`;
});
dragSplitter($('splitB'), (x) => {
  const box = document.querySelector('main').getBoundingClientRect();
  const f = (x - box.left) / box.width;
  layout.split = Math.max(0.12, Math.min(0.88, f));
  // grow factors must sum to at least 1: below that CSS distributes only that
  // fraction of the free space, so a hidden sibling leaves an empty gap
  $('map').style.flex = `${layout.split * 100} 1 0`;
  $('previewPane').style.flex = `${(1 - layout.split) * 100} 1 0`;
});
$('splitA').addEventListener('dblclick', () => togglePane('panel'));
$('splitB').addEventListener('dblclick', () => togglePane('preview'));

function loadLayout() {
  try {
    const o = JSON.parse(localStorage.getItem(LAYOUT_KEY) || 'null');
    if (o) {
      layout.panel = o.panel || layout.panel;
      layout.split = o.split || layout.split;
      layout.hidden = o.hidden || {};
    }
  } catch (e) { }
  applyLayout();
}

/* ----------------------------------------------------------------- sizing
   The drawing and the model are sized independently — sharing one pair of
   fields made "Model size" mean the page in SVG mode. Each pair keeps the
   capture's aspect while its lock is closed; opening it allows a stretch. */
const SIZES = [
  { w: 'widthMm', h: 'heightMm', lock: 'sizeLock', locked: true },
  { w: 'modelW', h: 'modelH', lock: 'modelLock', locked: true },
];

function syncSize(sz) {
  if (!sz.locked) return;
  const { w, h } = dims();
  // trailing ".0" costs a character the field cannot spare
  const v = +$(sz.w).value * h / w;
  $(sz.h).value = Number.isInteger(+v.toFixed(1)) ? String(Math.round(v)) : v.toFixed(1);
}

function syncModelHeight() { SIZES.forEach(syncSize); }

function applyLock() {
  for (const sz of SIZES) {
    $(sz.lock).classList.toggle('on', sz.locked);
    $(sz.lock).textContent = sz.locked ? '\u{1F512}' : '\u{1F513}';
    $(sz.lock).title = sz.locked
      ? 'aspect locked to the capture — click to unlock'
      : 'aspect free — click to lock to the capture';
    $(sz.h).readOnly = sz.locked;
    syncSize(sz);
  }
  meshEstimate();
}

SIZES.forEach((sz) => $(sz.lock).addEventListener('click', () => {
  sz.locked = !sz.locked; applyLock(); save();
}));
['widthMm', 'modelW', 'widthKm', 'heightKm', 'aspect'].forEach((id) =>
  $(id).addEventListener('input', () => { syncModelHeight(); }));

/* ------------------------------------------------------------------ persist */
const FIELDS = ['widthKm', 'heightKm', 'aspect', 'rot', 'demSource', 'osmSource', 'res', 'interval',
  'indexEvery', 'levelMin', 'levelMax', 'blur', 'simplify', 'smooth', 'minLen',
  'widthMm', 'heightMm', 'modelW', 'modelH', 'meshSmooth', 'waterStyle', 'marginMm', 'waterHatch', 'hatchSpacing', 'hatchAngle', 'seaLevel', 'seaSource',
  'meshN', 'zExag', 'baseMm', 'nozzleMm', 'buildingsMm', 'roadsMm', 'meshFormat',
  'waterMm', 'flatTol', 'maxHeight', 'backplateW', 'backplateH', 'backplateMm',
  'tripMm', 'tripWmm'];
const CHECKS = ['frame', 'labelsOn', 'seaFill', 'indexOn', 'waterMask', 'includeSea',
  'meshWater', 'meshBuildings', 'meshRoads', 'meshTrip', 'backplate', 'followView'];

function save() {
  const o = { lat: state.lat, lon: state.lon, zoom: map.getZoom() };
  FIELDS.forEach((f) => o[f] = $(f).value);
  CHECKS.forEach((c) => { o[c] = $(c).checked; });
  o.omode = outputMode();
  o.sizeLocked = SIZES[0].locked;
  o.modelLocked = SIZES[1].locked;
  o.layers = [...document.querySelectorAll('#layers input:checked')].map((c) => c.value);
  o.selectable = [...document.querySelectorAll('#selectable input:checked')].map((c) => c.value);
  try { localStorage.setItem(STORE, JSON.stringify(o)); } catch (e) { }
}

function load() {
  let o;
  try { o = JSON.parse(localStorage.getItem(STORE) || 'null'); } catch (e) { }
  if (o) {
    state.lat = o.lat ?? state.lat; state.lon = o.lon ?? state.lon;
    FIELDS.forEach((f) => { if (o[f] != null && o[f] !== '') $(f).value = o[f]; });
    CHECKS.forEach((c) => { if (o[c] != null) $(c).checked = o[c]; });
    if (o.sizeLocked != null) SIZES[0].locked = o.sizeLocked;
    if (o.modelLocked != null) SIZES[1].locked = o.modelLocked;
    if (o.omode) {
      const r = document.querySelector(`#outputMode input[value="${o.omode}"]`);
      if (r) r.checked = true;
    }
    if (o.layers) {
      document.querySelectorAll('#layers input').forEach(
        (c) => c.checked = o.layers.includes(c.value));
    }
    if (o.selectable) {
      document.querySelectorAll('#selectable input').forEach(
        (c) => c.checked = o.selectable.includes(c.value));
    }
    map.setView([state.lat, state.lon], o.zoom || 12);
    handle.setLatLng([state.lat, state.lon]);
  }
  $('heightRow').classList.toggle('hidden', $('aspect').value !== 'custom');
  syncHeightField();
  $('indexEvery').disabled = !$('indexOn').checked;
  $('plateSizeRow').classList.toggle('hidden', !$('backplate').checked);
  applyLock();
  syncCentreInputs();
  ['rot:rotOut', 'blur:blurOut', 'simplify:simplifyOut', 'smooth:smoothOut',
   'hatchAngle:hatchAngleOut', 'meshN:meshNOut', 'zExag:zExagOut']
    .forEach((p) => $(p.split(':')[0]).dispatchEvent(new Event('input')));
  drawRect();
}
document.querySelectorAll('#layers input, #selectable input, #frame, #labelsOn,'
  + ' #seaFill, #indexOn, #waterMask, #includeSea, #waterHatch, #demSource')
  .forEach((c) => c.addEventListener('change', save));

loadLayout();
load();
applyMode();
checkCoverage();
refreshCache();
refreshOsmData(false);

/* Handy from the browser console, and used by the UI tests. */
window.MC = { map, state, rect, handles, dims, drawRect, resizeFrom,
              beginResize, pageToLatLng, latLngToPage, toPageAxes };
