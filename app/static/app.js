'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts });
  if (r.status === 401) { location.href = '/login'; throw new Error('auth'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
// toast slides in/out (idea 8)
function toast(msg, ms = 2600) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove('show'), ms);
}
function form(obj) { const f = new FormData(); for (const [k, v] of Object.entries(obj)) f.append(k, v); return f; }

// ---------- sound cues (idea 9: implemented by sfx.js / window.FaceSfx) ----------
function sfx(kind) { try { window.FaceSfx?.play(kind); } catch { /* audio is optional */ } }
$('#sfx-toggle').addEventListener('click', () => { try { window.FaceSfx?.toggle(); } catch { /* optional */ } });
document.addEventListener('DOMContentLoaded', () => { if (!window.FaceSfx) { const b = $('#sfx-toggle'); b.disabled = true; b.title = 'Sound unavailable'; } });

// ---------- tabs with sliding pill (idea 8) ----------
function movePill() {
  const a = $('.tab.active'), pill = $('.tab-pill'); if (!a || !pill) return;
  pill.style.width = a.offsetWidth + 'px'; pill.style.transform = `translateX(${a.offsetLeft}px)`;
}
function show(view) {
  if (!['identify', 'enroll', 'settings'].includes(view)) view = 'identify';
  $$('.tab').forEach((t) => { const on = t.dataset.view === view; t.classList.toggle('active', on); t.setAttribute('aria-selected', on); });
  $$('.view').forEach((v) => (v.hidden = v.id !== 'view-' + view));
  movePill();
  if (view !== 'identify' && typeof stageStream !== 'undefined' && stageStream) stopStageCamera();
  if (view === 'identify') requestAnimationFrame(drawIdleMesh);
  if (view === 'enroll') loadPeople();
  if (view === 'settings') loadSettings();
  history.replaceState(null, '', '#' + view);
}
$$('.tab').forEach((t) => t.addEventListener('click', () => show(t.dataset.view)));
addEventListener('resize', movePill);
document.fonts?.ready.then(movePill);

// ---------- threshold ----------
let threshold = 0.45, defaultThreshold = 0.45, margin = 0.05, defaultMargin = 0.05;
function setThr(v) {
  threshold = +(+v).toFixed(2);
  $('#thr').value = threshold; $('#thr-val').textContent = threshold.toFixed(2);
  $('#thr2').value = threshold; $('#thr2-val').textContent = threshold.toFixed(2);
}
let rerunTimer;
$('#thr').addEventListener('input', (e) => {
  setThr(e.target.value);
  clearTimeout(rerunTimer); rerunTimer = setTimeout(() => lastFiles.length && runIdentify(lastFiles, { quiet: true }), 350);
});
$('#thr2').addEventListener('input', (e) => setThr(e.target.value));
function setMargin(v) { margin = +(+v).toFixed(2); $('#margin').value = margin; $('#margin-val').textContent = margin.toFixed(2); }
$('#margin').addEventListener('input', (e) => setMargin(e.target.value));
$('#save-thr').addEventListener('click', async () => {
  await api('/api/settings', { method: 'POST', body: form({ threshold, margin }) }); toast('Defaults saved');
});
$('#reset-thr').addEventListener('click', () => { setThr(defaultThreshold); setMargin(defaultMargin); });

// ---------- identify ----------
let lastFiles = [];
const drop = $('#id-drop'), idInput = $('#id-files');
idInput.addEventListener('change', () => { lastFiles = [...idInput.files]; runIdentify(lastFiles); idInput.value = ''; });
['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); if (live.dataset.mode !== 'live') drop.classList.add('over'); }));
drop.addEventListener('click', (e) => { if (live.dataset.mode !== 'idle' && live.dataset.mode !== 'result') e.preventDefault(); });
$('#pick-files').addEventListener('click', () => idInput.click());
$('#pick-files').addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); idInput.click(); } });
['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (e) => {
  const files = [...e.dataTransfer.files].filter((f) => f.type.startsWith('image/'));
  if (files.length) { lastFiles = files; runIdentify(files); }
});
$('#id-cam').addEventListener('click', () => startStageCamera());
$('#stage-stop').addEventListener('click', () => stopStageCamera());
$('#stage-snap').addEventListener('click', async () => {
  const file = await snapVideo($('#stage-video'), drop); if (!file) return;
  stopStageCamera(true); lastFiles = [file]; runIdentify(lastFiles);
});

function loadImg(file) {
  return new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = URL.createObjectURL(file); });
}
const COLORS = { known: '#2fd18b', unknown: '#ff5d6c', ambiguous: '#ffb547' }; // status only
const CYAN = '#38bdf8', TEAL = '#2dd4bf';
const statusOf = (f) => (f.known ? 'known' : f.ambiguous ? 'ambiguous' : 'unknown');

// detect -> align -> embed -> match strip (idea 12)
const STAGES = ['detect', 'align', 'embed', 'match'];
function pipelineHTML(stages, total, active = -1) {
  const parts = STAGES.map((s, i) => {
    const cls = active >= 0 ? (i < active ? 'done' : i === active ? 'on' : '') : 'done';
    const t = stages && stages[s] != null ? ` <b>${(+stages[s]).toFixed(stages[s] < 10 ? 1 : 0)}ms</b>` : '';
    return `<span class="stg ${cls}">${s}${t}</span>`;
  });
  return parts.join('<span class="link"></span>') + (total != null ? `<span class="total">Σ ${total} ms</span>` : '');
}
let scanTicker = null;
const live = $('#live');
function setMode(m) { live.dataset.mode = m; }
function startScan(files) {
  setMode('scanning'); holo('setState', 'scanning');
  setVerdict('scanning', 'Scanning…', `${files.length} image${files.length > 1 ? 's' : ''} · detect → align → embed → match`);
  setConfidence(null);
  const pl = $('#id-pipeline'); pl.hidden = false; let k = 0;
  pl.innerHTML = pipelineHTML(null, null, 0);
  clearInterval(scanTicker); scanTicker = setInterval(() => { k = (k + 1) % STAGES.length; pl.innerHTML = pipelineHTML(null, null, k); }, 420);
}
function stopScan(mode = 'result') { clearInterval(scanTicker); setMode(mode); }

