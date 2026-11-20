/* FaceID HUD sound effects — tiny Web Audio synth, no audio files.
 * API: window.FaceSfx = { play(kind), toggle(), muted }
 *   kind: 'match' | 'unknown' | 'ambiguous' | 'scan' (others ignored)
 * Mute persisted in localStorage 'faceid_sfx_muted' ('1' | '0').
 */
(function () {
  'use strict';
  var KEY = 'faceid_sfx_muted';
  var AC = window.AudioContext || window.webkitAudioContext;
  var ctx = null;
  var master = null;
  var muted = false;
  var gestured = false; // never create/resume audio before a real user gesture

  try { muted = window.localStorage.getItem(KEY) === '1'; } catch (_) {}

  function ensureCtx() {
    if (!AC || !gestured) return null;
    try {
      if (!ctx) {
        ctx = new AC();
        master = ctx.createGain();
        master.gain.value = 0.18; // keep it subtle
        master.connect(ctx.destination);
      }
      if (ctx.state === 'suspended' && ctx.resume) ctx.resume().catch(function () {});
      return ctx;
    } catch (_) { ctx = null; return null; }
  }

  // Lazily unlock audio on the first user gesture (autoplay policies).
  function onGesture() {
    gestured = true;
    ensureCtx();
    if (ctx && ctx.state === 'running') {
      window.removeEventListener('pointerdown', onGesture, true);
      window.removeEventListener('keydown', onGesture, true);
    }
  }
  window.addEventListener('pointerdown', onGesture, true);
  window.addEventListener('keydown', onGesture, true);

  // One enveloped oscillator note. t0/dur in seconds relative to now.
  function tone(opts) {
    var c = ctx, now = c.currentTime;
    var t0 = now + (opts.at || 0), dur = opts.dur, peak = opts.gain == null ? 1 : opts.gain;
    var osc = c.createOscillator(), g = c.createGain();
    osc.type = opts.type || 'sine';
    osc.frequency.setValueAtTime(opts.f, t0);
    if (opts.f2) osc.frequency.exponentialRampToValueAtTime(opts.f2, t0 + dur);
    if (opts.detune) osc.detune.setValueAtTime(opts.detune, t0);
    var dest = master;
    if (opts.lp) {
      var flt = c.createBiquadFilter();
      flt.type = 'lowpass'; flt.frequency.value = opts.lp;
      flt.connect(master); dest = flt;
    }
    if (opts.wobble) { // vibrato LFO
      var lfo = c.createOscillator(), lg = c.createGain();
      lfo.frequency.value = opts.wobble; lg.gain.value = opts.wobbleDepth || 12;
      lfo.connect(lg); lg.connect(osc.frequency);
      lfo.start(t0); lfo.stop(t0 + dur + 0.02);
    }
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(peak, t0 + Math.min(0.012, dur / 4));
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(g); g.connect(dest);
    osc.start(t0); osc.stop(t0 + dur + 0.02);
  }

  var SOUNDS = {
    // bright rising two-note chime (~260ms)
    match: function () {
      tone({ f: 880, dur: 0.12, type: 'triangle', gain: 0.9 });
      tone({ f: 1318.5, dur: 0.16, at: 0.09, type: 'triangle', gain: 0.9 });
      tone({ f: 2637, dur: 0.14, at: 0.09, type: 'sine', gain: 0.15 });
    },
    // low soft descending buzz (~240ms)
    unknown: function () {
      tone({ f: 220, f2: 140, dur: 0.24, type: 'sawtooth', gain: 0.5, lp: 900 });
      tone({ f: 110, f2: 80, dur: 0.24, type: 'sine', gain: 0.5 });
    },
    // mid wobble, two equal tones (~250ms)
    ambiguous: function () {
      tone({ f: 523.25, dur: 0.1, type: 'sine', gain: 0.7, wobble: 18, wobbleDepth: 10 });
      tone({ f: 523.25, dur: 0.12, at: 0.13, type: 'sine', gain: 0.7, wobble: 18, wobbleDepth: 10 });
    },
    // quick soft sweep/blip (~140ms)
    scan: function () {
      tone({ f: 600, f2: 1800, dur: 0.14, type: 'sine', gain: 0.35, lp: 3000 });
    }
  };

  function play(kind) {
    try {
      if (muted || !Object.prototype.hasOwnProperty.call(SOUNDS, kind)) return;
      if (!ensureCtx()) return;
      SOUNDS[kind]();
    } catch (_) { /* never throw */ }
  }

  function syncButton() {
    try {
      var b = document.getElementById('sfx-toggle');
      if (!b) return;
      b.textContent = muted ? '🔇' : '🔊';
      b.setAttribute('aria-pressed', muted ? 'true' : 'false');
      var label = muted ? 'Sound effects off (click to unmute)' : 'Sound effects on (click to mute)';
      b.title = label;
      b.setAttribute('aria-label', label);
    } catch (_) {}
  }

  function toggle() {
    muted = !muted;
    try { window.localStorage.setItem(KEY, muted ? '1' : '0'); } catch (_) {}
    if (!muted) ensureCtx(); // toggle is usually a user gesture: unlock audio
    syncButton();
    return muted;
  }

  var api = { play: play, toggle: toggle };
  Object.defineProperty(api, 'muted', { enumerable: true, get: function () { return muted; } });
  window.FaceSfx = api;

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', syncButton);
  else syncButton();
})();
