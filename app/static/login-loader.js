/* FaceID login — "Holographic Scan" loading overlay (from loaders/loader-3.html).
 *
 *   FaceLoginLoader.play({ name, onDone, sfx })
 *     name   : subject name to show (falls back to "OPERATOR")
 *     onDone : called exactly once — after ACCESS GRANTED + a short beat,
 *              or immediately when the user clicks / presses a key to skip.
 *     sfx    : optional fn(kind) used for 'scan' / 'match' cues.
 *   Under prefers-reduced-motion the static granted frame is shown for ~600ms.
 *   Pure Canvas2D, no dependencies. */
(() => {
  'use strict';
  const GRANT = 3.6;      // seconds until ACCESS GRANTED
  const BEAT = 0.75;      // hold on the granted state before onDone
  const RM_HOLD = 600;    // ms, reduced-motion static frame

  const hash = (a, b = 0) => { const x = Math.sin(a * 127.1 + b * 311.7) * 43758.5453; return x - Math.floor(x); };
  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
  const ease = x => 1 - Math.pow(1 - clamp(x), 3);
  const mix = (a, b, k) => a + (b - a) * k;
  const FONT = '"IBM Plex Mono", ui-monospace, Consolas, monospace';

  const CORE = [];
  for (let i = 0; i < 140; i++) { const y = 1 - (i / 139) * 2, r = Math.sqrt(1 - y * y), a = i * 2.399963; CORE.push([Math.cos(a) * r, y, Math.sin(a) * r]); }
  const RINGS = [
    { tilt: 1.15, roll: 0.35, r: 1.55, sp: 0.9, label: 'KPS 5/5 LOCKED', n: 3 },
    { tilt: 1.35, roll: -0.5, r: 1.8, sp: -0.6, label: 'EMBED 512-D', n: 4 },
    { tilt: 0.95, roll: 0.0, r: 2.05, sp: 0.4, label: 'LIVENESS 0.94', n: 5 },
  ];

  function draw(g, W, H, t, NAME) {
    const gr = clamp((t - GRANT) / 0.6), ge = ease(gr);
    const prog = clamp((t - 0.2) / (GRANT - 0.4));
    const C1 = gr > 0 ? [47, 209, 139] : [56, 189, 248], C2 = gr > 0 ? [127, 240, 191] : [45, 212, 191];
    const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
    g.clearRect(0, 0, W, H);
    const CX = W / 2, CY = H * 0.41, R = Math.min(W, H) * 0.17 * (1 + ge * 0.08);
    const rotY = t * (t < GRANT ? 0.9 : 0.35) + 0.4, rotX = -0.28;
    const P = (x, y, z) => { let c = Math.cos(rotY), s = Math.sin(rotY); const X = x * c + z * s; let Z = -x * s + z * c; c = Math.cos(rotX); s = Math.sin(rotX); const Y = y * c - Z * s; Z = y * s + Z * c; const k = 3.2 / (3.2 - Z); return [CX + X * R * k, CY + Y * R * k, Z]; };
    const scanY = Math.sin(t * 2.4) * 1.05;
    const band = y => Math.max(0, 1 - Math.abs(y - scanY) / 0.12);
    const narrow = W < 760;

    // projector base + beam
    const BY = H * 0.87;
    const beam = g.createLinearGradient(0, BY, 0, CY - R);
    beam.addColorStop(0, rgba(C1, .22)); beam.addColorStop(1, rgba(C1, 0));
    g.fillStyle = beam; g.beginPath(); g.moveTo(CX - R * 0.55, BY); g.lineTo(CX + R * 0.55, BY); g.lineTo(CX + R * 1.5, CY - R * 0.4); g.lineTo(CX - R * 1.5, CY - R * 0.4); g.closePath(); g.fill();
    for (let i = 0; i < 4; i++) { g.strokeStyle = rgba(C1, 0.5 - i * 0.1); g.lineWidth = i ? 1 : 2; g.beginPath(); g.ellipse(CX, BY, R * (0.6 + i * 0.28), R * (0.13 + i * 0.06), 0, 0, Math.PI * 2); g.stroke(); }
    g.setLineDash([4, 10]); g.lineDashOffset = -t * 40; g.strokeStyle = rgba(C2, .6); g.beginPath(); g.ellipse(CX, BY, R * 1.6, R * 0.36, 0, 0, Math.PI * 2); g.stroke(); g.setLineDash([]);

    // orbit rings
    const ringPts = RINGS.map((rg, ri) => {
      const tilt = mix(rg.tilt, 0.06 * ri, ge), roll = mix(rg.roll, 0, ge), r = rg.r * mix(1, 0.92 + ri * 0.04, ge), pts = [];
      for (let i = 0; i <= 96; i++) { const a = i / 96 * Math.PI * 2, x = Math.cos(a) * r, z = Math.sin(a) * r; const yy = -z * Math.sin(tilt), zz = z * Math.cos(tilt); pts.push([x * Math.cos(roll) - yy * Math.sin(roll), x * Math.sin(roll) + yy * Math.cos(roll), zz]); }
      return { rg, pts };
    });
    const drawRing = (front) => ringPts.forEach(({ rg, pts }, ri) => {
      g.lineWidth = 1;
      for (let i = 0; i < 96; i++) { const a = P(...pts[i]), b = P(...pts[i + 1]); if ((a[2] > 0) !== front) continue; g.strokeStyle = rgba(ri === 1 ? C2 : C1, front ? .55 : .18); g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.stroke(); }
      for (let k = 0; k < rg.n; k++) {
        const u = ((t * rg.sp * 0.25 + k / rg.n) % 1 + 1) % 1, p = P(...pts[Math.floor(u * 96)]);
        if ((p[2] > 0) !== front) continue;
        g.fillStyle = rgba(C2, front ? 1 : .35); g.shadowColor = rgba(C2, 1); g.shadowBlur = front ? 12 : 0;
        g.beginPath(); g.arc(p[0], p[1], front ? 3.2 : 2, 0, Math.PI * 2); g.fill(); g.shadowBlur = 0;
        if (front && k === 0) { g.font = '11px ' + FONT; g.fillStyle = 'rgba(207,239,255,.85)'; g.textAlign = 'left'; g.fillText(rg.label, p[0] + 10, p[1] - 8); g.strokeStyle = rgba(C2, .5); g.beginPath(); g.moveTo(p[0] + 4, p[1] - 4); g.lineTo(p[0] + 9, p[1] - 9); g.stroke(); }
      }
    });
    drawRing(false);

    // wireframe globe
    const sy = 1.12;
    for (let la = -5; la <= 5; la++) {
      const phi = la / 6 * Math.PI / 2, y = Math.sin(phi) * sy, rr = Math.cos(phi), b = band(y);
      let prev = null;
      for (let i = 0; i <= 64; i++) { const a = i / 64 * Math.PI * 2, p = P(Math.cos(a) * rr, -y, Math.sin(a) * rr); if (prev) { g.strokeStyle = b > 0 ? rgba([210, 250, 255], .35 + b * .65) : rgba(C1, p[2] > 0 ? .5 : .12); g.lineWidth = b > 0.5 ? 1.6 : 1; g.beginPath(); g.moveTo(prev[0], prev[1]); g.lineTo(p[0], p[1]); g.stroke(); } prev = p; }
    }
    for (let lo = 0; lo < 18; lo++) {
      const a = lo / 18 * Math.PI * 2; let prev = null;
      for (let i = 0; i <= 40; i++) { const phi = -Math.PI / 2 + i / 40 * Math.PI, y = Math.sin(phi) * sy, p = P(Math.cos(a) * Math.cos(phi), -y, Math.sin(a) * Math.cos(phi)); if (prev) { const b = band(y); g.strokeStyle = b > 0 ? rgba([210, 250, 255], .3 + b * .6) : rgba(C1, p[2] > 0 ? .4 : .1); g.lineWidth = 1; g.beginPath(); g.moveTo(prev[0], prev[1]); g.lineTo(p[0], p[1]); g.stroke(); } prev = p; }
    }
    // inner core
    const cr = 0.55 + Math.sin(t * 3) * 0.03;
    for (let i = 0; i < CORE.length; i++) { const [x, y, z] = CORE[i], a = -t * 1.6, X = x * Math.cos(a) + z * Math.sin(a), Z = -x * Math.sin(a) + z * Math.cos(a), p = P(X * cr, y * cr * sy, Z * cr); const on = hash(i, Math.floor(t * 6)) > 0.8; g.fillStyle = rgba(on ? C2 : C1, on ? .95 : (p[2] > 0 ? .55 : .2)); g.fillRect(p[0] - 1, p[1] - 1, on ? 2.5 : 1.6, on ? 2.5 : 1.6); }

    // laser slice
    if (gr < 1) {
      g.beginPath();
      for (let i = 0; i <= 64; i++) { const a = i / 64 * Math.PI * 2, rr = Math.sqrt(Math.max(0, 1 - (scanY / sy) ** 2)) * 1.25, p = P(Math.cos(a) * rr, -scanY, Math.sin(a) * rr); i ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1]); }
      g.fillStyle = rgba(C2, .08 * (1 - gr)); g.strokeStyle = rgba([200, 255, 245], .9 * (1 - gr)); g.lineWidth = 1.5; g.shadowColor = rgba(C2, 1); g.shadowBlur = 16;
      g.fill(); g.stroke(); g.shadowBlur = 0;
      if (!narrow) {
        const ey = P(0, -scanY, 0)[1];
        g.strokeStyle = rgba(C2, .4 * (1 - gr)); g.lineWidth = 1; g.setLineDash([2, 6]); g.beginPath(); g.moveTo(CX - R * 1.9, ey); g.lineTo(CX + R * 1.9, ey); g.stroke(); g.setLineDash([]);
        g.font = '11px ' + FONT; g.fillStyle = `rgba(207,239,255,${.8 * (1 - gr)})`; g.textAlign = 'left';
        g.fillText(`Y ${(scanY * 100).toFixed(1).padStart(6)}  DEPTH ${(0.9 + hash(Math.floor(t * 20)) * .09).toFixed(3)}`, CX + R * 1.95, ey + 4);
        g.textAlign = 'right'; g.fillText(`SLICE ${String(Math.floor(t * 37) % 256).padStart(3, '0')}/256`, CX - R * 1.95, ey + 4);
      }
    }
    drawRing(true);

    // progress gauge
    const GR = R * 1.6;
    g.lineWidth = 2; g.strokeStyle = rgba(C1, .12); g.beginPath(); g.arc(CX, CY, GR, -Math.PI / 2, Math.PI * 1.5); g.stroke();
    g.strokeStyle = rgba(C2, .9); g.shadowColor = rgba(C2, 1); g.shadowBlur = 10; g.beginPath(); g.arc(CX, CY, GR, -Math.PI / 2, -Math.PI / 2 + prog * Math.PI * 2); g.stroke(); g.shadowBlur = 0;
    for (let i = 0; i < 72; i++) { const a = i / 72 * Math.PI * 2 - Math.PI / 2, l = i % 6 ? 4 : 9, on = i / 72 < prog; g.strokeStyle = rgba(on ? C2 : C1, on ? .8 : .2); g.lineWidth = 1; g.beginPath(); g.moveTo(CX + Math.cos(a) * (GR + 6), CY + Math.sin(a) * (GR + 6)); g.lineTo(CX + Math.cos(a) * (GR + 6 + l), CY + Math.sin(a) * (GR + 6 + l)); g.stroke(); }

    // side readouts (hidden on narrow screens)
    if (!narrow) {
      g.font = '11px ' + FONT;
      const L = [['SUBJECT', t > 2.6 ? NAME : '?'.repeat(Math.min(10, NAME.length))], ['KPS', `${Math.min(5, Math.floor(prog * 6))}/5`], ['EMBED', `${Math.floor(prog * 512)}/512`], ['COSINE', (prog * 0.9873).toFixed(4)], ['LIVENESS', prog > .7 ? 'PASS' : 'CHECKING'], ['SPOOF', (0.4 - prog * 0.397).toFixed(3)]];
      L.forEach(([k, v], i) => { const y = CY - 60 + i * 22, a = clamp((t - 0.3 - i * 0.15) / 0.3); g.textAlign = 'left'; g.fillStyle = `rgba(95,115,135,${a})`; g.fillText(k, W * 0.07, y); g.fillStyle = `rgba(207,239,255,${a})`; g.fillText(v, W * 0.07 + 90, y); });
      for (let i = 0; i < 8; i++) { let s = ''; for (let j = 0; j < 16; j++) s += '0123456789abcdef'[(hash(i + Math.floor(t * 8), j) * 16) | 0]; g.textAlign = 'right'; g.fillStyle = rgba(C1, 0.15 + (i === Math.floor(t * 10) % 8 ? .6 : .25)); g.fillText(s, W * 0.93, CY - 70 + i * 20); }
    }

    // percent / granted
    g.textAlign = 'center';
    const ty = H * 0.755;
    if (t < GRANT) {
      g.font = `600 ${narrow ? 36 : 46}px ${FONT}`; g.fillStyle = '#e1f8ff'; g.shadowColor = rgba(C1, .8); g.shadowBlur = 18;
      g.fillText(String(Math.floor(prog * 100)).padStart(2, '0') + '%', CX, ty); g.shadowBlur = 0;
      g.font = '12px ' + FONT; g.fillStyle = rgba(C1, .9);
      const ph = prog < .3 ? 'MAPPING TOPOLOGY' : prog < .6 ? 'EXTRACTING LANDMARKS' : prog < .9 ? 'VERIFYING SIGNATURE' : 'FINALISING';
      g.fillText(ph.split('').join(' ') + (Math.floor(t * 3) % 2 ? ' _' : '  '), CX, ty + 26);
    } else {
      const d = t - GRANT;
      if (d < .5) { g.fillStyle = `rgba(200,255,225,${(1 - d / .5) * .35})`; g.fillRect(0, 0, W, H); g.strokeStyle = rgba(C1, 1 - d / .5); g.lineWidth = 3; g.beginPath(); g.arc(CX, CY, GR + d * W * .6, 0, Math.PI * 2); g.stroke(); }
      g.font = `600 ${Math.round(Math.min(W * .075, 64))}px ${FONT}`; g.fillStyle = `rgba(47,209,139,${clamp(d / .2)})`; g.shadowColor = 'rgba(47,209,139,.8)'; g.shadowBlur = 24;
      g.fillText('ACCESS GRANTED', CX, ty + 10); g.shadowBlur = 0;
      g.font = '12px ' + FONT; g.fillStyle = `rgba(191,245,220,${clamp((d - .3) / .3)})`;
      g.fillText(`WELCOME BACK, ${NAME}`.split('').join(' '), CX, ty + 40);
    }
  }

  let active = null;

  function play(opts = {}) {
    if (active) return active;
    const onDone = typeof opts.onDone === 'function' ? opts.onDone : () => {};
    const sfx = typeof opts.sfx === 'function' ? opts.sfx : () => {};
    const NAME = String(opts.name || 'OPERATOR').trim().toUpperCase().slice(0, 24) || 'OPERATOR';
    const rm = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    const freezeAt = typeof opts.freezeAt === 'number' ? opts.freezeAt : null; // testing/screenshots only

    const el = document.createElement('div');
    el.className = 'fll-overlay';
    el.setAttribute('role', 'status');
    el.innerHTML = '<canvas aria-hidden="true"></canvas><span class="fll-sr">Access granted. Loading FaceID…</span>'
      + '<span class="fll-hint" aria-hidden="true">Click or press any key to skip</span>';
    document.body.appendChild(el);
    try { document.fonts && document.fonts.load('600 46px ' + FONT); } catch (_) {}
    const cv = el.querySelector('canvas'), g = cv.getContext('2d');
    let W = 0, H = 0;
    const size = () => { const dpr = Math.min(2, window.devicePixelRatio || 1); W = window.innerWidth; H = window.innerHeight; cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); g.setTransform(dpr, 0, 0, dpr, 0, 0); };
    size();

    let raf = 0, timer = 0, finished = false, matched = false;
    const start = performance.now();
    const now = () => freezeAt != null ? freezeAt : rm ? GRANT + 1.5 : (performance.now() - start) / 1000;

    const finish = () => {
      if (finished) return; finished = true;
      cancelAnimationFrame(raf); clearTimeout(timer);
      window.removeEventListener('resize', onResize);
      window.removeEventListener('keydown', onKey, true);
      el.removeEventListener('pointerdown', finish);
      try { onDone(); } catch (e) { setTimeout(() => { throw e; }); }
    };
    const onKey = (e) => { if (!e.repeat) { e.preventDefault(); finish(); } };
    const onResize = () => { size(); if (rm || freezeAt != null) draw(g, W, H, now(), NAME); };
    window.addEventListener('resize', onResize);
    window.addEventListener('keydown', onKey, true);
    el.addEventListener('pointerdown', finish);

    const frame = () => {
      const t = now();
      if (!matched && t >= GRANT) { matched = true; sfx('match'); }
      draw(g, W, H, t, NAME);
      if (!finished && !rm && freezeAt == null) raf = requestAnimationFrame(frame);
    };
    requestAnimationFrame(() => el.classList.add('is-on'));
    if (rm) { el.classList.add('is-on'); }
    else if (freezeAt == null) setTimeout(() => { if (!finished && !matched) sfx('scan'); }, 1500);
    frame();
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { if (!finished && (rm || freezeAt != null)) draw(g, W, H, now(), NAME); });
    if (freezeAt == null) timer = setTimeout(finish, rm ? RM_HOLD : (GRANT + BEAT) * 1000);

    active = { skip: finish, el };
    return active;
  }

  window.FaceLoginLoader = { play, GRANT, BEAT };
})();