// ---------- hologram (Swarm's hologram.js; every call is optional so the app works without it) ----------
let peopleCache = null;
function holo(fn, ...args) { try { return window.FaceHologram?.[fn]?.(...args); } catch (e) { console.warn('FaceHologram.' + fn, e); } }
function mountHologram() {
  if (!window.FaceHologram || mountHologram.done) return;
  mountHologram.done = true;
  window.FaceHologram?.mount(document.getElementById('hologram-stage'));
  document.body.classList.add('holo-ready');
  holo('setState', 'idle');
  if (peopleCache) holo('setPeople', peopleCache.map(holoPerson));
}
const holoPerson = (p) => ({ id: p.id, name: p.name, thumbUrl: p.photos.length ? `/api/photos/${p.photos[0].id}/thumb` : null });
function setPeopleCache(people) { peopleCache = people; holo('setPeople', people.map(holoPerson)); }

// ---------- verdict + confidence readouts beside the stage (frameless) ----------
function setVerdict(state, title, sub) {
  const v = $('#status-card'); v.dataset.state = state;
  $('#status-title').textContent = title; $('#status-sub').innerHTML = sub || '';
  v.classList.remove('pop'); void v.offsetWidth; if (['known', 'unknown', 'ambiguous'].includes(state)) v.classList.add('pop');
}
const ARC_N = 34, ARC_R = 48, ARC_C = [60, 62], ARC_A0 = 135, ARC_SPAN = 270;
function buildArc() {
  const svg = $('#arc'); if (!svg || svg.childElementCount) return;
  const lerp = (a, b, t) => Math.round(a + (b - a) * t);
  const c0 = [0x38, 0xbd, 0xf8], c1 = [0x2d, 0xd4, 0xbf]; // #38bdf8 -> #2dd4bf
  let h = '';
  for (let i = 0; i < ARC_N; i++) {
    const a1 = (ARC_A0 + (i / ARC_N) * ARC_SPAN + 1.2) * Math.PI / 180, a2 = (ARC_A0 + ((i + 1) / ARC_N) * ARC_SPAN - 1.2) * Math.PI / 180;
    const p = (a) => `${(ARC_C[0] + ARC_R * Math.cos(a)).toFixed(2)} ${(ARC_C[1] + ARC_R * Math.sin(a)).toFixed(2)}`;
    const t = i / (ARC_N - 1), col = `rgb(${lerp(c0[0], c1[0], t)},${lerp(c0[1], c1[1], t)},${lerp(c0[2], c1[2], t)})`;
    h += `<path class="seg" style="--segc:${col};transition-delay:${i * 18}ms" d="M ${p(a1)} A ${ARC_R} ${ARC_R} 0 0 1 ${p(a2)}"/>`;
  }
  svg.innerHTML = h + '<line class="thr" id="arc-thr" x1="0" y1="0" x2="0" y2="0"><title>match threshold</title></line>';
}
function setConfidence(value, state = 'known', sub = '') {
  buildArc();
  const blk = $('#conf-block'); blk.dataset.state = value == null ? 'idle' : state;
  $('#conf-val').textContent = value == null ? '—' : Math.min(99.9, value * 100).toFixed(1) + '%'; // the sigmoid never truly reaches 100
  $('#conf-sub').textContent = sub;
  const lit = value == null ? 0 : Math.round(clamp(value, 0, 1) * ARC_N);
  $$('#arc .seg').forEach((el, i) => el.classList.toggle('on', i < lit));
  // threshold tick: the confidence curve is exactly 50% at the threshold
  const a = (ARC_A0 + 0.5 * ARC_SPAN) * Math.PI / 180, t = $('#arc-thr');
  t.setAttribute('x1', ARC_C[0] + (ARC_R - 8) * Math.cos(a)); t.setAttribute('y1', ARC_C[1] + (ARC_R - 8) * Math.sin(a));
  t.setAttribute('x2', ARC_C[0] + (ARC_R + 8) * Math.cos(a)); t.setAttribute('y2', ARC_C[1] + (ARC_R + 8) * Math.sin(a));
}
function resetLive() {
  setMode('idle'); holo('setState', 'idle');
  setVerdict('idle', 'Awaiting Scan', 'Drop a photo or start the camera');
  setConfidence(null);
}

// idle fallback: thin cyan wireframe head + projector rings, hidden once hologram.js mounts
function drawIdleMesh() {
  const cv = $('#idle-mesh'); if (!cv || document.body.classList.contains('holo-ready')) return;
  const r = cv.getBoundingClientRect(); if (!r.width) return;
  const dpr = devicePixelRatio || 1; cv.width = r.width * dpr; cv.height = r.height * dpr;
  const ctx = cv.getContext('2d'); ctx.scale(dpr, dpr);
  const cx = r.width / 2, cy = r.height * 0.42, rx = Math.min(r.width * 0.2, r.height * 0.22), ry = rx * 1.32;
  ctx.strokeStyle = 'rgba(56,189,248,.28)'; ctx.lineWidth = 0.8; ctx.fillStyle = 'rgba(160,240,255,.85)';
  ctx.shadowColor = CYAN; ctx.shadowBlur = 6;
  const pts = [];
  for (let i = 0; i <= 12; i++) {
    const v = -1 + (2 * i) / 12, row = [];
    for (let j = 0; j <= 14; j++) {
      const u = -1 + (2 * j) / 14, w = Math.sqrt(Math.max(0, 1 - v * v)) * (v > 0.3 ? 1 - (v - 0.3) * 0.55 : 1);
      row.push([cx + u * rx * w, cy + v * ry]);
    }
    pts.push(row);
  }
  ctx.beginPath();
  pts.forEach((row, i) => row.forEach(([x, y], j) => {
    if (j) { ctx.moveTo(row[j - 1][0], row[j - 1][1]); ctx.lineTo(x, y); }
    if (i) { ctx.moveTo(pts[i - 1][j][0], pts[i - 1][j][1]); ctx.lineTo(x, y); }
    if (i && j) { ctx.moveTo(pts[i - 1][j - 1][0], pts[i - 1][j - 1][1]); ctx.lineTo(x, y); }
  }));
  ctx.stroke();
  pts.flat().forEach(([x, y]) => { ctx.beginPath(); ctx.arc(x, y, 1.1, 0, Math.PI * 2); ctx.fill(); });
  ctx.shadowColor = TEAL; ctx.shadowBlur = 12; const by = cy + ry + r.height * 0.14;
  const g = ctx.createLinearGradient(0, cy + ry * 0.7, 0, by); g.addColorStop(0, 'rgba(56,189,248,0)'); g.addColorStop(1, 'rgba(56,189,248,.16)');
  ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(cx - rx * 0.45, cy + ry * 0.7); ctx.lineTo(cx + rx * 0.45, cy + ry * 0.7); ctx.lineTo(cx + rx * 0.95, by); ctx.lineTo(cx - rx * 0.95, by); ctx.fill();
  [[1, .45], [.72, .35], [.45, .7], [.22, .95]].forEach(([k, a]) => { ctx.strokeStyle = `rgba(45,212,191,${a})`; ctx.lineWidth = 1.4; ctx.beginPath(); ctx.ellipse(cx, by, rx * 1.9 * k, rx * 0.32 * k, 0, 0, Math.PI * 2); ctx.stroke(); });
}
addEventListener('resize', () => { clearTimeout(drawIdleMesh._t); drawIdleMesh._t = setTimeout(drawIdleMesh, 150); });

