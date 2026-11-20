'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts });
  if (r.status === 401) { location.href = '/login'; throw new Error('auth'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
function toast(msg, ms = 2600) {
  const t = $('#toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}
function form(obj) { const f = new FormData(); for (const [k, v] of Object.entries(obj)) f.append(k, v); return f; }

// ---------- tabs ----------
function show(view) {
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === view));
  $$('.view').forEach((v) => (v.hidden = v.id !== 'view-' + view));
  if (view === 'enroll') loadPeople();
  if (view === 'settings') loadSettings();
  history.replaceState(null, '', '#' + view);
}
$$('.tab').forEach((t) => t.addEventListener('click', () => show(t.dataset.view)));

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
  clearTimeout(rerunTimer); rerunTimer = setTimeout(() => lastFiles.length && runIdentify(lastFiles), 350);
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
['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (e) => {
  const files = [...e.dataTransfer.files].filter((f) => f.type.startsWith('image/'));
  if (files.length) { lastFiles = files; runIdentify(files); }
});

function loadImg(file) {
  return new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = URL.createObjectURL(file); });
}
const COLORS = { known: '#2fd18b', unknown: '#ff5d6c', ambiguous: '#ffb547' };

async function runIdentify(files) {
  const st = $('#id-status');
  st.innerHTML = `<span class="spinner"></span>Analyzing ${files.length} photo${files.length > 1 ? 's' : ''}…`;
  const fd = new FormData(); files.forEach((f) => fd.append('files', f)); fd.append('threshold', threshold); fd.append('margin', margin);
  let data;
  try { data = await api('/api/identify', { method: 'POST', body: fd }); }
  catch (e) { st.textContent = 'Error: ' + e.message; return; }
  const total = data.results.reduce((a, r) => a + r.faces.length, 0);
  const known = data.results.reduce((a, r) => a + r.faces.filter((f) => f.known).length, 0);
  st.textContent = `${total} face${total === 1 ? '' : 's'} found · ${known} recognized · threshold ${data.threshold.toFixed(2)}` +
    (data.enrolled_people ? '' : ' · nobody enrolled yet — go to Enroll first');
  const wrap = $('#id-results'); wrap.innerHTML = '';
  for (let k = 0; k < data.results.length; k++) {
    const r = data.results[k];
    const card = document.createElement('div'); card.className = 'card result';
    wrap.appendChild(card);
    if (r.error) { card.innerHTML = `<div><h3>${esc(r.filename)}</h3><p class="error">${esc(r.error)}</p></div>`; continue; }
    const img = await loadImg(files[k]);
    const cv = document.createElement('canvas'); drawResult(cv, img, r);
    const left = document.createElement('div'); left.className = 'canvas-wrap'; left.appendChild(cv);
    const right = document.createElement('div');
    right.innerHTML = `<h3>${esc(r.filename)}</h3><div class="muted small">${r.faces.length} face(s) · ${r.ms} ms</div>`;
    const ul = document.createElement('ul'); ul.className = 'face-list';
    r.faces.forEach((f, i) => {
      const li = document.createElement('li'); li.className = 'face-item' + (f.known ? '' : f.ambiguous ? ' ambiguous' : ' unknown');
      const th = document.createElement('canvas'); cropFace(th, img, r, f.bbox);
      const pct = Math.round(f.confidence * 100);
      li.appendChild(th);
      const info = document.createElement('div');
      const sub = f.known ? `confidence ${pct}%` : f.ambiguous ? 'too close to call' : `unknown (${pct}% sure)`;
      info.innerHTML = `<div><span class="nm">#${i + 1} ${esc(f.ambiguous ? 'Ambiguous' : f.name)}</span>${f.ambiguous ? `<span class="pill warn">${esc(f.runner_up)}?</span>` : ''}</div>
        <div class="muted small">${sub} · similarity ${f.score.toFixed(3)}${!f.known && !f.ambiguous && f.runner_up ? ` · closest: ${esc(f.runner_up)}` : ''}</div>
        <div class="bar"><i style="width:${pct}%"></i></div>`;
      li.appendChild(info); ul.appendChild(li);
    });
    if (!r.faces.length) ul.innerHTML = '<li class="muted">No faces detected.</li>';
    right.appendChild(ul);
    card.append(left, right);
  }
}

