(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const when = (t) => t ? new Date(t * 1000).toLocaleString() : '';
  let me = null;

  async function api(path, opts = {}) {
    const r = await fetch('/api/mp' + path, { credentials: 'same-origin', ...opts });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || r.statusText);
    return body;
  }
  const post = (path, data) => api(path, { method: "POST", body: toFD(data) });
  function toFD(o) { const f = new FormData(); Object.entries(o || {}).forEach(([k, v]) => f.append(k, v)); return f; }
  function msg(form, text, err) { const m = $('.mp-msg', form); if (m) { m.textContent = text; m.classList.toggle('err', !!err); } }

  // previews
  $$('.mp-drop input[type=file]').forEach((inp) => inp.addEventListener('change', () => {
    const p = $('.mp-prev', inp.parentElement); p.innerHTML = '';
    [...inp.files].slice(0, 10).forEach((f) => { const i = new Image(); i.src = URL.createObjectURL(f); p.append(i); });
  }));

  // routing
  const VIEWS = { sighting: null, self: null, login: null, case: ['family', 'admin'], queue: ['reviewer', 'admin'], admin: ['admin'] };
  function route() {
    let v = (location.hash || '#sighting').slice(1);
    if (!(v in VIEWS)) v = 'sighting';
    if (VIEWS[v] && (!me || !VIEWS[v].includes(me.role))) v = 'login';
    $$('.mp-view').forEach((s) => (s.hidden = s.id !== 'v-' + v));
    $$('#nav a').forEach((a) => a.classList.toggle('on', a.dataset.view === v));
    ({ case: loadCases, queue: loadQueue, admin: loadAdmin,
       sighting: () => MPGuard.prepare($('#f-sighting'), 'sighting'), self: () => MPGuard.prepare($('#f-self'), 'self') }[v] || (() => {}))();
  }
  async function refreshMe() {
    me = (await api('/me')).user;
    $('#who').textContent = me ? `${me.username} · ${me.role}` : '';
    $('#login-link').hidden = !!me; $('#logout').hidden = !me;
    $$('#nav a[data-role]').forEach((a) => (a.hidden = !me || !a.dataset.role.split(' ').includes(me.role)));
  }
  window.addEventListener('hashchange', route);

  // public forms
  $('#use-gps').onclick = () => navigator.geolocation && navigator.geolocation.getCurrentPosition((p) => {
    $('#f-sighting [name=lat]').value = p.coords.latitude.toFixed(5); $('#f-sighting [name=lon]').value = p.coords.longitude.toFixed(5);
  });
  $('#f-sighting').onsubmit = async (e) => {
    e.preventDefault(); const f = e.target; const fd = new FormData(f);
    ['lat', 'lon'].forEach((k) => { if (!fd.get(k)) fd.delete(k); });
    try { msg(f, 'Securing submission…'); Object.entries(await MPGuard.fields(f, 'sighting')).forEach(([k, v]) => fd.append(k, v)); msg(f, 'Uploading…'); const r = await api('/sightings', { method: 'POST', body: fd }); f.reset(); $('.mp-prev', f).innerHTML = ''; msg(f, r.message); }
    catch (err) { msg(f, err.message, true); }
  };
  $('#f-self').onsubmit = async (e) => {
    e.preventDefault(); const f = e.target; const fd = new FormData(f);
    if (!fd.get('file') || !fd.get('file').size) fd.delete('file');
    try { Object.entries(await MPGuard.fields(f, 'self')).forEach(([k, v]) => fd.append(k, v)); const r = await api('/self-report', { method: 'POST', body: fd }); f.reset(); $('.mp-prev', f).innerHTML = ''; msg(f, r.message); }
    catch (err) { msg(f, err.message, true); }
  };
  $('#f-login').onsubmit = async (e) => {
    e.preventDefault(); const f = e.target;
    try { const r = await api('/login', { method: 'POST', body: new FormData(f) }); await refreshMe();
      location.hash = { family: '#case', reviewer: '#queue', admin: '#admin' }[r.user.role]; route(); }
    catch (err) { msg(f, err.message, true); }
  };
  $('#f-register').onsubmit = async (e) => {
    e.preventDefault(); const f = e.target;
    try { await api('/register', { method: 'POST', body: new FormData(f) }); msg(f, 'Account created — sign in above.'); }
    catch (err) { msg(f, err.message, true); }
  };
  $('#logout').onclick = async () => { await api('/logout', { method: 'POST' }); await refreshMe(); location.hash = '#sighting'; route(); };

  // family
  $('#f-case').onsubmit = async (e) => {
    e.preventDefault(); const f = e.target;
    try { msg(f, 'Checking photos…'); const r = await api('/cases', { method: 'POST', body: new FormData(f) });
      f.reset(); $('.mp-prev', f).innerHTML = ''; msg(f, `Case ${r.case.ref} submitted. An administrator will verify it.`); loadCases(); }
    catch (err) { msg(f, err.message, true); }
  };
  async function loadCases() {
    if (!me || me.role !== 'family') { $('#my-cases').innerHTML = '<p class="muted small">Sign in with a family account to see your cases.</p>'; return; }
    const { cases } = await api('/my/cases');
    if (!cases.length) { $('#my-cases').innerHTML = '<p class="muted small">No cases yet.</p>'; return; }
    $('#my-cases').innerHTML = cases.map((c) => `
      <div class="mp-card" data-id="${c.id}">
        <h3>${esc(c.person_name)} <span class="mp-pill ${c.status}">${c.status}</span> <span class="mono small muted">${esc(c.ref)}</span></h3>
        <div class="mp-thumbs">${c.photos.map((p) => `<img src="${p}" alt="">`).join('')}</div>
        <div class="mp-meta"><span>Last seen <b>${esc(c.last_seen_text || '—')}</b></span><span>Confirmed sightings <b>${c.approved_sightings}</b></span></div>
        <div class="mp-updates small"></div>
        <div class="mp-actions">${c.approved_sightings ? '<button class="btn small" data-act="updates">Show confirmed sightings</button>' : ''}
          ${c.status !== 'closed' ? '<button class="btn small ghost" data-act="close">Close case (found)</button>' : ''}</div>
      </div>`).join('');
  }
  $('#my-cases').onclick = async (e) => {
    const b = e.target.closest('button'); if (!b) return; const card = b.closest('.mp-card'); const id = card.dataset.id;
    if (b.dataset.act === 'close' && confirm('Close this case? Matching stops and face data is deleted.')) { await post(`/my/cases/${id}/close`, { reason: 'found' }); loadCases(); }
    if (b.dataset.act === 'updates') {
      const r = await api(`/my/cases/${id}/updates`);
      $('.mp-updates', card).innerHTML = r.message ? `<p class="muted">${esc(r.message)}</p>` : r.updates.map((u) => `
        <div class="mp-side" style="grid-template-columns:96px 200px 1fr"><img src="${u.face}" alt="Matched face"><img src="${u.photo}" alt="Sighting photo (other people blurred)">
          <div class="mp-meta"><span>Where <b>${esc(u.location_text || '—')}</b></span>
          ${u.lat != null ? `<span>GPS <b>${u.lat.toFixed(4)}, ${u.lon.toFixed(4)}</b></span>` : ''}
          <span>Seen <b>${esc(u.seen_at || '—')}</b></span><span>Confirmed <b>${when(u.approved_at)}</b></span></div></div>`).join('');
    }
  };

  // reviewer
  function groupPhoto(m) {
    const ar = m.width && m.height ? `aspect-ratio:${m.width}/${m.height}` : '';
    const box = (f) => f.bbox ? `<span class="mp-face ${f.matched ? 'hit' : ''}" style="left:${f.bbox[0] * 100}%;top:${f.bbox[1] * 100}%;width:${(f.bbox[2] - f.bbox[0]) * 100}%;height:${(f.bbox[3] - f.bbox[1]) * 100}%">${f.matched ? '<b>MATCH</b>' : ''}</span>` : '';
    return `<div class="mp-photo" style="${ar}"><img src="${m.sighting_photo}" alt="">${(m.faces || []).map(box).join('')}</div>`;
  }
  async function loadQueue() {
    const q = await api('/review/queue');
    $('#q-meta').textContent = `${q.matches.length} matches · ${q.self_reports.length} self-reports · threshold ${q.threshold}`;
    $('#q-self').innerHTML = q.self_reports.map((r) => `
      <div class="mp-card" data-sr="${r.id}">
        <h3>Self-report · ${esc(r.person_name)} <span class="mp-pill hold">case on hold</span> <span class="mono small muted">${esc(r.ref)}</span></h3>
        <div class="mp-side"><figure><figcaption>Case photo</figcaption><img src="${r.case_photos[0] || ''}" alt=""></figure>
          <div class="mp-score"><b>${r.score != null ? r.score.toFixed(2) : '—'}</b><span>SIMILARITY</span></div>
          <figure><figcaption>Self-report photo</figcaption>${r.photo ? `<img src="${r.photo}" alt="">` : '<p class="muted small">No photo provided</p>'}</figure></div>
        <p class="small">“${esc(r.message || 'No message')}”</p>
        <div class="mp-actions"><button class="btn ok" data-d="confirm">Confirm — close case, share nothing</button><button class="btn bad" data-d="reject">Reject</button></div>
      </div>`).join('');
    $('#q-matches').innerHTML = q.matches.length ? q.matches.map((m) => `
      <div class="mp-card" data-m="${m.id}">
        <h3>${esc(m.person_name)} <span class="mono small muted">${esc(m.ref)} · match #${m.id}</span>
          ${m.on_hold ? '<span class="mp-pill hold">on hold · self-report pending</span>' : '<span class="mp-pill pending">awaiting review</span>'}
          ${m.duplicates ? `<span class="mp-pill">+${m.duplicates} near-duplicate${m.duplicates > 1 ? 's' : ''}</span>` : ''}</h3>
        <div class="mp-side">
          <figure><figcaption>Case photo${m.case_photos.length > 1 ? 's' : ''}</figcaption><img src="${m.case_photos[0]}" alt="">
            ${m.case_photos.length > 1 ? `<div class="mp-thumbs">${m.case_photos.slice(1).map((p) => `<img src="${p}" alt="">`).join('')}</div>` : ''}</figure>
          <div class="mp-score"><b>${m.score.toFixed(2)}</b><span>SIMILARITY</span></div>
          <figure><figcaption>Submitted sighting${m.face_count > 1 ? ` · group photo, ${m.face_count} faces` : ''}</figcaption>
            ${groupPhoto(m)}
            ${m.face_count > 1 ? `<div class="mp-crop"><img src="${m.face_crop}" alt="Matched face"><span class="small muted">matched face · other faces are blurred for the family</span></div>` : ''}</figure>
        </div>
        <div class="mp-meta"><span>Sighting at <b>${esc(m.location_text || '—')}</b></span>
          ${m.lat != null ? `<span>GPS <b>${m.lat.toFixed(4)}, ${m.lon.toFixed(4)}</b></span>` : ''}
          <span>Seen <b>${esc(m.seen_at || when(m.created_at))}</b></span><span>Case last seen <b>${esc(m.last_seen_text || '—')}</b></span>
          ${m.note ? `<span>Note <b>${esc(m.note)}</b></span>` : ''}</div>
        <div class="mp-actions"><button class="btn ok" data-d="approve" ${m.on_hold ? 'disabled' : ''}>Approve — release to family</button>
          <button class="btn bad" data-d="reject">Reject</button><input name="note" placeholder="review note (optional)" size="28"></div>
      </div>`).join('') : '<p class="muted small">Queue is empty. New candidate matches appear here automatically.</p>';
  }
  $('#v-queue').onclick = async (e) => {
    const b = e.target.closest('button[data-d]'); if (!b) return; const card = b.closest('.mp-card');
    const note = ($('input[name=note]', card) || {}).value || '';
    try {
      if (card.dataset.m) await post(`/review/matches/${card.dataset.m}`, { decision: b.dataset.d, note });
      else await post(`/review/self-reports/${card.dataset.sr}`, { decision: b.dataset.d, note });
      loadQueue();
    } catch (err) { alert(err.message); }
  };

  // admin
  let adminStatus = 'pending';
  $$('.mp-tabs button').forEach((b) => (b.onclick = () => { adminStatus = b.dataset.st; $$('.mp-tabs button').forEach((x) => x.classList.toggle('on', x === b)); loadAdmin(); }));
  async function loadAdmin() {
    const [{ cases }, { users }, { audit }] = await Promise.all([api('/admin/cases?status=' + adminStatus), api('/admin/users'), api('/admin/audit?limit=60')]);
    $('#a-cases').innerHTML = cases.length ? cases.map((c) => `
      <div class="mp-card" data-c="${c.id}">
        <h3>${esc(c.person_name)} <span class="mp-pill ${c.status}">${c.status}</span> <span class="mono small muted">${esc(c.ref)}</span></h3>
        <div class="mp-thumbs">${c.photos.map((p) => `<img src="${p}" alt="">`).join('')}</div>
        <div class="mp-meta"><span>Owner <b>${esc(c.owner)}</b> ${esc(c.owner_contact || '')}</span><span>Last seen <b>${esc(c.last_seen_text || '—')} ${esc(c.last_seen_at || '')}</b></span>
          <span>Filed <b>${when(c.created_at)}</b></span>${c.verification_ref ? `<span>Ref <b>${esc(c.verification_ref)}</b></span>` : ''}</div>
        <p class="small muted">${esc(c.description || '')}</p>
        ${c.status === 'pending' ? `<div class="mp-actions"><input name="vref" placeholder="Police report / verification ref" size="32">
          <button class="btn ok" data-a="verify">Verify &amp; go live</button><button class="btn bad" data-a="reject">Reject</button></div>`
        : c.status === 'verified' ? '<div class="mp-actions"><button class="btn ghost small" data-a="close">Close case</button></div>' : ''}
      </div>`).join('') : `<p class="muted small">No ${adminStatus} cases.</p>`;
    $('#a-users').innerHTML = users.map((u) => `<div>${esc(u.username)} <span class="mp-pill">${u.role}</span> ${u.active ? '' : '<span class="mp-pill rejected">inactive</span>'}</div>`).join('');
    $('#a-audit').innerHTML = audit.map((a) => `<div>${new Date(a.ts * 1000).toLocaleTimeString()} <b>${esc(a.action)}</b> ${esc(a.actor || a.actor_role)} ${a.target_type ? esc(a.target_type + '#' + (a.target_id ?? '')) : ''}</div>`).join('');
  }
  $('#a-cases').onclick = async (e) => {
    const b = e.target.closest('button[data-a]'); if (!b) return; const card = b.closest('.mp-card'); const id = card.dataset.c;
    try {
      if (b.dataset.a === 'verify') { const r = await post(`/admin/cases/${id}/verify`, { verification_ref: $('input[name=vref]', card).value }); if (r.new_matches) alert(`${r.new_matches} candidate match(es) sent to the review queue`); }
      if (b.dataset.a === 'reject') await post(`/admin/cases/${id}/reject`, {});
      if (b.dataset.a === 'close' && confirm('Close case and delete its face data?')) await post(`/admin/cases/${id}/close`, { reason: 'other' });
      loadAdmin();
    } catch (err) { alert(err.message); }
  };
  $('#f-user').onsubmit = async (e) => { e.preventDefault(); const f = e.target;
    try { await api('/admin/users', { method: 'POST', body: new FormData(f) }); f.reset(); msg(f, ''); loadAdmin(); } catch (err) { msg(f, err.message, true); } };
  $('#purge').onclick = async () => { const r = await post('/admin/purge', {}); $('#purge-msg').textContent = `${r.deleted} expired sighting(s) deleted (retention ${r.retention_days} days)`; loadAdmin(); };

  refreshMe().then(route);
})();