// ---------- live camera on the stage (idea 6) ----------
let stageStream = null;
function cameraError(e) {
  if (!navigator.mediaDevices?.getUserMedia) return `Camera isn't available here.<br><span class="small">Browsers only allow the camera on <b>https://</b> or <b>http://localhost</b>. Upload a photo instead.</span>`;
  const n = e?.name;
  if (n === 'NotAllowedError' || n === 'SecurityError') return 'Camera permission was denied.<br><span class="small">Allow camera access in the browser’s site settings, or upload a photo instead.</span>';
  if (n === 'NotFoundError' || n === 'OverconstrainedError') return 'No camera was found on this device.<br><span class="small">Upload a photo instead.</span>';
  if (n === 'NotReadableError') return 'The camera is busy in another app.<br><span class="small">Close it there, or upload a photo instead.</span>';
  return `Couldn't start the camera (${esc(n || e?.message || 'unknown error')}).<br><span class="small">Upload a photo instead.</span>`;
}
function stageMessage(html, ms = 7000) {
  const m = $('#stage-msg'); m.innerHTML = html; m.hidden = false;
  clearTimeout(stageMessage._t); stageMessage._t = setTimeout(() => (m.hidden = true), ms);
}
async function startStageCamera() {
  if (!navigator.mediaDevices?.getUserMedia) { stageMessage(cameraError()); return; }
  try {
    stageStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user', width: { ideal: 1280 }, height: { ideal: 960 } }, audio: false });
  } catch (e) { stageMessage(cameraError(e)); return; }
  const v = $('#stage-video'); v.srcObject = stageStream; await v.play().catch(() => {});
  $('#stage-msg').hidden = true;
  setMode('live'); holo('setState', 'scanning');
  setVerdict('scanning', 'Camera Live', 'Align your face in the oval, then capture');
  setConfidence(null);
  $('#id-cam').hidden = true; $('#pick-files').hidden = true; $('#stage-snap').hidden = false; $('#stage-stop').hidden = false; $('#stage-snap').focus();
}
function stopStageCamera(keep = false) {
  stageStream?.getTracks().forEach((t) => t.stop()); stageStream = null; $('#stage-video').srcObject = null;
  $('#id-cam').hidden = false; $('#pick-files').hidden = false; $('#stage-snap').hidden = true; $('#stage-stop').hidden = true;
  if (!keep) resetLive();
}
// grab a still from a <video> as a JPEG File (unmirrored), with a flash on `flashEl`
function snapVideo(video, flashEl) {
  return new Promise((resolve) => {
    if (!video.videoWidth) { toast('Camera not ready yet'); resolve(null); return; }
    const c = document.createElement('canvas'); c.width = video.videoWidth; c.height = video.videoHeight;
    c.getContext('2d').drawImage(video, 0, 0);
    if (flashEl) { flashEl.classList.remove('flash'); void flashEl.offsetWidth; flashEl.classList.add('flash'); }
    c.toBlob((blob) => resolve(blob ? new File([blob], `camera-${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`, { type: 'image/jpeg' }) : null), 'image/jpeg', 0.92);
  });
}