function drawResult(cv, img, r) {
  const scale = img.naturalWidth / r.width; // server may downscale large images
  cv.width = img.naturalWidth; cv.height = img.naturalHeight;
  const ctx = cv.getContext('2d'); ctx.drawImage(img, 0, 0);
  const lw = Math.max(2, Math.round(cv.width / 300)), fs = Math.max(13, Math.round(cv.width / 45));
  r.faces.forEach((f, i) => {
    const [x1, y1, x2, y2] = f.bbox.map((v) => v * scale);
    const col = f.known ? COLORS.known : (f.ambiguous ? COLORS.ambiguous : COLORS.unknown);
    ctx.lineWidth = lw; ctx.strokeStyle = col; ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    const label = `${i + 1}. ${f.ambiguous ? '? ' + f.runner_up : f.name}${f.known ? ' ' + Math.round(f.confidence * 100) + '%' : ''}`;
    ctx.font = `600 ${fs}px Inter, system-ui, sans-serif`;
    const tw = ctx.measureText(label).width + fs * 0.8, th = fs * 1.5;
    let ly = y1 - th; if (ly < 0) ly = y2;
    ctx.fillStyle = col; ctx.fillRect(x1 - lw / 2, ly, tw, th);
    ctx.fillStyle = '#0b0d12'; ctx.fillText(label, x1 + fs * 0.4, ly + fs * 1.1);
  });
}
function cropFace(cv, img, r, bbox) {
  const s = img.naturalWidth / r.width; const [x1, y1, x2, y2] = bbox.map((v) => v * s);
  const side = Math.max(x2 - x1, y2 - y1) * 1.3, cx = (x1 + x2) / 2, cy = (y1 + y2) / 2;
  cv.width = cv.height = 96;
  cv.getContext('2d').drawImage(img, cx - side / 2, cy - side / 2, side, side, 0, 0, 96, 96);
}

// ---------- enroll ----------
$('#new-person').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = e.target.name.value.trim(); if (!name) return;
  try { await api('/api/people', { method: 'POST', body: form({ name }) }); e.target.reset(); toast(`Added ${name}. Now upload photos.`); loadPeople(); }
  catch (err) { toast(err.message); }
});

async function loadPeople() {
  const data = await api('/api/people');
  const grid = $('#people'); grid.innerHTML = '';
  $('#people-empty').hidden = data.people.length > 0;
  for (const p of data.people) grid.appendChild(personCard(p));
}

function personCard(p) {
  const el = $('#person-tpl').content.firstElementChild.cloneNode(true);
  $('.pname', el).textContent = p.name;
  $('.pcount', el).textContent = `${p.photos.length} photo${p.photos.length === 1 ? '' : 's'}` + (p.photos.length < 3 ? ' · add more for best accuracy' : '');
  const av = $('.avatar', el);
  if (p.photos.length) av.style.backgroundImage = `url(/api/photos/${p.photos[0].id}/thumb)`; else av.textContent = p.name[0].toUpperCase();
  const thumbs = $('.thumbs', el);
  for (const ph of p.photos) {
    const t = document.createElement('div'); t.className = 'thumb';
    t.title = `sharpness ${ph.quality.blur} · face ${ph.quality.face_size}px`;
    t.innerHTML = `<img loading="lazy" src="/api/photos/${ph.id}/thumb" alt=""><button title="Delete photo" aria-label="Delete photo">✕</button>`;
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
  const input = $('input[type=file]', el), log = $('.enroll-log', el);
  input.addEventListener('change', async () => {
    const fd = new FormData(); [...input.files].forEach((f) => fd.append('files', f));
    fd.append('force', $('.force-cb', el).checked);
    log.innerHTML = `<span><span class="spinner"></span>Checking ${input.files.length} photo(s)…</span>`;
    try {
      const res = await api(`/api/people/${p.id}/photos`, { method: 'POST', body: fd });
      const lines = res.results.map((r) => {
        const s = [];
        if (r.accepted) s.push(`<span class="ok">✓ ${esc(r.filename)} added</span>`);
        r.errors.forEach((m) => s.push(`<span class="err">✕ ${esc(r.filename)}: ${esc(m)}</span>`));
        r.warnings.forEach((m) => s.push(`<span class="warn">⚠ ${esc(r.filename)}: ${esc(m)}</span>`));
        return s.join('');
      });
      const fresh = personCard((await api('/api/people')).people.find((x) => x.id === p.id));
      $('.enroll-log', fresh).innerHTML = lines.join('');
      el.replaceWith(fresh);
    } catch (e) { log.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  });
  return el;
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
show((location.hash || '#identify').slice(1));
