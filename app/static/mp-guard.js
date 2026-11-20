/* Client side of app/antispam.py: form token, minimum fill time, hashcash proof-of-work, optional captcha.
   Self-contained SHA-256 (sync) so PoW also works on plain-http LAN hosts where crypto.subtle is unavailable. */
(() => {
  const K = new Uint32Array([0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,
    0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,
    0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,
    0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,
    0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]);
  const W = new Uint32Array(64);
  function sha256(bytes) {           // bytes: Uint8Array -> Uint32Array(8)
    const l = bytes.length, n = ((l + 9 + 63) >> 6) << 6, m = new Uint8Array(n);
    m.set(bytes); m[l] = 0x80; const bits = l * 8;
    m[n - 4] = bits >>> 24; m[n - 3] = bits >>> 16; m[n - 2] = bits >>> 8; m[n - 1] = bits;
    let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a, h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
    for (let o = 0; o < n; o += 64) {
      for (let i = 0; i < 16; i++) W[i] = (m[o + 4 * i] << 24) | (m[o + 4 * i + 1] << 16) | (m[o + 4 * i + 2] << 8) | m[o + 4 * i + 3];
      for (let i = 16; i < 64; i++) {
        const a = W[i - 15], b = W[i - 2];
        const s0 = ((a >>> 7) | (a << 25)) ^ ((a >>> 18) | (a << 14)) ^ (a >>> 3);
        const s1 = ((b >>> 17) | (b << 15)) ^ ((b >>> 19) | (b << 13)) ^ (b >>> 10);
        W[i] = (W[i - 16] + s0 + W[i - 7] + s1) | 0;
      }
      let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
      for (let i = 0; i < 64; i++) {
        const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        const t1 = (h + S1 + ((e & f) ^ (~e & g)) + K[i] + W[i]) | 0;
        const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        const t2 = (S0 + ((a & b) ^ (a & c) ^ (b & c))) | 0;
        h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      h0 = (h0 + a) | 0; h1 = (h1 + b) | 0; h2 = (h2 + c) | 0; h3 = (h3 + d) | 0;
      h4 = (h4 + e) | 0; h5 = (h5 + f) | 0; h6 = (h6 + g) | 0; h7 = (h7 + h) | 0;
    }
    return new Uint32Array([h0, h1, h2, h3, h4, h5, h6, h7]);
  }
  const enc = new TextEncoder();
  function leadingZeroBitsOk(hw, bits) {
    for (let i = 0; bits > 0; i++, bits -= 32) {
      const need = Math.min(32, bits);
      if (need === 32 ? hw[i] !== 0 : (hw[i] >>> (32 - need)) !== 0) return false;
    }
    return true;
  }
  async function solve(token, bits) {
    if (!bits) return '';
    for (let i = 0; ; i++) {
      if (leadingZeroBitsOk(sha256(enc.encode(token + ':' + i)), bits)) return String(i);
      if (i % 4000 === 3999) await new Promise((r) => setTimeout(r));  // keep the page responsive
    }
  }

  const state = new Map();  // form element -> {t, loadedAt, captchaId}
  async function prepare(formEl, form) {
    const r = await fetch('/api/mp/form-token?form=' + form, { credentials: 'same-origin' });
    const t = await r.json();
    const st = { t, loadedAt: Date.now() };
    state.set(formEl, st);
    if (t.captcha && !formEl.querySelector('.mp-captcha')) {
      const box = document.createElement('div'); box.className = 'mp-captcha'; formEl.querySelector('button.primary').before(box);
      const src = t.captcha.provider === 'turnstile' ? 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'
                                                     : 'https://js.hcaptcha.com/1/api.js?render=explicit';
      await new Promise((res) => { const s = document.createElement('script'); s.src = src; s.onload = res; document.head.append(s); });
      const api = t.captcha.provider === 'turnstile' ? window.turnstile : window.hcaptcha;
      st.captchaId = api.render(box, { sitekey: t.captcha.sitekey });
      st.captchaApi = api;
    }
    // proof of work starts in the background while the person fills the form
    st.pow = solve(t.token, t.pow_bits);
    return st;
  }
  async function fields(formEl, form) {
    let st = state.get(formEl) || await prepare(formEl, form);
    const wait = st.t.min_fill_seconds * 1000 - (Date.now() - st.loadedAt);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait + 50));
    const out = { form_token: st.t.token, pow_nonce: await st.pow };
    if (st.captchaApi) out.captcha_response = st.captchaApi.getResponse(st.captchaId) || '';
    state.delete(formEl);  // tokens are single use; next submit gets a fresh one
    setTimeout(() => prepare(formEl, form), 0);
    return out;
  }
  window.MPGuard = { prepare, fields, _sha256: sha256 };
})();