let runSeq = 0;
async function runIdentify(files, { quiet = false } = {}) {
  const seq = ++runSeq;
  const st = $('#id-status');
  st.innerHTML = `<span class="spinner"></span>SCANNING ${files.length} IMAGE${files.length > 1 ? 'S' : ''} · detect → align → embed → match…`;
  startScan(files);
  const fd = new FormData(); files.forEach((f) => fd.append('files', f)); fd.append('threshold', threshold); fd.append('margin', margin);
  let data;
  const minShow = reducedMotion() || quiet ? 0 : 700; // let the beam sweep at least once
  try { [data] = await Promise.all([api('/api/identify', { method: 'POST', body: fd }), sleep(minShow)]); }
  catch (e) { if (seq === runSeq) { stopScan('idle'); holo('setState', 'idle'); $('#id-pipeline').hidden = true; st.textContent = 'Error: ' + e.message; setVerdict('none', 'Scan Failed', esc(e.message)); } return; }
  if (seq !== runSeq) return; // a newer run superseded this one
  stopScan('result');
  showVerdict(data);
  const faces = data.results.flatMap((r) => r.faces);
  const known = faces.filter((f) => f.known).length;
  st.innerHTML = `<span class="k">${faces.length}</span> face${faces.length === 1 ? '' : 's'} detected · <span class="k">${known}</span> recognized · threshold <span class="k">${data.threshold.toFixed(2)}</span> · margin <span class="k">${data.margin.toFixed(2)}</span>` +
    (data.enrolled_people ? '' : ' · nobody enrolled yet — go to Enroll first');
  // aggregate stage timings over all images
  const agg = {}; let totalMs = 0;
  for (const r of data.results) { totalMs += r.ms || 0; for (const s of STAGES) if (r.stages?.[s] != null) agg[s] = (agg[s] || 0) + r.stages[s]; }
  $('#id-pipeline').innerHTML = pipelineHTML(Object.keys(agg).length ? agg : null, totalMs);
  // audio cues
  if (!quiet) {
    sfx('scan');
    const cue = faces.some((f) => f.ambiguous) ? 'ambiguous' : known ? 'match' : faces.length ? 'unknown' : null;
    if (cue) setTimeout(() => sfx(cue), 220);
  }
  const wrap = $('#id-results'); wrap.innerHTML = '';
  for (let k = 0; k < data.results.length; k++) {
    const r = data.results[k];
    const card = document.createElement('div'); card.className = 'card result'; card.style.setProperty('--i', k);
    wrap.appendChild(card);
    if (r.error) { card.innerHTML = `<div><h3>${esc(r.filename)}</h3><p class="error">${esc(r.error)}</p></div>`; continue; }
    const img = await loadImg(files[k]);
    const cv = document.createElement('canvas'); cv.setAttribute('role', 'img');
    cv.setAttribute('aria-label', `${r.filename}: ` + (r.faces.map((f, i) => `${i + 1} ${f.known ? f.name : f.ambiguous ? 'ambiguous' : 'unknown'}`).join(', ') || 'no faces'));
    drawResult(cv, img, r, { delay: k * 110 });
    const left = document.createElement('div'); left.className = 'canvas-wrap'; left.appendChild(cv);
    const right = document.createElement('div');
    right.innerHTML = `<h3>${esc(r.filename)}</h3><div class="muted small mono">${r.faces.length} face(s) · ${r.width}×${r.height}px · ${r.ms} ms</div>
      <div class="pipeline">${pipelineHTML(r.stages, r.ms)}</div>`;
    const ul = document.createElement('ul'); ul.className = 'face-list';
    r.faces.forEach((f, i) => ul.appendChild(faceItem(f, i, img, r, data, k)));
    if (!r.faces.length) ul.innerHTML = '<li class="muted">No faces detected.</li>';
    right.appendChild(ul);
    card.append(left, right);
    enableTilt(card);
  }
  // kick the gauge transitions after the elements are in the DOM
  requestAnimationFrame(() => requestAnimationFrame(() => $$('[data-p]', wrap).forEach((el) => {
    if (el.classList.contains('fill')) el.style.width = el.dataset.p + '%'; else el.style.setProperty('--p', el.dataset.p);
  })));
}

// headline verdict beside the stage + hologram update (top face = most confident known face, else ambiguous, else first)
function showVerdict(data) {
  const faces = data.results.flatMap((r) => r.faces.map((f) => ({ ...f, _r: r })));
  const known = faces.filter((f) => f.known);
  holo('setState', 'result');
  if (!faces.length) {
    setVerdict('none', 'No Face Detected', data.enrolled_people ? 'Try a clearer, more frontal photo' : 'Nobody enrolled yet. Add people under Enrolled');
    setConfidence(null);
    holo('update', { name: null, status: 'unknown', confidence: 0, kps: null });
    return;
  }
  const top = known.length ? known.reduce((a, b) => (b.confidence > a.confidence ? b : a)) : faces.find((f) => f.ambiguous) || faces[0];
  const st = statusOf(top);
  const conf = top.known ? top.confidence : top.ambiguous ? 0.5 : 1 - top.confidence; // always "match confidence"
  const extra = faces.length > 1 ? `<br>${known.length} of ${faces.length} faces recognized` : '';
  const names = [...new Set(known.map((f) => f.name))];
  if (st === 'known') setVerdict('known', names.length > 1 ? 'Faces Recognized' : 'Face Recognized', `<b>${names.map(esc).join(', ')}</b>${extra}`);
  else if (st === 'ambiguous') setVerdict('ambiguous', 'Too Close to Call', `${esc(top.runner_up)}${extra}`);
  else setVerdict('unknown', 'Unknown Face', `Not enrolled${top.runner_up ? ` · closest ${esc(top.runner_up)} at ${top.score.toFixed(2)}` : ''}${extra}`);
  setConfidence(conf, st, `similarity ${top.score.toFixed(3)} · threshold ${data.threshold.toFixed(2)}`);
  // landmarks normalised to 0..1 of the analysed image, so the hologram can use them at any size
  const kps = top.kps ? top.kps.map(([x, y]) => [+(x / top._r.width).toFixed(4), +(y / top._r.height).toFixed(4)]) : null;
  holo('update', { name: top.known ? top.name : null, status: st, confidence: conf, kps });
}

