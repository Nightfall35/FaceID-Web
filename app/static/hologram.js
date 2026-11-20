/* FaceID · 3D hologram stage (no dependencies, Canvas2D + hand-rolled 3D projection).
 *
 * window.FaceHologram = {
 *   mount(stageEl, opts?)   // stageEl e.g. <div id="hologram-stage">; returns FaceHologram
 *   update({ name, status: 'known'|'unknown'|'ambiguous', confidence, kps }) // also accepts null (clear)
 *   setPeople([{ id, name, thumbUrl }])
 *   setState('idle'|'scanning'|'result')       // 'result' shows the status from the last update()
 *   destroy()
 * }
 * confidence: 0..1 (values > 1 are treated as percent). kps: 5 face keypoints [[x,y]x5] in source-image pixels,
 * InsightFace order (left eye, right eye, nose, left mouth, right mouth); used only for relative geometry
 * (yaw/pitch/roll), so the coordinate space / image size doesn't matter.
 * opts: { overlay: 'auto'|true|false  // draw the in-stage floating verdict label + arc gauge.
 *                                      // 'auto' = off when the page already has #verdict-block / #conf-block
 *         maxPeople: 14, background: 'transparent' }
 * Sound is NOT played here (app.js owns FaceSfx).
 */
(() => {
  'use strict';
  const TAU = Math.PI * 2;
  const CYAN = [56, 189, 248], TEAL = [45, 212, 191];
  const STATUS = {
    known: { rgb: [47, 209, 139], hex: '#2fd18b', title: 'Face Recognized' },
    unknown: { rgb: [255, 93, 108], hex: '#ff5d6c', title: 'Unknown' },
    ambiguous: { rgb: [255, 181, 71], hex: '#ffb547', title: 'Ambiguous' },
  };
  const ALIAS = { known: 'known', match: 'known', recognized: 'known', ok: 'known', unknown: 'unknown', nomatch: 'unknown', ambiguous: 'ambiguous' };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  const mix = (c1, c2, t) => [lerp(c1[0], c2[0], t), lerp(c1[1], c2[1], t), lerp(c1[2], c2[2], t)];
  // tint without passing through purple: base -> pale white -> status colour (never a cyan/red RGB mix)
  const WHITE = [228, 246, 255];
  const tintMix = (base, to, t) => (t <= 0.001 ? base : t < 0.5 ? mix(base, WHITE, t * 2) : mix(WHITE, to, t * 2 - 1));
  const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;
  const smooth = (e0, e1, x) => { const t = clamp((x - e0) / (e1 - e0), 0, 1); return t * t * (3 - 2 * t); };
  const gauss = (x, y, x0, y0, sx, sy) => Math.exp(-(((x - x0) / sx) ** 2) - (((y - y0) / sy) ** 2));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const reduced = () => !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);

  // ---------------- head geometry (procedural, built once) ----------------
  // side-profile table: [y, front z, back z, half-width] from crown to chin (head faces +z)
  const PROFILE = [
    [1.0, 0.0, -0.06, 0.06], [0.97, 0.18, -0.3, 0.28], [0.9, 0.36, -0.56, 0.45], [0.76, 0.54, -0.8, 0.6], [0.56, 0.66, -0.92, 0.68],
    [0.36, 0.72, -0.96, 0.7], [0.22, 0.74, -0.95, 0.71], [0.1, 0.68, -0.9, 0.72], [-0.05, 0.67, -0.82, 0.7], [-0.2, 0.66, -0.7, 0.66],
    [-0.35, 0.64, -0.56, 0.6], [-0.5, 0.64, -0.42, 0.53], [-0.64, 0.6, -0.3, 0.45], [-0.76, 0.56, -0.2, 0.34], [-0.86, 0.44, -0.1, 0.22],
    [-0.93, 0.26, 0.0, 0.1],
  ];
  function profileAt(y) {
    for (let i = 0; i < PROFILE.length - 1; i++) {
      const A = PROFILE[i], B = PROFILE[i + 1];
      if (y <= A[0] && y >= B[0]) {
        const t = (A[0] - y) / (A[0] - B[0]), k = (1 - Math.cos(t * Math.PI)) / 2;
        return [lerp(A[1], B[1], k), lerp(A[2], B[2], k), lerp(A[3], B[3], k)];
      }
    }
    const L = PROFILE[PROFILE.length - 1]; return [L[1], L[2], L[3]];
  }
  function headPoint(y, th) {
    const [zf, zb, w] = profileAt(y);
    const sn = Math.sin(th), cs = Math.cos(th);
    const front = cs > 0, n = front ? 3.2 : 2.2; // flatter face, rounder skull
    const pw = (v) => Math.sign(v) * Math.abs(v) ** (2 / n);
    let x = w * pw(sn), z = (front ? zf : -zb) * pw(cs);
    const u = w > 0.01 ? x / w : 0; // -1..1 across the face
    if (front) {
      const fz = Math.max(0, cs) ** 1.5;
      let f = 0;
      f += (0.08 + 0.27 * smooth(0.12, -0.2, y)) * smooth(0.2, 0.1, y) * (1 - smooth(-0.2, -0.27, y)) * gauss(u, 0, 0, 0, 0.17 + 0.1 * smooth(0, -0.25, y), 1); // nose bridge -> tip
      f += 0.05 * gauss(u, y, -0.2, -0.24, 0.12, 0.04) + 0.05 * gauss(u, y, 0.2, -0.24, 0.12, 0.04); // nostril wings
      f -= 0.12 * (gauss(u, y, -0.42, 0.1, 0.2, 0.075) + gauss(u, y, 0.42, 0.1, 0.2, 0.075)); // eye sockets
      f += 0.035 * (gauss(u, y, -0.42, 0.09, 0.1, 0.035) + gauss(u, y, 0.42, 0.09, 0.1, 0.035)); // eyeballs
      f += 0.05 * gauss(u, y, 0, 0.25, 0.75, 0.05); // brow ridge
      f += 0.06 * gauss(u, y, 0, -0.44, 0.38, 0.06) - 0.03 * gauss(u, y, 0, -0.47, 0.32, 0.012); // lips + mouth line
      f += 0.06 * gauss(u, y, 0, -0.74, 0.35, 0.07); // chin
      f += 0.05 * (gauss(u, y, -0.62, -0.08, 0.2, 0.1) + gauss(u, y, 0.62, -0.08, 0.2, 0.1)); // cheekbones
      z += f * fz;
    }
    const ear = gauss(cs, y, -0.12, 0.02, 0.16, 0.17) * smooth(0.8, 0.97, Math.abs(sn));
    x += Math.sign(sn) * 0.1 * ear;
    return [x, y, z];
  }
  function buildHead() {
    const ROWS = 36, COLS = 46, V = [], lines = [];
    const idx = (i, j) => i * COLS + (j % COLS);
    for (let i = 0; i < ROWS; i++) {
      const y = lerp(0.995, -0.93, i / (ROWS - 1));
      for (let j = 0; j < COLS; j++) V.push(headPoint(y, (j / COLS) * TAU));
    }
    for (let i = 0; i < ROWS; i++) for (let j = 0; j < COLS; j++) {
      lines.push(idx(i, j), idx(i, j + 1));
      if (i < ROWS - 1) { lines.push(idx(i, j), idx(i + 1, j)); if ((i + j) % 2 === 0) lines.push(idx(i, j), idx(i + 1, j + 1)); }
    }
    // neck
    const base = V.length, NR = 9, NC = 28;
    for (let i = 0; i < NR; i++) {
      const y = lerp(-0.58, -1.4, i / (NR - 1)), w = 1 + 0.45 * smooth(-1.12, -1.4, y);
      for (let j = 0; j < NC; j++) { const a = (j / NC) * TAU; V.push([Math.sin(a) * 0.29 * w, y, Math.cos(a) * 0.31 * w - 0.17 - (y + 0.58) * 0.1]); }
    }
    for (let i = 0; i < NR; i++) for (let j = 0; j < NC; j++) {
      const a = base + i * NC + j, b = base + i * NC + ((j + 1) % NC);
      lines.push(a, b); if (i < NR - 1) lines.push(a, a + NC);
    }
    // per-vertex normals from the grid (for rim/fresnel shading that sculpts the face)
    const N = new Float32Array(V.length * 3);
    const nrm = (k, a, b, c, d) => { // cross(b-a, d-c)
      const u = [V[b][0] - V[a][0], V[b][1] - V[a][1], V[b][2] - V[a][2]], v = [V[d][0] - V[c][0], V[d][1] - V[c][1], V[d][2] - V[c][2]];
      let nx = u[1] * v[2] - u[2] * v[1], ny = u[2] * v[0] - u[0] * v[2], nz = u[0] * v[1] - u[1] * v[0];
      const l = Math.hypot(nx, ny, nz) || 1; nx /= l; ny /= l; nz /= l;
      const p = V[k]; if (nx * p[0] + ny * (p[1] + 0.1) + nz * p[2] < 0) { nx = -nx; ny = -ny; nz = -nz; } // point outward
      N[k * 3] = nx; N[k * 3 + 1] = ny; N[k * 3 + 2] = nz;
    };
    for (let i = 0; i < ROWS; i++) for (let j = 0; j < COLS; j++) nrm(idx(i, j), idx(i, j + 1), idx(i, j + COLS - 1), idx(Math.max(0, i - 1), j), idx(Math.min(ROWS - 1, i + 1), j));
    for (let i = 0; i < NR; i++) for (let j = 0; j < NC; j++) { const k = base + i * NC + j; N[k * 3] = Math.sin((j / NC) * TAU); N[k * 3 + 2] = Math.cos((j / NC) * TAU); }
    // bright feature contours (eyes, brows, nose, lips, jaw) drawn on the surface so the face reads at any angle
    const contours = [], contourUV = [], cStart = V.length;
    const surf = (u, y) => { // face-space (u across -1..1, y) -> surface point slightly lifted
      const sn = Math.sign(u) * Math.min(1, Math.abs(u)) ** (3.2 / 2), th = Math.asin(clamp(sn, -1, 1));
      const q = headPoint(y, th); return [q[0] * 1.01, q[1], q[2] + 0.012];
    };
    const poly = (pts, closed) => {
      const ids = pts.map((p) => { V.push(surf(p[0], p[1])); contourUV.push([V.length - 1, p[0], p[1]]); return V.length - 1; });
      if (closed) ids.push(ids[0]); contours.push(ids);
    };
    const ell = (cu, cy, ru, ry, n = 18, a0 = 0, a1 = TAU, closed = true) => poly(Array.from({ length: n + (closed ? 0 : 1) }, (_, i) => {
      const a = a0 + (a1 - a0) * (i / n); return [cu + Math.cos(a) * ru, cy + Math.sin(a) * ry]; }), closed);
    for (const sx of [-1, 1]) {
      ell(sx * 0.42, 0.095, 0.17, 0.042); // eye
      ell(sx * 0.42, 0.097, 0.05, 0.03, 10); // iris
      ell(sx * 0.42, 0.105, 0.24, 0.1, 12, Math.PI * 1.15, Math.PI * 1.85, false); // brow arc
      poly([[sx * 0.18, -0.2], [sx * 0.22, -0.24], [sx * 0.15, -0.265], [sx * 0.05, -0.255]]); // nostril wing
    }
    poly(Array.from({ length: 10 }, (_, i) => [0, lerp(0.16, -0.235, i / 9)])); // nose ridge
    poly([[-0.05, -0.255], [0, -0.262], [0.05, -0.255]]);
    poly([[-0.3, -0.47], [-0.14, -0.445], [-0.05, -0.44], [0, -0.448], [0.05, -0.44], [0.14, -0.445], [0.3, -0.47]]); // upper lip (cupid's bow)
    ell(0, -0.475, 0.19, 0.035, 12, 0, Math.PI, false); // lower lip
    poly(Array.from({ length: 9 }, (_, i) => [lerp(-0.3, 0.3, i / 8), -0.47 + 0.008 * Math.sin((i / 8) * Math.PI)])); // mouth line
    ell(0, -0.72, 0.25, 0.06, 12, 0, Math.PI, false); // chin
    // contour normals: numeric surface normal at each contour point
    const N2 = new Float32Array(V.length * 3); N2.set(N);
    for (const c of contourUV) {
      const [k, u, y] = c, e = 0.02;
      const a = surf(u - e, y), b = surf(u + e, y), d = surf(u, y - e), f = surf(u, y + e);
      const t1 = [b[0] - a[0], b[1] - a[1], b[2] - a[2]], t2 = [f[0] - d[0], f[1] - d[1], f[2] - d[2]];
      let nx = t1[1] * t2[2] - t1[2] * t2[1], ny = t1[2] * t2[0] - t1[0] * t2[2], nz = t1[0] * t2[1] - t1[1] * t2[0];
      const l = Math.hypot(nx, ny, nz) || 1; if (nz < 0) { nx = -nx; ny = -ny; nz = -nz; }
      N2[k * 3] = nx / l; N2[k * 3 + 1] = ny / l; N2[k * 3 + 2] = nz / l;
    }
    const sp2 = new Float32Array(V.length).map(() => Math.random());
    return { V, N: N2, lines: new Uint32Array(lines), contours, cStart, sparkle: sp2, neckStart: base };
  }

  // ---------------- instance ----------------
  let S = null; // state of the mounted instance

  function mount(stageEl, opts = {}) {
    if (!stageEl) throw new Error('FaceHologram.mount: missing element');
    if (S) destroy();
    const o = { overlay: 'auto', maxPeople: 14, background: 'transparent', ...opts };
    const showOverlay = o.overlay === 'auto' ? !(document.getElementById('verdict-block') || document.getElementById('conf-block')) : !!o.overlay;
    const root = document.createElement('div');
    root.className = 'fh-root' + (showOverlay ? '' : ' fh-no-overlay');
    root.setAttribute('aria-hidden', 'true');
    root.style.background = o.background;
    root.innerHTML = `<canvas class="fh-canvas"></canvas><div class="fh-nodes"></div>
      <div class="fh-label" data-state="idle"><span class="fh-ring"><svg viewBox="0 0 24 24"><path class="fh-i-ok" d="M6 12.5l4 4 8-9"/><path class="fh-i-bad" d="M7 7l10 10M17 7L7 17"/><path class="fh-i-amb" d="M9.2 9a3 3 0 1 1 4.3 2.7c-1 .5-1.5 1.1-1.5 2.1M12 17.2v.3"/></svg></span>
        <span class="fh-txt"><span class="fh-title"></span><span class="fh-name"></span></span></div>
      <div class="fh-conf" data-state="idle"><span class="fh-conf-lbl">Confidence</span><span class="fh-conf-val">—</span></div>
      <div class="fh-scan"></div>`;
    stageEl.appendChild(root);
    const cv = root.querySelector('canvas');
    S = {
      o, root, stageEl, cv, ctx: cv.getContext('2d'), showOverlay,
      hc: document.createElement('canvas'), gc: document.createElement('canvas'),
      head: buildHead(), proj: null,
      nodesEl: root.querySelector('.fh-nodes'), labelEl: root.querySelector('.fh-label'), confEl: root.querySelector('.fh-conf'),
      W: 0, H: 0, dpr: 1, L: null,
      state: 'idle', status: null, result: null, people: [], nodes: [],
      t0: performance.now(), last: performance.now(), time: 0,
      yaw: -0.35, pitch: 0, roll: 0, target: { yaw: 0, pitch: 0, roll: 0, has: false },
      tint: 0, tintRGB: STATUS.known.rgb, confShown: 0, confTarget: 0, labelIn: 0, scanT: 0, flick: 1,
      raf: 0, running: false, zones: [], orbit: null, watched: new Set(), frame: 0,
    };
    S.proj = new Float32Array(S.head.V.length * 3); S.shade = new Float32Array(S.head.V.length); S.facing = new Float32Array(S.head.V.length);
    S.hctx = S.hc.getContext('2d'); S.gctx = S.gc.getContext('2d');
    S.ro = new ResizeObserver(() => { resize(); kick(); });
    S.onWin = () => { measureAvoid(); kick(); };
    window.addEventListener('resize', S.onWin);
    S.ro.observe(stageEl);
    S.onVis = () => (document.hidden ? stop() : kick());
    document.addEventListener('visibilitychange', S.onVis);
    S.mq = window.matchMedia ? matchMedia('(prefers-reduced-motion: reduce)') : null;
    S.onMq = () => kick();
    S.mq?.addEventListener?.('change', S.onMq);
    resize();
    document.body.classList.add('holo-ready');
    kick();
    return api;
  }

  function destroy() {
    if (!S) return;
    stop();
    S.ro.disconnect();
    document.removeEventListener('visibilitychange', S.onVis);
    window.removeEventListener('resize', S.onWin);
    S.mq?.removeEventListener?.('change', S.onMq);
    S.root.remove();
    document.body.classList.remove('holo-ready');
    S = null;
  }

  function resize() {
    const r = S.stageEl.getBoundingClientRect();
    const W = Math.max(1, Math.round(r.width)), H = Math.max(1, Math.round(r.height));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (W === S.W && H === S.H && dpr === S.dpr) { measureAvoid(); return; }
    S.W = W; S.H = H; S.dpr = dpr;
    for (const c of [S.cv, S.hc]) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
    S.gc.width = Math.max(1, Math.round(W / 4)); S.gc.height = Math.max(1, Math.round(H / 4));
    // layout: head unit scale, centre, base
    const s = Math.min(W * 0.19, H * 0.235);
    S.L = { s, cx: W / 2, cy: H * 0.4, D: 5.2, baseY: -1.62 };
    // overlay positions
    const lx = S.L.cx - 1.25 * s, ly = S.L.cy + 0.05 * s;
    S.L.label = [lx, ly];
    S.L.gauge = [S.L.cx + 0.95 * s, S.L.cy + 0.02 * s, 0.6 * s]; // arc centre + radius (arc spans down -> right)
    S.labelEl.style.left = lx + 'px'; S.labelEl.style.top = ly + 'px';
    S.confEl.style.left = (S.L.gauge[0] + 0.12 * s) + 'px'; S.confEl.style.top = (S.L.gauge[1] - 0.12 * s) + 'px';
    S.root.style.setProperty('--fh-s', s + 'px');
    measureAvoid();
    layoutNodes();
  }

  // ---------------- keep-out zones (page readouts beside / over the stage) ----------------
  // Anything matching these selectors (plus opts.avoid) is treated as an obstruction: the orbit is narrowed and
  // re-centred into the free space, name labels flip side / drop below, and whatever still collides is faded out.
  const AVOID_SEL = '#verdict-block, .verdict-block, #status-card, #conf-block, .conf-block, [data-holo-avoid]';
  function measureAvoid() {
    if (!S || !S.L) return;
    const sr = S.stageEl.getBoundingClientRect();
    const sel = AVOID_SEL + (S.o.avoid ? ', ' + S.o.avoid : '');
    const zones = [], pad = 10;
    let els = [];
    try { els = [...document.querySelectorAll(sel)]; } catch { els = [...document.querySelectorAll(AVOID_SEL)]; }
    for (const el of els) {
      if (S.root.contains(el) || el.contains(S.stageEl)) continue;
      if (!S.watched.has(el)) { S.watched.add(el); S.ro.observe(el); }
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const r = el.getBoundingClientRect(); if (r.width < 2 || r.height < 2) continue;
      zones.push({ l: r.left - sr.left - pad, t: r.top - sr.top - pad, r: r.right - sr.left + pad, b: r.bottom - sr.top + pad });
    }
    S.zones = zones;
    const { s, cx, cy } = S.L;
    // free horizontal span in the orbit's vertical band (stage edges clip, so they are walls too)
    const bandT = cy - 1.4 * s, bandB = cy + 1.2 * s;
    let fl = 6, fr = S.W - 6;
    for (const z of zones) {
      if (z.b < bandT || z.t > bandB || z.r < 0 || z.l > S.W) continue;
      if ((z.l + z.r) / 2 < cx) fl = Math.max(fl, z.r); else fr = Math.min(fr, z.l);
    }
    if (fr - fl < 2 * s) { fl = 6; fr = S.W - 6; } // obstruction covers the middle: ignore for sizing, fade instead
    const ocx = clamp((fl + fr) / 2, cx - 0.35 * s, cx + 0.35 * s);
    const half = Math.min(ocx - fl, fr - ocx);
    for (const n of S.nodes) { const w = n.el.querySelector('.fh-nm')?.offsetWidth; if (w) n.nw = w; }
    const maxNw = S.nodes.reduce((m, n) => Math.max(m, n.nw || 0), 0);
    const sc = clamp(s / 150, 0.85, 1.15), avR = 22 * sc, minR = 0.95 * s + avR, maxR = 1.72 * 1.3 * s;
    let rx = half - avR - (maxNw + 30) * sc; // room for side labels at the extremes
    if (rx < minR) rx = half - avR - (maxNw / 2) * sc; // else labels go below the avatar there
    S.orbit = { cx: ocx, rx: clamp(rx, minR, maxR), fl, fr };
  }
  const hitZone = (l, t, r, b) => {
    if (l < 0 || t < 0 || r > S.W || b > S.H) return true;
    for (const z of S.zones) if (l < z.r && z.l < r && t < z.b && z.t < b) return true;
    return false;
  };

  // project a world point (already in head/world space) -> screen [x, y, depthScale, z]
  function project(x, y, z, out, k) {
    const { s, cx, cy, D } = S.L;
    const p = D / (D - z);
    out[k] = cx + x * s * p; out[k + 1] = cy - y * s * p; out[k + 2] = z;
    return p;
  }

  // ---------------- people orbit ----------------
  function setPeople(list) {
    if (!S) return;
    const arr = Array.isArray(list) ? list : list?.people || [];
    S.people = arr.slice(0, S.o.maxPeople).map((p) => ({
      id: p.id, name: String(p.name ?? ''),
      thumbUrl: p.thumbUrl || p.thumb_url || p.avatar || (p.photos?.[0] ? `/api/photos/${p.photos[0].id}/thumb` : ''),
    }));
    S.nodesEl.innerHTML = '';
    S.nodes = S.people.map((p, i) => {
      const el = document.createElement('div'); el.className = 'fh-node';
      const av = p.thumbUrl ? `<img alt="" src="${esc(p.thumbUrl)}" draggable="false">` : `<b>${esc((p.name[0] || '?').toUpperCase())}</b>`;
      el.innerHTML = `<span class="fh-av">${av}</span><span class="fh-nm">${esc(p.name)}</span>`;
      const img = el.querySelector('img');
      if (img) img.addEventListener('error', () => { img.replaceWith(Object.assign(document.createElement('b'), { textContent: (p.name[0] || '?').toUpperCase() })); }, { once: true });
      S.nodesEl.appendChild(el);
      return { p, el, a0: (i / Math.max(1, S.people.length)) * TAU, x: 0, y: 0, z: 0 };
    });
    if (S.people.length > 0 && arr.length > S.people.length) {
      const more = document.createElement('div'); more.className = 'fh-more'; more.textContent = `+${arr.length - S.people.length} enrolled`;
      S.nodesEl.appendChild(more);
    }
    markMatched(); requestAnimationFrame(() => { measureAvoid(); layoutNodes(); kick(); }); layoutNodes(); kick();
  }
  function markMatched() {
    for (const n of S.nodes) n.el.classList.toggle('fh-hit', S.state === 'result' && S.status === 'known' && !!S.result && n.p.name === S.result.name);
  }
  function layoutNodes() {
    if (!S || !S.L || !S.nodes.length) return;
    const { s } = S.L, R = 1.72, tilt = 0.42;
    const ob = S.orbit || { cx: S.L.cx, rx: R * 1.3 * s };
    const RX = ob.rx / (R * s), dx = ob.cx - S.L.cx;
    const spin = S.time * 0.085;
    const out = [0, 0, 0];
    for (const n of S.nodes) {
      const a = n.a0 + spin;
      const x = Math.sin(a) * R * RX, z0 = Math.cos(a) * R;
      const y = 0.1 - z0 * Math.sin(tilt) * 0.85, z = z0 * Math.cos(tilt);
      const p = project(x, y, z, out, 0);
      out[0] += dx;
      n.x = out[0]; n.y = out[1]; n.z = z;
      const front = z / R; // -1 back .. 1 front
      const overFace = front > 0.2 && Math.abs(x) < 0.85 ? 0.35 : 1; // keep the face readable
      const nearUi = S.showOverlay && S.labelIn > 0.3 && S.L.label && ((Math.abs(out[0] - (S.L.label[0] - 0.55 * s)) < 0.75 * s && Math.abs(out[1] - S.L.label[1]) < 0.45 * s)
        || (Math.abs(out[0] - (S.L.gauge[0] + 0.45 * s)) < 0.7 * s && Math.abs(out[1] - S.L.gauge[1]) < 0.55 * s)) ? 0.2 : 1;
      const sc = clamp(p, 0.8, 1.25) * clamp(s / 150, 0.85, 1.15);
      // label placement: outer side -> below -> inner side -> hidden; avatar itself fades if it collides
      const X = out[0], Y = out[1], nw = (n.nw || 80) * sc, outerLeft = X < ob.cx;
      const side = (left) => (left ? hitZone(X - 26 * sc - nw, Y - 9 * sc, X - 26 * sc, Y + 9 * sc) : hitZone(X + 26 * sc, Y - 9 * sc, X + 26 * sc + nw, Y + 9 * sc));
      let mode;
      if (!side(outerLeft)) mode = outerLeft ? 'l' : 'r';
      else if (!hitZone(X - nw / 2, Y + 22 * sc, X + nw / 2, Y + 40 * sc)) mode = 'b';
      else if (!side(!outerLeft)) mode = outerLeft ? 'r' : 'l';
      else mode = 'off';
      const avHit = hitZone(X - 20 * sc, Y - 20 * sc, X + 20 * sc, Y + 20 * sc);
      if (mode !== n.mode) {
        n.mode = mode;
        n.el.classList.toggle('fh-left', mode === 'l');
        n.el.classList.toggle('fh-below', mode === 'b');
        n.el.classList.toggle('fh-nm-off', mode === 'off');
      }
      const op = avHit ? 0.06 : clamp((0.45 + 0.55 * (front + 1) / 2) * overFace * nearUi, 0.1, 1);
      n.el.style.transform = `translate(${X.toFixed(1)}px, ${Y.toFixed(1)}px) scale(${sc.toFixed(3)})`;
      n.el.style.opacity = op.toFixed(2);
      n.el.style.zIndex = z > 0 ? 3 : 1;
      n.el.classList.toggle('fh-blocked', avHit);
    }
  }

  // ---------------- results / state ----------------
  function normStatus(st) { return ALIAS[String(st || '').toLowerCase()] || null; }
  function update(res) {
    if (!S) return;
    if (!res) { S.result = null; S.status = null; S.target.has = false; S.confTarget = 0; renderOverlay(); markMatched(); kick(); return; }
    const status = normStatus(res.status) || (res.known ? 'known' : res.ambiguous ? 'ambiguous' : 'unknown');
    let c = Number(res.confidence); if (!isFinite(c)) c = 0; if (c > 1) c /= 100; c = clamp(c, 0, 1);
    S.result = { name: res.name ?? '', status, confidence: c, kps: res.kps };
    S.status = status; S.confTarget = c;
    S.target = poseFromKps(res.kps);
    S.tint = 1; S.tintRGB = STATUS[status].rgb;
    if (S.state !== 'scanning') S.state = 'result';
    renderOverlay(); markMatched(); remeasureSoon(); kick();
  }
  function poseFromKps(k) {
    if (!Array.isArray(k) || k.length < 3 || !k.every((p) => Array.isArray(p) && isFinite(p[0]) && isFinite(p[1]))) return { yaw: 0, pitch: 0, roll: 0, has: false };
    const [le, re, no] = k, mo = k.length >= 5 ? [(k[3][0] + k[4][0]) / 2, (k[3][1] + k[4][1]) / 2] : null;
    const em = [(le[0] + re[0]) / 2, (le[1] + re[1]) / 2];
    const ed = Math.hypot(re[0] - le[0], re[1] - le[1]) || 1;
    const yaw = clamp(((no[0] - em[0]) / ed) * 1.6, -0.75, 0.75);
    const roll = clamp(-Math.atan2(re[1] - le[1], re[0] - le[0]), -0.5, 0.5);
    let pitch = 0;
    if (mo) { const r = (no[1] - em[1]) / ((mo[1] - em[1]) || 1); pitch = clamp((r - 0.55) * 1.4, -0.4, 0.4); }
    return { yaw, pitch, roll, has: true };
  }
  function setState(st) {
    if (!S) return;
    st = String(st || 'idle').toLowerCase();
    const asStatus = normStatus(st);
    if (asStatus) { // tolerate setState('match'|'unknown'|'ambiguous')
      if (!S.result) S.result = { name: '', status: asStatus, confidence: S.confTarget, kps: null };
      S.result.status = asStatus; S.status = asStatus; S.tint = 1; S.tintRGB = STATUS[asStatus].rgb; st = 'result';
    }
    if (!['idle', 'scanning', 'result'].includes(st)) st = 'idle';
    if (st === 'result' && S.state !== 'result' && S.status) { S.tint = 1; S.tintRGB = STATUS[S.status].rgb; S.labelIn = 0; }
    if (st === 'scanning') S.scanT = 0;
    if (st === 'idle') S.target = { yaw: 0, pitch: 0, roll: 0, has: false };
    S.state = st; S.root.dataset.state = st;
    renderOverlay(); markMatched(); remeasureSoon(); kick();
  }
  // page readouts change size/visibility right after setState/update: measure again once they have laid out
  function remeasureSoon() { requestAnimationFrame(() => requestAnimationFrame(() => { if (S) { measureAvoid(); if (reduced()) drawFrame(true); } })); setTimeout(() => S && measureAvoid(), 450); }
  function renderOverlay() {
    const st = S.state === 'result' && S.status ? S.status : S.state; // idle | scanning | known | unknown | ambiguous
    S.labelEl.dataset.state = st; S.confEl.dataset.state = st;
    const title = S.labelEl.querySelector('.fh-title'), name = S.labelEl.querySelector('.fh-name');
    if (st === 'scanning') { title.textContent = 'Scanning…'; name.textContent = ''; }
    else if (STATUS[st]) {
      title.textContent = STATUS[st].title;
      name.textContent = st === 'known' ? (S.result?.name || '') : st === 'ambiguous' ? (S.result?.name && S.result.name !== 'Unknown' ? S.result.name : 'Too close to call') : 'Not enrolled';
    } else { title.textContent = ''; name.textContent = ''; }
    if (!STATUS[st]) S.confEl.querySelector('.fh-conf-val').textContent = '—';
  }

  // ---------------- render loop ----------------
  function kick() {
    if (!S) return;
    if (reduced()) { stop(); drawFrame(true); return; }
    if (document.hidden) return;
    if (!S.running) { S.running = true; S.last = performance.now(); S.raf = requestAnimationFrame(loop); }
  }
  function stop() { if (S) { cancelAnimationFrame(S.raf); S.running = false; } }
  function loop(now) {
    if (!S || !S.running) return;
    const dt = Math.min(0.05, (now - S.last) / 1000); S.last = now;
    S.time += dt; if (++S.frame % 45 === 0) measureAvoid(); step(dt);
    if (S.W > 2 && S.H > 2 && S.stageEl.offsetParent !== null) drawFrame(false);
    S.raf = requestAnimationFrame(loop);
  }
  function step(dt) {
    const scanning = S.state === 'scanning';
    // pose: idle sway, or ease toward detected face pose
    let ty, tp, tr;
    if (S.target.has && S.state === 'result') { ty = S.target.yaw * 0.8 - 0.62 + Math.sin(S.time * 0.5) * 0.1; tp = S.target.pitch; tr = S.target.roll; }
    else { ty = Math.sin(S.time * (scanning ? 0.9 : 0.32)) * 0.62; tp = Math.sin(S.time * 0.21) * 0.06; tr = 0; }
    const k = 1 - Math.exp(-dt * 2.2);
    S.yaw = lerp(S.yaw, ty, k); S.pitch = lerp(S.pitch, tp, k); S.roll = lerp(S.roll, tr, k);
    // tint: flash then settle to a faint hue
    const settle = S.state === 'result' && S.status ? 0.8 : 0;
    S.tint = lerp(S.tint, settle, 1 - Math.exp(-dt * (settle ? 0.8 : 3)));
    S.confShown = lerp(S.confShown, S.state === 'result' && S.status ? S.confTarget : 0, 1 - Math.exp(-dt * 4));
    S.labelIn = lerp(S.labelIn, (S.state === 'result' && S.status) || scanning ? 1 : 0, 1 - Math.exp(-dt * 5));
    S.scanT += dt;
    S.flick = Math.random() < 0.012 ? 0.72 + Math.random() * 0.2 : lerp(S.flick, 1, 0.25);
    layoutNodes();
  }

  function drawFrame(isStatic) {
    if (!S || !S.L) return;
    if (isStatic) { // reduced motion: a fixed, pleasant 3/4 view
      const t = S.target.has && S.state === 'result' ? { ...S.target, yaw: S.target.yaw * 0.8 - 0.62 } : { yaw: -0.62, pitch: 0.02, roll: 0 };
      S.yaw = S.o.fixedYaw ?? t.yaw; S.pitch = t.pitch; S.roll = t.roll;
      S.tint = S.state === 'result' && S.status ? 0.85 : 0;
      S.confShown = S.state === 'result' && S.status ? S.confTarget : 0;
      S.labelIn = (S.state === 'result' && S.status) || S.state === 'scanning' ? 1 : 0;
      S.flick = 1; layoutNodes();
    }
    const { ctx, hctx, gctx, dpr, W, H, L } = S;
    const scanning = S.state === 'scanning';
    const col = S.tint > 0.01 ? S.tintRGB : CYAN;
    const cA = tintMix(CYAN, col, S.tint), cB = tintMix(TEAL, col, S.tint);
    // ---- head into offscreen ----
    hctx.setTransform(1, 0, 0, 1, 0, 0); hctx.clearRect(0, 0, S.hc.width, S.hc.height);
    hctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    hctx.globalCompositeOperation = 'lighter';
    const { V, N: NN, lines, sparkle } = S.head, P = S.proj, SH = S.shade, SHF = S.facing;
    const cy = Math.cos(S.yaw), sy = Math.sin(S.yaw), cp = Math.cos(S.pitch), sp = Math.sin(S.pitch), cr = Math.cos(S.roll), sr = Math.sin(S.roll);
    const bob = isStatic ? 0 : Math.sin(S.time * 1.1) * 0.03;
    for (let i = 0, k = 0; i < V.length; i++, k += 3) {
      let [x, y, z] = V[i];
      y += 0.1; // pivot near the neck
      let x1 = x * cy + z * sy, z1 = -x * sy + z * cy; // yaw
      let y1 = y * cp - z1 * sp, z2 = y * sp + z1 * cp; // pitch
      const x2 = x1 * cr - y1 * sr, y2 = x1 * sr + y1 * cr; // roll
      project(x2, y2 - 0.1 + bob, z2, P, k);
      const nx = NN[k], ny = NN[k + 1], nz = NN[k + 2];
      const nz1 = -nx * sy + nz * cy, nz2 = ny * sp + nz1 * cp;
      SHF[i] = nz2;
      SH[i] = clamp(1 - Math.abs(nz2), 0, 1) ** 1.6 * (nz2 > -0.2 ? 1 : 0.35) + (z2 > 0 ? 0.12 * nz2 : 0); // rim + front fill
    }
    const top = L.cy - 1.05 * L.s, bot = L.cy + 1.5 * L.s;
    const grad = hctx.createLinearGradient(0, top, 0, bot);
    grad.addColorStop(0, rgba(cA, 1)); grad.addColorStop(1, rgba(cB, 1));
    // wireframe in 3 depth buckets
    const buckets = [[], [], []];
    for (let i = 0; i < lines.length; i += 2) {
      const a = lines[i] * 3, b = lines[i + 1] * 3, z = (P[a + 2] + P[b + 2]) / 2;
      const sh = (SH[lines[i]] + SH[lines[i + 1]]) / 2 + (z > 0.2 ? 0.15 : z < -0.3 ? -0.2 : 0);
      buckets[sh > 0.5 ? 0 : sh > 0.2 ? 1 : 2].push(a, b);
    }
    const lineA = [0.55, 0.24, 0.07];
    hctx.strokeStyle = grad; hctx.lineWidth = 0.7;
    for (let bI = 0; bI < 3; bI++) {
      const B = buckets[bI]; hctx.globalAlpha = lineA[bI] * S.flick; hctx.beginPath();
      for (let i = 0; i < B.length; i += 2) { hctx.moveTo(P[B[i]], P[B[i] + 1]); hctx.lineTo(P[B[i + 1]], P[B[i + 1] + 1]); }
      hctx.stroke();
    }
    // feature contours (only the parts turned toward the viewer)
    hctx.lineWidth = 1.1; hctx.lineJoin = 'round';
    const { contours } = S.head;
    for (const pass of [0, 1]) {
      hctx.globalAlpha = (pass ? 0.95 : 0.25) * S.flick; hctx.beginPath();
      for (const c of contours) for (let i = 0; i < c.length - 1; i++) {
        const a = c[i] * 3, b = c[i + 1] * 3;
        const vis = SHF[c[i]] > 0.2 && SHF[c[i + 1]] > 0.2; if (vis !== !!pass) continue;
        hctx.moveTo(P[a], P[a + 1]); hctx.lineTo(P[b], P[b + 1]);
      }
      hctx.stroke();
    }
    // vertices (points), brighter in front, sparkle + scan band highlight
    const scanPeriod = scanning ? 1.4 : 4.5;
    const band = isStatic ? -99 : 1.0 - ((S.scanT % scanPeriod) / scanPeriod) * 2.7; // world y of the scan band
    hctx.fillStyle = grad;
    const ptA = [1, 0.5, 0.16];
    for (let bI = 0; bI < 3; bI++) {
      hctx.globalAlpha = ptA[bI] * S.flick; hctx.beginPath();
      for (let i = 0, k = 0; i < S.head.cStart; i++, k += 3) {
        const z = P[k + 2], sh = SH[i] + (z > 0.2 ? 0.15 : z < -0.3 ? -0.2 : 0); if ((sh > 0.5 ? 0 : sh > 0.2 ? 1 : 2) !== bI) continue;
        const tw = isStatic ? 0.5 : 0.5 + 0.5 * Math.sin(S.time * 3 + sparkle[i] * 40);
        const r = (0.6 + 0.9 * tw * (bI === 0 ? 1 : 0.6)) * (1 + z * 0.25);
        hctx.rect(P[k] - r / 2, P[k + 1] - r / 2, r, r);
      }
      hctx.fill();
    }
    if (!isStatic) { // scan band: bright points near band height
      hctx.globalAlpha = scanning ? 1 : 0.55; hctx.fillStyle = rgba(tintMix([220, 250, 255], col, S.tint * 0.6), 1); hctx.beginPath();
      for (let i = 0, k = 0; i < V.length; i++, k += 3) {
        const d = Math.abs(V[i][1] - band); if (d > 0.05) continue;
        hctx.rect(P[k] - 1.2, P[k + 1] - 1.2, 2.4, 2.4);
      }
      hctx.fill();
    }
    hctx.globalAlpha = 1;
    // ---- glow: downsample head ----
    gctx.globalCompositeOperation = 'copy';
    gctx.drawImage(S.hc, 0, 0, S.gc.width, S.gc.height);
    // ---- compose main ----
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, S.cv.width, S.cv.height);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalCompositeOperation = 'lighter';
    drawBase(ctx, cA, cB, isStatic);
    ctx.globalAlpha = 0.9; ctx.imageSmoothingEnabled = true;
    ctx.drawImage(S.gc, 0, 0, W, H); ctx.drawImage(S.gc, -2, -2, W + 4, H + 4);
    ctx.globalAlpha = 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.drawImage(S.hc, 0, 0); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawNodeLinks(ctx, cA);
    if (S.showOverlay) drawOverlay(ctx, isStatic);
    ctx.globalCompositeOperation = 'source-over'; ctx.globalAlpha = 1;
  }

  function drawBase(ctx, cA, cB, isStatic) {
    const { s, cx, D } = S.L, out = [0, 0, 0];
    const t = isStatic ? 0 : S.time, scanning = S.state === 'scanning';
    project(0, S.L.baseY, 0, out, 0);
    const bx = out[0], by = out[1], squash = 0.2;
    // volumetric beam
    const beamTop = S.L.cy + 0.55 * s, w0 = 0.42 * s, w1 = 0.95 * s;
    const bg = ctx.createLinearGradient(0, by, 0, beamTop);
    const pulse = 0.85 + 0.15 * Math.sin(t * (scanning ? 6 : 2));
    bg.addColorStop(0, rgba(cA, 0.36 * pulse)); bg.addColorStop(0.55, rgba(cB, 0.1 * pulse)); bg.addColorStop(1, rgba(cB, 0));
    ctx.fillStyle = bg; ctx.beginPath();
    ctx.moveTo(bx - w0, by); ctx.lineTo(bx - w1, beamTop); ctx.lineTo(bx + w1, beamTop); ctx.lineTo(bx + w0, by); ctx.closePath(); ctx.fill();
    // beam streaks
    ctx.lineWidth = 1;
    for (let i = 0; i < 9; i++) {
      const u = (i / 8 - 0.5) * 2, ph = (t * 0.6 + i * 0.37) % 1;
      ctx.strokeStyle = rgba(cA, 0.1 + 0.08 * Math.sin(t * 2 + i));
      ctx.beginPath(); ctx.moveTo(bx + u * w0 * 0.9, by); ctx.lineTo(bx + u * w1 * 0.9, lerp(by, beamTop, 0.4 + 0.6 * ph)); ctx.stroke();
    }
    // rising particles
    ctx.fillStyle = rgba([210, 250, 255], 0.8);
    for (let i = 0; i < 26; i++) {
      const ph = ((t * (0.18 + (i % 5) * 0.03)) + i * 0.137) % 1, u = Math.sin(i * 12.9898) ;
      const y = lerp(by, beamTop - 0.3 * s, ph), x = bx + u * lerp(w0, w1, ph) * 0.85, a = Math.sin(ph * Math.PI);
      ctx.globalAlpha = 0.6 * a; ctx.fillRect(x - 0.8, y - 0.8, 1.6, 1.6);
    }
    ctx.globalAlpha = 1;
    // core glow
    const rg = ctx.createRadialGradient(bx, by, 0, bx, by, 0.75 * s);
    rg.addColorStop(0, rgba([220, 252, 255], 0.75 * pulse)); rg.addColorStop(0.25, rgba(cA, 0.35)); rg.addColorStop(1, rgba(cA, 0));
    ctx.save(); ctx.translate(bx, by); ctx.scale(1, squash * 1.4); ctx.translate(-bx, -by);
    ctx.fillStyle = rg; ctx.beginPath(); ctx.arc(bx, by, 0.75 * s, 0, TAU); ctx.fill(); ctx.restore();
    // concentric rings in perspective
    const rings = [[0.32, 2.2, 0.9, 0], [0.55, 1.4, 0.7, 0], [0.82, 1.6, 0.6, 1], [1.12, 1, 0.42, 2], [1.42, 1.2, 0.3, 3], [1.75, 0.8, 0.22, 4]];
    for (const [r, lw, a, kind] of rings) {
      const rx = r * s * (D / D), ry = rx * squash;
      const pul = scanning ? 0.75 + 0.25 * Math.sin(t * 8 - r * 4) : 0.9 + 0.1 * Math.sin(t * 1.6 - r * 3);
      ctx.strokeStyle = rgba(kind % 2 ? cB : cA, a * pul); ctx.lineWidth = lw;
      if (kind === 2 || kind === 4) { // dashed rotating ring
        ctx.setLineDash(kind === 2 ? [10, 7] : [2, 6]); ctx.lineDashOffset = (kind === 2 ? -1 : 1) * t * 22;
      }
      ctx.beginPath(); ctx.ellipse(bx, by, rx, ry, 0, 0, TAU); ctx.stroke(); ctx.setLineDash([]);
    }
    // expanding pulse ring
    const ph = (t * (scanning ? 0.9 : 0.35)) % 1, pr = lerp(0.3, 1.9, ph) * s;
    ctx.strokeStyle = rgba(cA, 0.45 * (1 - ph)); ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.ellipse(bx, by, pr, pr * squash, 0, 0, TAU); ctx.stroke();
  }

  function drawNodeLinks(ctx, cA) {
    if (!S.nodes.length) return;
    const { cx, cy, s } = S.L;
    ctx.lineWidth = 0.8;
    for (const n of S.nodes) {
      const hit = n.el.classList.contains('fh-hit');
      const front = n.z / 1.72;
      const a = hit ? 0.75 : 0.06 + 0.12 * (front + 1) / 2;
      const dirx = n.x - cx, diry = n.y - cy, dl = Math.hypot(dirx, diry) || 1;
      const ex = cx + (dirx / dl) * 0.62 * s, ey = cy + (diry / dl) * 0.75 * s;
      ctx.strokeStyle = rgba(hit ? STATUS.known.rgb : cA, a);
      ctx.beginPath(); ctx.moveTo(n.x - (dirx / dl) * 20, n.y - (diry / dl) * 20); ctx.lineTo(ex, ey); ctx.stroke();
      ctx.fillStyle = rgba(hit ? STATUS.known.rgb : cA, a * 1.6); ctx.fillRect(ex - 1.2, ey - 1.2, 2.4, 2.4);
    }
  }

  function drawOverlay(ctx, isStatic) {
    const st = S.state === 'result' && S.status ? S.status : S.state;
    const vis = S.labelIn;
    S.labelEl.style.opacity = vis.toFixed(3); S.confEl.style.opacity = (st in STATUS ? vis : 0).toFixed(3);
    if (vis < 0.02) return;
    const c = STATUS[st]?.rgb || CYAN, { s } = S.L;
    // connector from label to the left cheek (follows the head)
    const P = S.proj, out = [0, 0, 0];
    const cheek = [-0.45, -0.12, 0.45];
    const cyw = Math.cos(S.yaw), syw = Math.sin(S.yaw);
    project(cheek[0] * cyw + cheek[2] * syw, cheek[1], -cheek[0] * syw + cheek[2] * cyw, out, 0);
    const [lx, ly] = S.L.label;
    ctx.globalAlpha = vis; ctx.strokeStyle = rgba(c, 0.55); ctx.lineWidth = 0.8;
    ctx.beginPath(); ctx.moveTo(lx + 6, ly); ctx.lineTo(lx + 0.18 * s, ly); ctx.lineTo(out[0], out[1]); ctx.stroke();
    ctx.fillStyle = rgba(c, 0.9); ctx.beginPath(); ctx.arc(out[0], out[1], 2, 0, TAU); ctx.fill();
    // arc gauge
    if (st in STATUS) {
      const [gx, gy, gr] = S.L.gauge, N = 26, A0 = Math.PI / 2 + 0.12, A1 = -0.32; // canvas angles: down -> slightly above right
      const lit = Math.round(N * S.confShown);
      for (let i = 0; i < N; i++) {
        const t0 = lerp(A0, A1, (i + 0.62) / N), t1 = lerp(A0, A1, i / N);
        const on = i < lit;
        const cc = on ? (st === 'known' ? mix(TEAL, CYAN, i / N) : c) : CYAN;
        ctx.strokeStyle = rgba(cc, on ? 0.95 : 0.13); ctx.lineWidth = on ? 6 : 5;
        ctx.beginPath(); ctx.arc(gx, gy, gr, t0, t1, false); ctx.stroke();
      }
      // connector gauge -> jaw
      const jaw = [0.4, -0.45, 0.25];
      project(jaw[0] * cyw + jaw[2] * syw, jaw[1], -jaw[0] * syw + jaw[2] * cyw, out, 0);
      const ex = gx + gr * Math.cos(A0), ey = gy + gr * Math.sin(A0);
      ctx.strokeStyle = rgba(c, 0.4); ctx.lineWidth = 0.8;
      ctx.beginPath(); ctx.moveTo(ex - 4, ey); ctx.lineTo(out[0], out[1]); ctx.stroke();
      S.confEl.querySelector('.fh-conf-val').textContent = (S.confShown * 100).toFixed(1) + '%';
    }
    ctx.globalAlpha = 1;
    void P; void isStatic;
  }

  const api = { mount, update, setPeople, setState, destroy,
    get mounted() { return !!S; }, get state() { return S ? S.state : null; } };
  window.FaceHologram = api;
  try { window.dispatchEvent(new Event('facehologram:ready')); } catch { /* old browsers */ }
})();