// one face row: ring gauge (match confidence) + similarity gauge with threshold tick + readout (ideas 5, 10)
function faceItem(f, i, img, r, data, k) {
  const status = statusOf(f);
  const li = document.createElement('li'); li.className = 'face-item' + (status === 'known' ? '' : ' ' + status);
  li.style.setProperty('--i', k); li.style.setProperty('--j', i);
  const th = document.createElement('canvas'); cropFace(th, img, r, f.bbox);
  const thr = data.threshold, mrg = data.margin;
  // f.confidence is "how sure of the verdict"; for unknown faces that is certainty it's NOT the closest person.
  // Show a single consistent number everywhere: the match confidence for the best candidate.
  const matchConf = f.known ? f.confidence : f.ambiguous ? 0.5 : 1 - f.confidence;
  const pct = Math.round(matchConf * 100);
  const title = f.known ? esc(f.name) : f.ambiguous ? 'Ambiguous' : 'Unknown';
  const verdict = f.known ? 'MATCH' : f.ambiguous ? 'AMBIGUOUS' : 'NO MATCH';
  let sub;
  if (f.known) sub = `match confidence ${pct}%`;
  else if (f.ambiguous) sub = `too close to call: ${esc(f.runner_up)}`;
  else if (f.runner_up) sub = `not enrolled · closest is ${esc(f.runner_up)} at ${f.score.toFixed(2)}, below the ${thr.toFixed(2)} threshold`;
  else sub = 'not enrolled · nobody to compare against';
  const pos = (s) => clamp(s, 0, 1) * 100;
  // second-best: for known faces it's runner_up_score; for unknown faces runner_up IS the best (same score)
  const ruScore = f.known ? f.runner_up_score : null;
  const delta = ruScore != null ? f.score - ruScore : null;
  const passCls = f.score >= thr ? 'pass' : 'fail';
  const marginCell = f.ambiguous ? `<span class="amb">Δ&lt;${mrg.toFixed(2)}</span>`
    : delta != null ? `<span class="${delta >= mrg ? 'pass' : 'amb'}">${delta.toFixed(3)}</span>` : '<b>—</b>';
  li.appendChild(th);
  const info = document.createElement('div'); info.className = 'face-top';
  info.innerHTML = `<div class="who"><div><span class="nm">#${i + 1} ${title}</span><span class="verdict">${verdict}</span></div>
      <div class="sub">${sub}</div></div>
    <div class="ring-wrap" title="Match confidence"><div class="ring" data-p="${pct}"></div><span>${pct}%</span></div>`;
  li.appendChild(info);
  const g = document.createElement('div'); g.className = 'gauge';
  g.title = `similarity ${f.score.toFixed(3)} vs threshold ${thr.toFixed(2)}`;
  g.innerHTML = `<div class="fill" data-p="${pos(f.score).toFixed(1)}"></div>` +
    (ruScore != null ? `<div class="ru" style="left:${pos(ruScore)}%" title="runner-up ${esc(f.runner_up)} ${ruScore.toFixed(3)}"></div>` : '') +
    `<div class="tick" style="left:${pos(thr)}%" data-label="THR ${thr.toFixed(2)}"></div>`;
  li.appendChild(g);
  const ro = document.createElement('div'); ro.className = 'readout';
  ro.innerHTML = `<span>sim <span class="${passCls}">${f.score.toFixed(3)}</span></span>
    <span>nn <b>${(f.nn_score ?? f.score).toFixed(3)}</b></span>
    <span>centroid <b>${f.centroid_score != null ? f.centroid_score.toFixed(3) : '—'}</b></span>
    <span>thr <b>${thr.toFixed(2)}</b></span>
    <span>2nd <b>${f.known && f.runner_up ? esc(f.runner_up) + ' ' + ruScore.toFixed(3) : f.ambiguous ? 'tied' : '—'}</b></span>
    <span>Δ ${marginCell}</span>
    <span>margin <b>${mrg.toFixed(2)}</b></span>
    <span>det <b>${(f.det_score ?? 0).toFixed(2)}</b></span>`;
  li.appendChild(ro);
  return li;
}

// corner-bracket frames, glowing 5-point landmarks, floating chips that never clip (ideas 2, 3)
function drawResult(cv, img, r, { delay = 0 } = {}) {
  const scale = img.naturalWidth / r.width; // server may downscale large images
  const W = (cv.width = img.naturalWidth), H = (cv.height = img.naturalHeight);
  const ctx = cv.getContext('2d');
  const u = Math.max(W, H) / 900; // unit relative to image size
  const lw = Math.max(1.5, 2.2 * u), fs = Math.max(12, Math.round(15 * u));
  ctx.font = `600 ${fs}px Inter, system-ui, sans-serif`;
  const faces = r.faces.map((f, i) => {
    const [x1, y1, x2, y2] = f.bbox.map((v) => v * scale);
    const col = COLORS[statusOf(f)];
    const pct = Math.round((f.known ? f.confidence : f.ambiguous ? 0.5 : 1 - f.confidence) * 100);
    const label = `${i + 1} · ${f.known ? f.name : f.ambiguous ? '? ' + f.runner_up : 'UNKNOWN'}`;
    const meta = f.known ? `${pct}%` : f.ambiguous ? 'AMBIG' : `${f.score.toFixed(2)}`;
    return { f, i, x1, y1, x2, y2, col, label, meta, kps: (f.kps || []).map(([x, y]) => [x * scale, y * scale]) };
  });
  // lay out chips: try above, below, inside-top; clamp inside canvas; avoid overlaps
  const placed = [];
  const overlaps = (a) => placed.some((b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h);
  for (const o of faces) {
    let f2 = fs;
    ctx.font = `600 ${f2}px Inter, system-ui, sans-serif`;
    const mono = () => `600 ${Math.round(f2 * 0.85)}px "IBM Plex Mono", ui-monospace, monospace`;
    const measure = () => { ctx.font = `600 ${f2}px Inter, system-ui, sans-serif`; const a = ctx.measureText(o.label).width; ctx.font = mono(); const b = ctx.measureText(o.meta).width; return { a, b, w: a + b + f2 * 1.6 }; };
    let m = measure();
    while (m.w > W - 4 && f2 > 8) { f2 -= 1; m = measure(); }
    let label = o.label;
    if (m.w > W - 4) { // still too wide: truncate with ellipsis rather than clip
      ctx.font = `600 ${f2}px Inter, system-ui, sans-serif`;
      while (label.length > 3 && ctx.measureText(label + '…').width + m.b + f2 * 1.6 > W - 4) label = label.slice(0, -1);
      label += '…'; o.label = label; m = measure();
    }
    const w = m.w, h = f2 * 1.7, gap = 6 * u;
    const x = clamp(o.x1, 2, W - w - 2);
    const cands = [o.y1 - h - gap, o.y2 + gap, o.y1 + gap];
    let y = null;
    for (const cy of cands) { const c = { x, y: clamp(cy, 2, H - h - 2), w, h }; if (!overlaps(c)) { y = c.y; break; } }
    if (y == null) { y = clamp(cands[0], 2, H - h - 2); while (overlaps({ x, y, w, h }) && y + h < H - 2) y += h + 2; y = clamp(y, 2, H - h - 2); }
    o.chip = { x, y, w, h, fs: f2, la: m.a }; placed.push(o.chip);
  }
  const rr = (x, y, w, h, rad) => { ctx.beginPath(); ctx.moveTo(x + rad, y); ctx.arcTo(x + w, y, x + w, y + h, rad); ctx.arcTo(x + w, y + h, x, y + h, rad); ctx.arcTo(x, y + h, x, y, rad); ctx.arcTo(x, y, x + w, y, rad); ctx.closePath(); };
  function frame(t) { // t: 0..1 animation progress
    ctx.clearRect(0, 0, W, H); ctx.drawImage(img, 0, 0);
    for (const o of faces) {
      const e = 1 - Math.pow(1 - clamp(t * 1.25 - o.i * 0.05, 0, 1), 3);
      if (e <= 0) continue;
      const { x1, y1, x2, y2, col } = o, bw = x2 - x1, bh = y2 - y1;
      const L = Math.min(bw, bh) * 0.24 * e;
      ctx.save();
      ctx.strokeStyle = col; ctx.lineWidth = lw; ctx.lineCap = 'round'; ctx.shadowColor = col; ctx.shadowBlur = 10 * u;
      // faint full box
      ctx.globalAlpha = 0.22 * e; ctx.strokeRect(x1, y1, bw, bh); ctx.globalAlpha = 1;
      ctx.beginPath();
      ctx.moveTo(x1, y1 + L); ctx.lineTo(x1, y1); ctx.lineTo(x1 + L, y1);
      ctx.moveTo(x2 - L, y1); ctx.lineTo(x2, y1); ctx.lineTo(x2, y1 + L);
      ctx.moveTo(x2, y2 - L); ctx.lineTo(x2, y2); ctx.lineTo(x2 - L, y2);
      ctx.moveTo(x1 + L, y2); ctx.lineTo(x1, y2); ctx.lineTo(x1, y2 - L);
      ctx.stroke();
      // landmarks: eyes, nose, mouth corners + mesh lines
      if (o.kps.length === 5) {
        const [le, re, no, lm, rmo] = o.kps;
        ctx.globalAlpha = 0.6 * e; ctx.lineWidth = Math.max(1, lw * 0.5); ctx.strokeStyle = CYAN; ctx.shadowColor = CYAN;
        ctx.beginPath();
        [[le, re], [le, no], [re, no], [no, lm], [no, rmo], [lm, rmo]].forEach(([a, b]) => { ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); });
        ctx.stroke();
        ctx.globalAlpha = e; ctx.fillStyle = '#eaffff'; ctx.shadowBlur = 14 * u;
        const pr = Math.max(2, Math.min(bw, bh) * 0.025);
        for (const [x, y] of o.kps) { ctx.beginPath(); ctx.arc(x, y, pr * (0.6 + 0.4 * e), 0, Math.PI * 2); ctx.fill(); }
        ctx.strokeStyle = TEAL; ctx.lineWidth = Math.max(1, lw * 0.6);
        for (const [x, y] of o.kps) { ctx.beginPath(); ctx.arc(x, y, pr * 2.1, 0, Math.PI * 2); ctx.stroke(); }
      }
      ctx.restore();
      // chip + leader line
      const c = o.chip; ctx.save(); ctx.globalAlpha = e;
      const anchorY = c.y + c.h <= y1 ? y1 : c.y >= y2 ? y2 : c.y + c.h / 2;
      ctx.strokeStyle = col; ctx.lineWidth = Math.max(1, lw * 0.6); ctx.beginPath();
      ctx.moveTo(clamp(c.x + 10 * u, x1, x2), c.y + c.h <= y1 ? c.y + c.h : c.y >= y2 ? c.y : anchorY); ctx.lineTo(clamp(c.x + 10 * u, x1, x2), anchorY); ctx.stroke();
      ctx.fillStyle = 'rgba(4,8,16,.82)'; ctx.shadowColor = col; ctx.shadowBlur = 12 * u; rr(c.x, c.y, c.w, c.h, c.h * 0.28); ctx.fill(); ctx.shadowBlur = 0;
      ctx.strokeStyle = col; ctx.lineWidth = Math.max(1, lw * 0.5); ctx.stroke();
      ctx.fillStyle = col; ctx.fillRect(c.x, c.y + c.h * 0.2, Math.max(2, 3 * u), c.h * 0.6);
      ctx.textBaseline = 'middle';
      ctx.font = `600 ${c.fs}px Inter, system-ui, sans-serif`; ctx.fillStyle = '#eaf6ff'; ctx.fillText(o.label, c.x + c.fs * 0.6, c.y + c.h / 2);
      ctx.font = `600 ${Math.round(c.fs * 0.85)}px "IBM Plex Mono", ui-monospace, monospace`; ctx.fillStyle = col;
      ctx.fillText(o.meta, c.x + c.fs * 1.0 + c.la, c.y + c.h / 2 + 0.5);
      ctx.restore();
    }
  }
  if (reducedMotion()) { frame(1); return; }
  frame(0);
  const dur = 750; let t0 = null;
  setTimeout(() => requestAnimationFrame(function step(ts) { t0 ??= ts; const t = clamp((ts - t0) / dur, 0, 1); frame(t); if (t < 1) requestAnimationFrame(step); }), delay + 200);
}
function cropFace(cv, img, r, bbox) {
  const s = img.naturalWidth / r.width; const [x1, y1, x2, y2] = bbox.map((v) => v * s);
  const side = Math.max(x2 - x1, y2 - y1) * 1.3, cx = (x1 + x2) / 2, cy = (y1 + y2) / 2;
  cv.width = cv.height = 96;
  cv.getContext('2d').drawImage(img, cx - side / 2, cy - side / 2, side, side, 0, 0, 96, 96);
}

// subtle 3D tilt (idea 12)
function enableTilt(card) {
  if (reducedMotion() || !matchMedia('(hover: hover) and (pointer: fine)').matches) return;
  card.addEventListener('pointermove', (e) => {
    const b = card.getBoundingClientRect(); const px = (e.clientX - b.left) / b.width - 0.5, py = (e.clientY - b.top) / b.height - 0.5;
    card.classList.add('tilting'); card.style.setProperty('--ry', (px * 4).toFixed(2) + 'deg'); card.style.setProperty('--rx', (-py * 4).toFixed(2) + 'deg');
  });
  card.addEventListener('pointerleave', () => { card.classList.remove('tilting'); card.style.setProperty('--rx', '0deg'); card.style.setProperty('--ry', '0deg'); });
}

// ---------- live camera (idea 6) ----------
let camStream = null, camOnCapture = null;
async function openCamera({ title, onCapture }) {
  const modal = $('#cam-modal'), video = $('#cam-video'), msg = $('#cam-msg');
  $('#cam-title').textContent = title; camOnCapture = onCapture;
  modal.hidden = false; msg.hidden = true; $('#cam-snap').disabled = true; $('#cam-res').textContent = '';
  const fail = (text) => { msg.hidden = false; msg.innerHTML = text; $('#cam-snap').disabled = true; };
  if (!navigator.mediaDevices?.getUserMedia) {
    fail(`Camera isn't available here.<br><span class="small">Browsers only allow the camera on <b>https://</b> or <b>http://localhost</b>. Use photo upload instead.</span>`);
    return;
  }
  try {
    camStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user', width: { ideal: 1280 }, height: { ideal: 960 } }, audio: false });
    if (modal.hidden) { stopCamera(); return; }
    video.srcObject = camStream; await video.play().catch(() => {});
    $('#cam-snap').disabled = false; $('#cam-snap').focus();
    const upd = () => { if (video.videoWidth) $('#cam-res').textContent = `${video.videoWidth}×${video.videoHeight}`; };
    video.onloadedmetadata = upd; upd();
  } catch (e) {
    const denied = e.name === 'NotAllowedError' || e.name === 'SecurityError';
    fail(denied ? 'Camera permission was denied.<br><span class="small">Allow camera access in the browser’s site settings, or upload a photo instead.</span>'
      : e.name === 'NotFoundError' ? 'No camera was found on this device.<br><span class="small">Upload a photo instead.</span>'
      : `Couldn't start the camera (${esc(e.name || e.message)}).<br><span class="small">Upload a photo instead.</span>`);
  }
}
function stopCamera() {
  camStream?.getTracks().forEach((t) => t.stop()); camStream = null;
  $('#cam-video').srcObject = null; $('#cam-modal').hidden = true;
}
$('#cam-close').addEventListener('click', stopCamera);
$('#cam-cancel').addEventListener('click', stopCamera);
$('#cam-modal').addEventListener('click', (e) => { if (e.target.id === 'cam-modal') stopCamera(); });
addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#cam-modal').hidden) stopCamera(); });
$('#cam-snap').addEventListener('click', () => {
  const video = $('#cam-video'); if (!video.videoWidth) return;
  const c = document.createElement('canvas'); c.width = video.videoWidth; c.height = video.videoHeight;
  c.getContext('2d').drawImage(video, 0, 0);
  const view = $('#cam-view'); view.classList.remove('flash'); void view.offsetWidth; view.classList.add('flash');
  c.toBlob((blob) => {
    if (!blob) { toast('Capture failed'); return; }
    const file = new File([blob], `camera-${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`, { type: 'image/jpeg' });
    const cb = camOnCapture; setTimeout(() => { stopCamera(); cb?.(file); }, reducedMotion() ? 0 : 250);
  }, 'image/jpeg', 0.92);
});

// ---------- enroll ----------
$('#new-person').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = e.target.name.value.trim(); if (!name) return;
  try { await api('/api/people', { method: 'POST', body: form({ name }) }); e.target.reset(); toast(`Added ${name}. Now add photos.`); loadPeople(); }
  catch (err) { toast(err.message); }
});

async function loadPeople() {
  const data = await api('/api/people');
  setPeopleCache(data.people);
  const grid = $('#people'); grid.innerHTML = '';
  $('#people-empty').hidden = data.people.length > 0;
  data.people.forEach((p, i) => { const el = personCard(p); el.style.setProperty('--i', i); grid.appendChild(el); });
  animateMeters(grid);
}
function animateMeters(root) {
  requestAnimationFrame(() => requestAnimationFrame(() => $$('[data-p]', root).forEach((el) => {
    if (el.tagName === 'I') el.style.width = el.dataset.p + '%'; else el.style.setProperty('--p', el.dataset.p);
  })));
}

// quality grading, matching the server's enrollment gates (MIN_BLUR 25 / WARN 60, MIN_FACE 64px, MIN_DET 0.6, yaw > 0.45 rejected)
const Q = {
  blur: { label: 'Sharpness', norm: (v) => clamp(v / 200, 0, 1), grade: (v) => (v >= 60 ? 'good' : v >= 25 ? 'fair' : 'poor'), fmt: (v) => v.toFixed(0) },
  face_size: { label: 'Face size', norm: (v) => clamp(v / 220, 0, 1), grade: (v) => (v >= 112 ? 'good' : v >= 64 ? 'fair' : 'poor'), fmt: (v) => v + 'px' },
  det_score: { label: 'Detection', norm: (v) => clamp(v, 0, 1), grade: (v) => (v >= 0.8 ? 'good' : v >= 0.6 ? 'fair' : 'poor'), fmt: (v) => v.toFixed(2) },
  yaw: { label: 'Pose', norm: (v) => clamp(1 - v / 0.5, 0, 1), grade: (v) => (v <= 0.15 ? 'good' : v <= 0.3 ? 'fair' : 'poor'), fmt: (v) => (v <= 0.15 ? 'frontal' : v.toFixed(2)) },
};
function photoGrade(q) {
  const g = Object.keys(Q).filter((k) => q?.[k] != null).map((k) => Q[k].grade(q[k]));
  return g.includes('poor') ? 'poor' : g.includes('fair') ? 'fair' : 'good';
}
function qualityMeters(photos) {
  if (!photos.length) return '<span>QUALITY</span><span class="qb"></span><span class="v na">no data</span>';
  return Object.entries(Q).map(([k, d]) => {
    const vals = photos.map((p) => p.quality?.[k]).filter((v) => v != null);
    if (!vals.length) return `<span>${d.label}</span><span class="qb"></span><span class="v na">n/a</span>`;
    const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
    const g = d.grade(avg);
    return `<span>${d.label}</span><span class="qb"><i class="${g}" data-p="${(d.norm(avg) * 100).toFixed(0)}"></i></span><span class="v ${g}" title="average over ${vals.length} photo(s)">${d.fmt(k === 'face_size' ? Math.round(avg) : avg)}</span>`;
  }).join('');
}

function personCard(p) {
  const el = $('#person-tpl').content.firstElementChild.cloneNode(true);
  const n = p.photos.length;
  $('.pname', el).textContent = p.name;
  $('.pcount', el).textContent = `${n} photo${n === 1 ? '' : 's'}` + (n < 3 ? ` · add ${3 - n} more for best accuracy` : ' · enrollment complete');
  const ring = $('.av-ring', el);
  ring.dataset.p = Math.round(Math.min(n, 3) / 3 * 100); ring.classList.toggle('complete', n >= 3);
  $('.ring-lbl', ring).textContent = `${Math.min(n, 3)}/3`;
  const av = $('.avatar', el);
  if (n) av.style.backgroundImage = `url(/api/photos/${p.photos[0].id}/thumb)`; else av.textContent = p.name[0].toUpperCase();
  $('.qmeters', el).innerHTML = qualityMeters(p.photos);
  const thumbs = $('.thumbs', el);
  for (const ph of p.photos) {
    const q = ph.quality || {};
    const t = document.createElement('div'); t.className = 'thumb';
    t.title = `sharpness ${q.blur} · face ${q.face_size}px · det ${q.det_score}` + (q.yaw != null ? ` · yaw ${q.yaw}` : '');
    t.innerHTML = `<img src="/api/photos/${ph.id}/thumb" alt="${esc(p.name)} reference photo"><span class="qdot ${photoGrade(q)}"></span><button title="Delete photo" aria-label="Delete photo">✕</button>`;
    const im = $('img', t); // retry once if a thumb fails to load (seen intermittently right after enrollment)
    im.addEventListener('error', () => { if (!im.dataset.retry) { im.dataset.retry = 1; setTimeout(() => (im.src = `/api/photos/${ph.id}/thumb?r=${Date.now()}`), 400); } });
    $('button', t).addEventListener('click', async () => {
      if (!confirm('Delete this photo?')) return;
      await api(`/api/photos/${ph.id}`, { method: 'DELETE' }); loadPeople();
    });
    thumbs.appendChild(t);
  }
  $('.del-person', el).addEventListener('click', async () => {
    if (!confirm(`Delete ${p.name} and all their photos?`)) return;
    await api(`/api/people/${p.id}`, { method: 'DELETE' }); toast(`Deleted ${p.name}`); loadPeople();
  });
  const input = $('input[type=file]', el);
  input.addEventListener('change', () => { uploadPhotos(p, el, [...input.files]); input.value = ''; });
  $('.cam-enroll', el).addEventListener('click', () => openCamera({ title: `Enroll · ${p.name}`, onCapture: (file) => uploadPhotos(p, el, [file]) }));
  return el;
}

async function uploadPhotos(p, el, files) {
  if (!files.length) return;
  const log = $('.enroll-log', el);
  const fd = new FormData(); files.forEach((f) => fd.append('files', f));
  fd.append('force', $('.force-cb', el).checked);
  log.innerHTML = `<span><span class="spinner"></span>Checking ${files.length} photo(s)…</span>`;
  el.classList.add('scanning');
  try {
    const res = await api(`/api/people/${p.id}/photos`, { method: 'POST', body: fd });
    const lines = res.results.map((r) => {
      const s = [];
      if (r.accepted) s.push(`<span class="ok">✓ ${esc(r.filename)} added</span>`);
      r.errors.forEach((m) => s.push(`<span class="err">✕ ${esc(r.filename)}: ${esc(m)}</span>`));
      r.warnings.forEach((m) => s.push(`<span class="warn">⚠ ${esc(r.filename)}: ${esc(m)}</span>`));
      return s.join('');
    });
    const all = (await api('/api/people')).people; setPeopleCache(all);
    const fresh = personCard(all.find((x) => x.id === p.id));
    fresh.style.setProperty('--i', 0);
    $('.enroll-log', fresh).innerHTML = lines.join('');
    el.replaceWith(fresh); animateMeters(fresh);
    sfx(res.results.some((r) => r.accepted) ? 'match' : 'unknown');
  } catch (e) { el.classList.remove('scanning'); log.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

// ---------- settings ----------
async function loadSettings() {
  const s = await api('/api/settings');
  defaultThreshold = s.default_threshold; $('#def-thr').textContent = s.default_threshold.toFixed(2);
  defaultMargin = s.default_margin; $('#def-margin').textContent = s.default_margin.toFixed(2);
  $('#stats').textContent = `${s.people} people · ${s.photos} reference photos · password protection ${s.password_protected ? 'on' : 'OFF (set FACEID_PASSWORD)'}`;
  $('#logout').hidden = !s.password_protected;
  return s;
}
$('#wipe').addEventListener('click', async () => {
  const c = $('#wipe-confirm').value;
  if (c !== 'DELETE') { toast('Type DELETE to confirm'); return; }
  await api('/api/wipe', { method: 'POST', body: form({ confirm: c }) });
  $('#wipe-confirm').value = ''; toast('All data deleted'); setThr(defaultThreshold); setMargin(defaultMargin); loadSettings();
});
$('#logout').addEventListener('click', async () => { await api('/api/logout', { method: 'POST' }); location.href = '/login'; });

// ---------- boot ----------
loadSettings().then((s) => { setThr(s.threshold); setMargin(s.margin); });
mountHologram();
resetLive();
show((location.hash || '#identify').slice(1));
api('/api/people').then((d) => setPeopleCache(d.people)).catch(() => {});
// mount Swarm's hologram whenever hologram.js is available (defer/module scripts may load after this file)
mountHologram();
document.addEventListener('DOMContentLoaded', mountHologram);
addEventListener('load', () => { mountHologram(); drawIdleMesh(); });
addEventListener('facehologram:ready', mountHologram);
document.fonts?.ready.then(drawIdleMesh);
