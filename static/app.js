// Timelapse Cutter — browser UI. Times are in seconds of *video* on a shared
// master timeline; real time = video time × interval × fps.
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const TRACKS = ['A', 'B'];

let S = null;            // project state (mirrors project.json)
let AN = {};             // analysis status per clip id
let PROJECT_DIR = '';
const DIFFS = {};        // clip id -> { smooth: Float32Array, thumbEvery, nThumbs }
const THUMBS = new Map();
let THR = { A: 1, B: 1 };

const ui = {
  t: 0, playing: false, speed: 1, skipCuts: true,
  sel: null, selCut: -1, selClip: null,
  view: { start: 0, pps: 20 },
  lastFrame: 0,
};
let undoStack = [], redoStack = [];
let segCache = null;

// ------------------------------------------------------------------ utils

const api = async (path, body) => {
  const r = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  return r.json();
};
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const fps = () => S.settings.fps || 30;
const ratio = () => (S.settings.interval || 2) * fps();
const frameRound = (t) => Math.round(t * fps()) / fps();

function fmtVideo(t) {
  t = Math.max(0, t);
  const f = Math.floor((t % 1) * fps() + 1e-6);
  const s = Math.floor(t) % 60, m = Math.floor(t / 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}:${String(f).padStart(2, '0')}`;
}
function fmtDur(sec) {
  sec = Math.round(Math.max(0, sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
  if (h) return `${h}h${String(m).padStart(2, '0')}`;
  if (m) return `${m}m${String(s).padStart(2, '0')}`;
  return `${s}s`;
}
const fmtReal = (t) => fmtDur(t * ratio());

function mergeRanges(list) {
  const r = list.filter((c) => c.end - c.start > 1e-4).map((c) => ({ ...c })).sort((a, b) => a.start - b.start);
  const out = [];
  for (const c of r) {
    const last = out[out.length - 1];
    if (last && c.start <= last.end + 1e-6) {
      last.end = Math.max(last.end, c.end);
      if ('auto' in last) last.auto = last.auto && c.auto;
    } else out.push(c);
  }
  return out;
}
function subtractRange(list, s, e) {
  const out = [];
  for (const c of list) {
    if (c.end <= s || c.start >= e) { out.push(c); continue; }
    if (c.start < s) out.push({ ...c, end: s });
    if (c.end > e) out.push({ ...c, start: e });
  }
  return out;
}
const rangeAt = (list, t) => list.findIndex((c) => t >= c.start && t < c.end);
// Manual and auto cuts are merged separately so re-detection only replaces auto cuts.
const normCuts = (list) => [...mergeRanges(list.filter((c) => !c.auto)), ...mergeRanges(list.filter((c) => c.auto))];

// ------------------------------------------------------------------ model

const clipsOf = (track) => S.clips.filter((c) => c.track === track);
const clipEnd = (c) => c.start + c.duration;
const timelineEnd = () => Math.max(1, ...S.clips.map(clipEnd));

function coverage(track, t) {
  if (rangeAt(S.hidden[track] || [], t) >= 0) return null;
  for (let i = S.clips.length - 1; i >= 0; i--) {
    const c = S.clips[i];
    if (c.track === track && t >= c.start && t < clipEnd(c)) return c;
  }
  return null;
}

function layoutAt(t) {
  const o = S.overrides.find((r) => t >= r.start && t < r.end);
  return o ? { ...S.layout, mode: o.mode, ...(o.main ? { main: o.main } : {}) } : S.layout;
}
const layoutLabel = (o) => ({ pip: `PiP ${o.main} main`, sbs: 'side by side', stack: 'stacked', blend: 'superpose', A: 'A only', B: 'B only' })[o.mode];

function changed(redetect = true) {
  segCache = null; viewMap = null;
  drawDirty = true;
  updateStats();
  scheduleSave();
  if (redetect) scheduleDetect();
}

let detectTimer = null;
function scheduleDetect() {
  if (!S.detect.auto) return;
  clearTimeout(detectTimer);
  detectTimer = setTimeout(() => detectStill(false), 150);
}

// Kept parts of the master timeline, split wherever the visible clips change.
function segments() {
  if (segCache) return segCache;
  const end = timelineEnd();
  const cuts = mergeRanges(S.cuts);
  const kept = [];
  let t = 0;
  for (const c of cuts) {
    if (c.start > t) kept.push([t, Math.min(c.start, end)]);
    t = Math.max(t, c.end);
  }
  if (t < end) kept.push([t, end]);
  const bps = new Set();
  for (const c of S.clips) { bps.add(c.start); bps.add(clipEnd(c)); }
  for (const tr of TRACKS) for (const h of S.hidden[tr] || []) { bps.add(h.start); bps.add(h.end); }
  for (const o of S.overrides) { bps.add(o.start); bps.add(o.end); }
  const sorted = [...bps].sort((a, b) => a - b);
  const segs = [];
  const minDur = 0.5 / fps();
  for (const [a, b] of kept) {
    const pts = [a, ...sorted.filter((x) => x > a && x < b), b];
    for (let i = 0; i < pts.length - 1; i++) {
      const s0 = pts[i], s1 = pts[i + 1];
      if (s1 - s0 < minDur) continue;
      const m = (s0 + s1) / 2;
      const ca = coverage('A', m), cb = coverage('B', m);
      if (!ca && !cb) continue;
      segs.push({
        start: s0, end: s1, dur: s1 - s0,
        a: ca ? { clip: ca.id, t: s0 - ca.start } : null,
        b: cb ? { clip: cb.id, t: s0 - cb.start } : null,
        layout: layoutAt(m),
      });
    }
  }
  // Output length in frames, and the cross-dissolve with the previous piece:
  // "session" when a camera switches to another clip, "cut" when time jumps.
  const f = fps(), speed = S.output.speed || 1, T = S.transitions || { session: 0, cut: 0 };
  for (const sg of segs) sg.frames = Math.round((sg.dur * f) / speed);
  const out = segs.filter((sg) => sg.frames >= 1);
  out.forEach((sg, i) => {
    sg.kind = null; sg.fade = 0;
    if (!i) return;
    const p = out[i - 1];
    const session = ['a', 'b'].some((k) => p[k] && sg[k] && p[k].clip !== sg[k].clip);
    const relayout = p.layout.mode !== sg.layout.mode || p.layout.main !== sg.layout.main;
    sg.kind = session ? 'session' : Math.abs(p.end - sg.start) > 1e-6 ? 'cut' : relayout ? 'layout' : null;
    const want = sg.kind === 'session' ? T.session : T.cut;   // layout changes use the "cut" length
    if (sg.kind) sg.fade = Math.max(0, Math.min(want || 0, Math.floor(p.frames / 2), Math.floor(sg.frames / 2)));
  });
  return (segCache = out);
}

const outputSeconds = () => segments().reduce((a, sg) => a + sg.frames - sg.fade, 0) / fps();

function updateStats() {
  const segs = segments();
  const kept = segs.reduce((a, s) => a + s.dur, 0);
  const total = timelineEnd();
  $('#forgetKeepsBtn').hidden = !S.keeps.length;
  renderTransitions();
  $('#stats').innerHTML = S.clips.length
    ? `Timeline <b>${fmtDur(total)}</b> (${fmtReal(total)} real) → output <b>${fmtDur(outputSeconds())}</b>
       · cut ${total ? Math.round(100 * (1 - kept / total)) : 0}%`
    : '';
}

// ------------------------------------------------------------------ undo / save

const snapshot = () => JSON.stringify({
  clips: S.clips.map((c) => [c.id, c.start, c.track]), cuts: S.cuts, keeps: S.keeps, hidden: S.hidden,
  splits: S.splits, overrides: S.overrides,
});
function restore(snap) {
  const o = JSON.parse(snap);
  const m = new Map(o.clips.map(([id, start, track]) => [id, { start, track }]));
  for (const c of S.clips) if (m.has(c.id)) Object.assign(c, m.get(c.id));
  S.cuts = o.cuts; S.keeps = o.keeps || []; S.hidden = o.hidden;
  S.splits = o.splits || []; S.overrides = o.overrides || [];
  ui.selCut = -1;
  changed(false);
}
function pushUndo(snap = snapshot()) {
  undoStack.push(snap);
  if (undoStack.length > 200) undoStack.shift();
  redoStack = [];
}
function undo() { if (undoStack.length) { redoStack.push(snapshot()); restore(undoStack.pop()); } }
function redo() { if (redoStack.length) { undoStack.push(snapshot()); restore(redoStack.pop()); } }

let saveTimer = null;
function scheduleSave() {
  $('#saveState').textContent = 'Saving…';
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    await api('/api/state', {
      clips: S.clips.map((c) => ({ id: c.id, start: c.start, track: c.track })),
      cuts: S.cuts, keeps: S.keeps, hidden: S.hidden, layout: S.layout, detect: S.detect,
      transitions: S.transitions, splits: S.splits, overrides: S.overrides,
      output: S.output, settings: S.settings, tracks: S.tracks,
    });
    $('#saveState').textContent = 'Saved';
  }, 500);
}

// ------------------------------------------------------------------ analysis data

async function loadDiffs(id) {
  if (DIFFS[id]) return;
  DIFFS[id] = { loading: true };
  const d = await api(`/api/diffs/${id}`).catch(() => null);
  if (!d || !d.diffs) { delete DIFFS[id]; return; }
  const raw = d.diffs, n = raw.length;
  const sm = new Float32Array(n);
  for (let i = 0; i < n; i++) {   // median of 3 kills single-frame noise spikes
    const a = raw[Math.max(0, i - 1)], b = raw[i], c = raw[Math.min(n - 1, i + 1)];
    sm[i] = Math.max(Math.min(a, b), Math.min(Math.max(a, b), c));
  }
  sm[0] = n > 1 ? sm[1] : 0;
  DIFFS[id] = { smooth: sm, thumbEvery: d.thumbEvery, nThumbs: Math.ceil(n / d.thumbEvery) };
  for (const k of [...THUMBS.keys()]) if (k.startsWith(id) && THUMBS.get(k).failed) THUMBS.delete(k);
  computeThresholds();
  drawDirty = true;
  scheduleDetect();
}

function computeThresholds() {
  for (const tr of TRACKS) {
    const vals = [];
    for (const c of clipsOf(tr)) {
      const d = DIFFS[c.id];
      if (d && d.smooth) for (let i = 0; i < d.smooth.length; i += 1) vals.push(d.smooth[i]);
    }
    if (!vals.length) { THR[tr] = 1; continue; }
    vals.sort((a, b) => a - b);
    const floor = vals[Math.floor(vals.length * 0.1)];
    THR[tr] = Math.max(floor * S.detect.sensitivity, 0.15 * S.detect.sensitivity);
  }
}

// manual = run from a user action (undoable); otherwise automatic re-detection.
function detectStill(manual = true) {
  const f = fps(), end = timelineEnd(), n = Math.ceil(end * f);
  const pending = S.clips.filter((c) => !(DIFFS[c.id] && DIFFS[c.id].smooth));
  if (pending.length) {
    $('#detectInfo').textContent = `Waiting for the analysis of ${pending.length} clip(s)…`;
    return;
  }
  const minStill = (S.detect.minStillMin * 60) / ratio();
  const pad = S.detect.padSec / ratio();
  const still = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const t = (i + 0.5) / f;
    let active = false;
    for (const tr of TRACKS) {
      const c = coverage(tr, t);
      if (!c) continue;
      const d = DIFFS[c.id];
      const v = d.smooth[clamp(Math.floor((t - c.start) * f), 0, d.smooth.length - 1)];
      if (v > THR[tr]) { active = true; break; }
    }
    still[i] = active ? 0 : 1;
  }
  const auto = [];
  let i = 0;
  while (i < n) {
    if (!still[i]) { i++; continue; }
    let j = i;
    while (j < n && still[j]) j++;
    const s = i / f, e = Math.min(end, j / f);
    if (e - s >= minStill) {
      const cs = i === 0 ? 0 : s + pad, ce = j >= n ? end : e - pad;
      if (ce - cs > 1 / f) auto.push({ start: cs, end: ce, auto: true });
    }
    i = j;
  }
  // Parts the user restored stay restored.
  let kept = auto;
  for (const k of S.keeps) kept = subtractRange(kept, k.start, k.end);
  kept = kept.filter((c) => c.end - c.start > 1 / f);
  const removed = kept.reduce((a, c) => a + c.end - c.start, 0);
  $('#detectInfo').textContent = `${kept.length} still part(s) cut, ${fmtDur(removed)} of video (${fmtReal(removed)} real).`;
  const old = S.cuts.filter((c) => c.auto);
  if (JSON.stringify(old) === JSON.stringify(kept)) return;
  if (manual) pushUndo();
  // Manual cuts stay; auto cuts are replaced.
  S.cuts = normCuts([...S.cuts.filter((c) => !c.auto), ...kept]);
  ui.selCut = -1;
  changed(false);
}

function removeCut(i) {
  pushUndo();
  const [c] = S.cuts.splice(i, 1);
  if (c.auto) S.keeps = mergeRanges([...S.keeps, { start: c.start, end: c.end }]);
  ui.selCut = -1;
  changed();
}

// ------------------------------------------------------------------ preview

const vids = { A: $('#vA'), B: $('#vB') };

function fitStage() {
  const wrap = $('#stageWrap'), st = $('#stage');
  const W = wrap.clientWidth - 24, H = wrap.clientHeight - 24;
  const ar = S ? S.output.width / S.output.height : 16 / 9;
  let w = W, h = W / ar;
  if (h > H) { h = H; w = H * ar; }
  st.style.width = `${Math.max(0, w)}px`;
  st.style.height = `${Math.max(0, h)}px`;
}

function place(v, x, y, w, h, z, opacity = 1, fit = 'contain', border = 0) {
  Object.assign(v.style, {
    display: 'block', left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%`,
    zIndex: z, opacity, objectFit: fit, outline: border ? `${border}px solid #fff` : 'none',
  });
}

function layoutStage(ca, cb) {
  const L = layoutAt(ui.t);
  const fit = L.fill ? 'cover' : 'contain';
  let show = [ca && 'A', cb && 'B'].filter(Boolean);
  if ((L.mode === 'A' || L.mode === 'B') && show.includes(L.mode)) show = [L.mode];
  for (const tr of TRACKS) if (!show.includes(tr)) vids[tr].style.display = 'none';
  if (show.length === 1) return place(vids[show[0]], 0, 0, 1, 1, 1, 1, fit);
  if (show.length === 0) return;
  const st = $('#stage');
  const scale = st.clientWidth / S.output.width;
  if (L.mode === 'sbs') { place(vids.A, 0, 0, 0.5, 1, 1, 1, fit); place(vids.B, 0.5, 0, 0.5, 1, 1, 1, fit); }
  else if (L.mode === 'stack') { place(vids.A, 0, 0, 1, 0.5, 1, 1, fit); place(vids.B, 0, 0.5, 1, 0.5, 1, 1, fit); }
  else if (L.mode === 'blend') { place(vids.A, 0, 0, 1, 1, 1, 1, fit); place(vids.B, 0, 0, 1, 1, 2, L.opacity, fit); }
  else {
    const main = L.main, inset = main === 'A' ? 'B' : 'A', sz = L.size;
    const m = (0.03 * Math.min(S.output.width, S.output.height));
    const mx = m / S.output.width, my = m / S.output.height;
    const x = L.corner.includes('l') ? mx : 1 - sz - mx;
    const y = L.corner.includes('t') ? my : 1 - sz - my;
    place(vids[main], 0, 0, 1, 1, 1, 1, fit);
    place(vids[inset], x, y, sz, sz, 2, 1, 'cover', Math.max(0, Math.round(L.border * scale)));
  }
}

function syncVideo(tr, clip) {
  const v = vids[tr];
  if (!clip) { if (!v.paused) v.pause(); return; }
  if (v._clip !== clip.id) {
    v._clip = clip.id; v._ready = false;
    v.src = `/media/${clip.id}`;
    v.onloadedmetadata = () => { v._ready = true; };
  }
  if (!v._ready) return;
  const want = clamp(ui.t - clip.start, 0, Math.max(0, (v.duration || clip.duration) - 0.001));
  if (ui.playing) {
    if (v.playbackRate !== ui.speed) v.playbackRate = ui.speed;
    if (v.paused) v.play().catch(() => {});
    if (Math.abs(v.currentTime - want) > 0.25 * Math.max(1, ui.speed)) v.currentTime = want;
  } else {
    if (!v.paused) v.pause();
    if (!v.seeking && Math.abs(v.currentTime - want) > 0.5 / fps()) v.currentTime = want;
  }
}

function nextCovered(t) {
  const starts = S.clips.map((c) => c.start).filter((s) => s > t).sort((a, b) => a - b);
  for (const s of starts) if (coverage('A', s + 1e-4) || coverage('B', s + 1e-4)) return s;
  return null;
}

function tick(now) {
  const dt = ui.lastFrame ? (now - ui.lastFrame) / 1000 : 0;
  ui.lastFrame = now;
  if (S) {
    const before = ui.t;
    if (ui.playing) {
      // Follow a playing video's own clock when possible (smooth, no drift);
      // fall back to wall-clock time over gaps, cuts and while loading.
      let next = ui.t + Math.min(dt, 0.25) * ui.speed;
      for (const tr of TRACKS) {
        const c = coverage(tr, ui.t), v = vids[tr];
        if (c && v._clip === c.id && !v.paused && !v.seeking && !v.ended && v.readyState >= 3) {
          const vt = c.start + v.currentTime;
          if (vt >= ui.t - 0.05 && vt <= ui.t + 0.5 * ui.speed) { next = Math.max(ui.t, vt); break; }
        }
      }
      ui.t = next;
      if (ui.skipCuts || ui.compact) {
        for (let k = 0; k < 4; k++) {
          const ci = rangeAt(S.cuts, ui.t);
          if (ci >= 0) { ui.t = S.cuts[ci].end; continue; }
          if (!coverage('A', ui.t) && !coverage('B', ui.t)) {
            const n = nextCovered(ui.t);
            if (n === null) break;
            ui.t = n; continue;
          }
          break;
        }
      }
      if (ui.t >= timelineEnd()) { ui.t = timelineEnd(); setPlaying(false); }
      maybeDissolve(before, ui.t);
      followPlayhead();
    }
    if (ui.fade) {
      const p = (now - ui.fade.t0) / ui.fade.dur;
      if (p >= 1) stopFade(); else fadeCv.style.opacity = 1 - p;
    }
    const ca = coverage('A', ui.t), cb = coverage('B', ui.t);
    syncVideo('A', ca); syncVideo('B', cb);
    layoutStage(ca, cb);
    $('#stageEmpty').style.display = S.clips.length ? 'none' : 'flex';
    $('#stageCut').style.display = rangeAt(S.cuts, ui.t) >= 0 ? 'block' : 'none';
    $('#tcVideo').textContent = fmtVideo(ui.t);
    $('#tcReal').textContent = S.clips.length ? `+${fmtReal(ui.t)} real` : '';
    drawTimeline();
  }
  requestAnimationFrame(tick);
}

// Preview dissolve: freeze the outgoing picture on a canvas and fade it out
// over the new footage (close enough to the exported cross-fade).
const fadeCv = $('#fadeCanvas');
function startFade(dur) {
  const st = $('#stage'), dpr = window.devicePixelRatio || 1;
  const W = st.clientWidth, H = st.clientHeight;
  fadeCv.width = W * dpr; fadeCv.height = H * dpr;
  const g = fadeCv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.fillStyle = '#000'; g.fillRect(0, 0, W, H);
  const vs = Object.values(vids).filter((v) => v.style.display === 'block' && v.readyState >= 2 && v.videoWidth)
    .sort((a, b) => a.style.zIndex - b.style.zIndex);
  for (const v of vs) {
    const x = v.offsetLeft, y = v.offsetTop, w = v.offsetWidth, h = v.offsetHeight;
    const k = (v.style.objectFit === 'cover' ? Math.max : Math.min)(w / v.videoWidth, h / v.videoHeight);
    const dw = v.videoWidth * k, dh = v.videoHeight * k;
    g.save();
    g.beginPath(); g.rect(x, y, w, h); g.clip();
    g.globalAlpha = parseFloat(v.style.opacity || '1');
    g.drawImage(v, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh);
    g.restore();
    const b = parseFloat(v.style.outlineWidth) || 0;
    if (v.style.outlineStyle === 'solid' && b) {
      g.strokeStyle = '#fff'; g.lineWidth = b; g.strokeRect(x - b / 2, y - b / 2, w + b, h + b);
    }
  }
  ui.fade = { t0: performance.now(), dur: Math.max(0.08, dur) * 1000 };
  fadeCv.style.opacity = 1;
}
function stopFade() { ui.fade = null; fadeCv.style.opacity = 0; }

function segIndexAt(t) {
  const segs = segments();
  let lo = 0, hi = segs.length - 1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (t < segs[m].start) hi = m - 1;
    else if (t >= segs[m].end) lo = m + 1;
    else return m;
  }
  return -1;
}
function maybeDissolve(t0, t1) {
  const i1 = segIndexAt(t1);
  if (i1 <= 0 || i1 === segIndexAt(t0)) return;
  const sg = segments()[i1];
  if (sg.fade) startFade(((sg.fade / fps()) * (S.output.speed || 1)) / ui.speed);
}

function setPlaying(p) {
  if (p && ui.t >= timelineEnd() - 1e-3) ui.t = 0;
  ui.playing = p;
  $('#playBtn').textContent = p ? '❚❚' : '▶';
  if (!p) for (const v of Object.values(vids)) v.pause();
}

function seek(t) { ui.t = clamp(t, 0, timelineEnd()); stopFade(); }

function followPlayhead() {
  const w = canvas.clientWidth - LABEL_W;
  const x = xOf(ui.t) - LABEL_W;
  if (x > w - 20 || x < 0) { ui.view.start = vOf(ui.t) - 20 / ui.view.pps; drawDirty = true; }
}

function jumpKept(dir) {
  const segs = segments();
  if (!segs.length) return;
  const bounds = [];
  for (const s of segs) bounds.push(s.start);
  const uniq = [...new Set(bounds)].sort((a, b) => a - b);
  const eps = 1e-3;
  const target = dir > 0 ? uniq.find((b) => b > ui.t + eps) : [...uniq].reverse().find((b) => b < ui.t - eps);
  if (target !== undefined) { seek(target); followPlayhead(); }
}

// ------------------------------------------------------------------ timeline canvas

const canvas = $('#tl');
const ctx = canvas.getContext('2d');
const base = document.createElement('canvas');
const bctx = base.getContext('2d');
const LABEL_W = 92, RULER = 32, TRACK_H = 92, GAP = 8, NAME_H = 15, GRAPH_H = 26, OUT_H = 16;
const trackY = (i) => RULER + 6 + i * (TRACK_H + GAP);
const OUT_Y = () => trackY(2) + 2;
let drawDirty = true;

// "View" coordinates: master time, or in collapsed mode the time with every
// part that is not exported (cuts, gaps) squeezed out.
let viewMap = null;
function keptIntervals() {
  if (viewMap) return viewMap;
  const out = [];
  for (const sg of segments()) {
    const last = out[out.length - 1];
    if (last && Math.abs(last.b - sg.start) < 1e-6) last.b = sg.end;
    else out.push({ a: sg.start, b: sg.end });
  }
  let off = 0;
  for (const k of out) { k.off = off; off += k.b - k.a; }
  return (viewMap = out);
}
function vOf(t) {
  if (!ui.compact) return t;
  const ks = keptIntervals();
  for (const k of ks) {
    if (t < k.a) return k.off;
    if (t <= k.b) return k.off + (t - k.a);
  }
  return viewEnd();
}
function tOfV(v) {
  if (!ui.compact) return v;
  const ks = keptIntervals();
  if (!ks.length) return 0;
  if (v <= 0) return ks[0].a;
  for (const k of ks) if (v < k.off + (k.b - k.a)) return k.a + (v - k.off);
  return ks[ks.length - 1].b;
}
function viewEnd() {
  if (!ui.compact) return timelineEnd();
  const ks = keptIntervals();
  return ks.length ? ks[ks.length - 1].off + ks[ks.length - 1].b - ks[ks.length - 1].a : 1;
}
const xOf = (t) => LABEL_W + (vOf(t) - ui.view.start) * ui.view.pps;
const tOf = (x) => tOfV(ui.view.start + (x - LABEL_W) / ui.view.pps);

function setCompact(on) {
  ui.compact = on;
  viewMap = null;
  $('#compactBtn').classList.toggle('on', on);
  $('#compactBtn').textContent = on ? 'Show cut parts' : 'Hide cut parts';
  zoomFit();
}

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  for (const c of [canvas, base]) { c.width = w * dpr; c.height = h * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  bctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  drawDirty = true;
}

function zoomFit() {
  const w = canvas.clientWidth - LABEL_W - 20;
  ui.view.pps = Math.max(0.05, w / viewEnd());
  ui.view.start = 0;
  drawDirty = true;
}
function zoomAt(factor, x) {
  const v = ui.view.start + (x - LABEL_W) / ui.view.pps;
  ui.view.pps = clamp(ui.view.pps * factor, 0.02, 400);
  ui.view.start = v - (x - LABEL_W) / ui.view.pps;
  drawDirty = true;
}

function niceStep(pps) {
  const steps = [1 / 30, 2 / 30, 5 / 30, 10 / 30, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600];
  return steps.find((s) => s * pps >= 70) || 7200;
}

function getThumb(id, n) {
  const key = `${id}/${n}`;
  let im = THUMBS.get(key);
  if (!im) {
    im = new Image();
    im.onload = () => { drawDirty = true; };
    im.onerror = () => { im.failed = true; };
    im.src = `/thumb/${id}/${n}.jpg`;
    THUMBS.set(key, im);
  }
  return im.complete && !im.failed && im.naturalWidth ? im : null;
}

function hatch(g, x0, y0, w, h, color) {
  g.save();
  g.beginPath(); g.rect(x0, y0, w, h); g.clip();
  g.strokeStyle = color; g.lineWidth = 1;
  g.beginPath();
  for (let x = x0 - h - ((x0 % 8) + 8) % 8; x < x0 + w; x += 8) { g.moveTo(x, y0 + h); g.lineTo(x + h, y0); }
  g.stroke();
  g.restore();
}

function drawBase() {
  const g = bctx, W = canvas.clientWidth, H = canvas.clientHeight;
  g.clearRect(0, 0, W, H);
  g.fillStyle = '#1b1d21'; g.fillRect(0, 0, W, H);
  const t0 = tOf(LABEL_W), t1 = tOf(W);

  // track rows
  TRACKS.forEach((tr, i) => {
    const y = trackY(i);
    g.fillStyle = '#15171a'; g.fillRect(LABEL_W, y, W - LABEL_W, TRACK_H);
    g.save(); g.beginPath(); g.rect(LABEL_W, y, W - LABEL_W, TRACK_H); g.clip();
    for (const c of clipsOf(tr)) {
      const x0 = xOf(c.start), x1 = xOf(clipEnd(c));
      if (x1 < LABEL_W || x0 > W) continue;
      drawClip(g, c, x0, x1, y, W);
    }
    for (const h of S.hidden[tr] || []) {
      const x0 = Math.max(LABEL_W, xOf(h.start)), x1 = Math.min(W, xOf(h.end));
      if (x1 <= x0) continue;
      g.fillStyle = 'rgba(10,10,12,0.72)'; g.fillRect(x0, y, x1 - x0, TRACK_H);
      hatch(g, x0, y, x1 - x0, TRACK_H, 'rgba(160,160,170,0.25)');
      g.fillStyle = '#aaa'; g.font = '11px -apple-system'; g.fillText('hidden', x0 + 4, y + TRACK_H / 2 + 4);
    }
    g.restore();
    g.fillStyle = '#22252a'; g.fillRect(0, y, LABEL_W - 4, TRACK_H);
    g.fillStyle = '#e6e7e9'; g.font = '600 12px -apple-system';
    g.fillText(tr, 10, y + 20);
    g.fillStyle = '#8b9098'; g.font = '11px -apple-system';
    const nm = S.tracks[tr].name;
    g.fillText(nm.length > 12 ? nm.slice(0, 11) + '…' : nm, 10, y + 36);
    const st = clipsOf(tr).map((c) => AN[c.id]).filter(Boolean);
    const busy = st.filter((a) => a.status !== 'done');
    if (busy.length) {
      const p = st.reduce((a, s) => a + (s.progress || 0), 0) / st.length;
      g.fillText(`analysing ${Math.round(p * 100)}%`, 10, y + 52);
    }
  });

  // output strip: what ends up in the video
  const oy = OUT_Y();
  g.fillStyle = '#8b9098'; g.font = '11px -apple-system'; g.fillText('Output', 10, oy + 12);
  for (const s of segments()) {
    const x0 = Math.max(LABEL_W, xOf(s.start)), x1 = Math.min(W, xOf(s.end));
    if (x1 <= x0) continue;
    g.fillStyle = s.a && s.b ? '#3ecf8e' : s.a ? '#4f8cff' : '#b57bff';
    g.fillRect(x0, oy + 3, Math.max(1, x1 - x0), OUT_H - 6);
  }
  g.font = '600 10px -apple-system';
  for (const o of S.overrides) {
    const x0 = Math.max(LABEL_W, xOf(o.start)), x1 = Math.min(W, xOf(o.end));
    if (x1 <= x0) continue;
    g.fillStyle = '#ffd60a'; g.fillRect(x0, oy, x1 - x0, 3);
    if (x1 - x0 > 50) {
      g.save(); g.beginPath(); g.rect(x0, oy, x1 - x0, OUT_H); g.clip();
      g.fillStyle = '#111'; g.fillText(layoutLabel(o), x0 + 4, oy + OUT_H - 4); g.restore();
    }
  }
  for (const s of segments()) {
    if (!s.fade) continue;
    const x = xOf(s.start), cy = oy + OUT_H / 2;
    if (x < LABEL_W || x > W) continue;
    g.fillStyle = s.kind === 'session' ? '#fff' : '#9aa0a8';
    g.beginPath(); g.moveTo(x, cy - 5); g.lineTo(x + 4, cy); g.lineTo(x, cy + 5); g.lineTo(x - 4, cy); g.fill();
  }

  // cuts across all tracks
  const yTop = trackY(0), yBot = trackY(1) + TRACK_H;
  S.cuts.forEach((c, i) => {
    const x0 = Math.max(LABEL_W, xOf(c.start)), x1 = Math.min(W, xOf(c.end));
    if (ui.compact) {
      if (x0 < LABEL_W || x0 > W) return;
      g.fillStyle = i === ui.selCut ? '#ff4d5e' : 'rgba(255,77,94,0.75)';
      g.fillRect(Math.round(x0) - 1, yTop, i === ui.selCut ? 3 : 2, yBot - yTop);
      g.beginPath(); g.moveTo(x0 - 4, RULER - 9); g.lineTo(x0 + 4, RULER - 9); g.lineTo(x0, RULER - 3); g.fill();
      return;
    }
    if (x1 <= x0) return;
    const sel = i === ui.selCut;
    g.fillStyle = sel ? 'rgba(255,77,94,0.42)' : 'rgba(255,77,94,0.22)';
    g.fillRect(x0, yTop, x1 - x0, yBot - yTop);
    hatch(g, x0, yTop, x1 - x0, yBot - yTop, sel ? 'rgba(255,120,130,0.6)' : 'rgba(255,77,94,0.35)');
    g.fillStyle = c.auto ? '#ff8a96' : '#ff4d5e';
    g.fillRect(x0, RULER - 5, x1 - x0, 5);
    if (sel) { g.strokeStyle = '#ff4d5e'; g.lineWidth = 2; g.strokeRect(x0 + 1, yTop + 1, x1 - x0 - 2, yBot - yTop - 2); }
  });

  // split points and the selected piece
  for (const sp of S.splits) {
    const x = Math.round(xOf(sp)) + 0.5;
    if (x < LABEL_W || x > W) continue;
    g.setLineDash([4, 3]); g.strokeStyle = 'rgba(255,255,255,0.7)'; g.lineWidth = 1;
    g.beginPath(); g.moveTo(x, RULER - 5); g.lineTo(x, yBot); g.stroke(); g.setLineDash([]);
    g.fillStyle = '#fff'; g.beginPath(); g.moveTo(x - 4, RULER - 9); g.lineTo(x + 4, RULER - 9); g.lineTo(x, RULER - 3); g.fill();
  }
  const piece = !ui.sel && selectedPiece();
  if (piece) {
    const x0 = Math.max(LABEL_W, xOf(piece.start)), x1 = Math.min(W, xOf(piece.end));
    if (x1 > x0) { g.strokeStyle = '#ffd60a'; g.lineWidth = 3; g.strokeRect(x0 + 1.5, yTop + 1.5, x1 - x0 - 3, yBot - yTop - 3); }
  }

  // parts restored on purpose (auto-cut leaves them alone)
  for (const k of S.keeps) {
    const x0 = Math.max(LABEL_W, xOf(k.start)), x1 = Math.min(W, xOf(k.end));
    if (x1 > x0) { g.fillStyle = 'rgba(62,207,142,0.85)'; g.fillRect(x0, RULER - 5, x1 - x0, 5); }
  }

  // selection
  if (ui.sel) {
    const x0 = Math.max(LABEL_W, xOf(ui.sel.start)), x1 = Math.min(W, xOf(ui.sel.end));
    if (x1 > x0) {
      g.fillStyle = 'rgba(79,140,255,0.22)'; g.fillRect(x0, 0, x1 - x0, yBot);
      g.strokeStyle = '#4f8cff'; g.lineWidth = 1; g.strokeRect(x0 + 0.5, 0.5, x1 - x0 - 1, yBot - 1);
    }
  }

  // ruler
  g.fillStyle = '#22252a'; g.fillRect(LABEL_W, 0, W - LABEL_W, RULER - 5);
  const step = niceStep(ui.view.pps);
  g.font = '10px -apple-system'; g.strokeStyle = '#555a62'; g.fillStyle = '#9aa0a8';
  g.beginPath();
  const v0 = ui.view.start, v1 = ui.view.start + (W - LABEL_W) / ui.view.pps;
  for (let v = Math.floor(Math.max(0, v0) / step) * step; v <= v1; v += step) {
    const x = Math.round(LABEL_W + (v - v0) * ui.view.pps) + 0.5;
    if (x < LABEL_W) continue;
    g.moveTo(x, RULER - 12); g.lineTo(x, RULER - 5);
    g.fillText(fmtVideo(v), x + 3, 11);
    g.fillText(fmtReal(tOfV(v)), x + 3, 22);
  }
  g.stroke();
  g.fillStyle = '#22252a'; g.fillRect(0, 0, LABEL_W - 4, RULER - 5);
  g.fillStyle = '#8b9098'; g.fillText(ui.compact ? 'output' : 'video', 10, 11); g.fillText('real', 10, 22);
}

function drawClip(g, c, x0, x1, y, W) {
  const sel = c.id === ui.selClip;
  g.fillStyle = '#2b3a55'; g.fillRect(x0, y, x1 - x0, TRACK_H);
  const d = DIFFS[c.id];
  // thumbnails
  const ty = y + NAME_H, th = TRACK_H - NAME_H - GRAPH_H;
  const ar = c.width && c.height ? c.width / c.height : 16 / 9;
  const tw = Math.max(20, th * ar);
  if (d && d.smooth) {
    const vx0 = Math.max(x0, LABEL_W), vx1 = Math.min(x1, W);
    const first = x0 + Math.floor((vx0 - x0) / tw) * tw;
    g.save(); g.beginPath(); g.rect(x0, y, x1 - x0, TRACK_H); g.clip();
    for (let x = first; x < vx1; x += tw) {
      const lt = tOf(x + tw / 2) - c.start;
      const n = clamp(Math.floor((lt * c.fps) / d.thumbEvery) + 1, 1, d.nThumbs);
      const im = getThumb(c.id, n);
      if (im) g.drawImage(im, x, ty, tw, th);
    }
    g.restore();
    // activity graph
    const gy = y + TRACK_H - GRAPH_H;
    g.fillStyle = '#121418'; g.fillRect(x0, gy, x1 - x0, GRAPH_H);
    const thr = THR[c.track], top = thr * 2.5;
    const sm = d.smooth;
    for (let x = Math.floor(vx0); x < vx1; x++) {
      const f0 = Math.floor((tOf(x) - c.start) * c.fps);
      const f1 = Math.max(f0 + 1, Math.floor((tOf(x + 1) - c.start) * c.fps));
      let v = 0;
      for (let f = Math.max(0, f0); f < Math.min(sm.length, f1); f++) if (sm[f] > v) v = sm[f];
      const hgt = Math.min(1, v / top) * (GRAPH_H - 2);
      g.fillStyle = v > thr ? '#ffa63d' : '#4b5059';
      g.fillRect(x, gy + GRAPH_H - hgt, 1, hgt);
    }
    const ly = Math.round(gy + GRAPH_H - (GRAPH_H - 2) / 2.5) + 0.5;
    g.setLineDash([3, 3]); g.strokeStyle = 'rgba(255,255,255,0.45)';
    g.beginPath(); g.moveTo(Math.max(x0, LABEL_W), ly); g.lineTo(Math.min(x1, W), ly); g.stroke();
    g.setLineDash([]);
  } else {
    const a = AN[c.id];
    g.fillStyle = '#8b9098'; g.font = '11px -apple-system';
    const msg = a && a.status === 'error' ? `analysis failed: ${a.error || ''}` :
      `analysing… ${a ? Math.round((a.progress || 0) * 100) : 0}%`;
    g.fillText(msg, Math.max(x0, LABEL_W) + 6, ty + th / 2 + 4);
  }
  // name bar
  g.fillStyle = 'rgba(0,0,0,0.55)'; g.fillRect(x0, y, x1 - x0, NAME_H);
  g.save(); g.beginPath(); g.rect(x0, y, x1 - x0, NAME_H); g.clip();
  g.fillStyle = '#e6e7e9'; g.font = '11px -apple-system';
  g.fillText(`${c.name} · ${fmtReal(c.duration)}`, Math.max(x0, LABEL_W) + 5, y + 11);
  g.restore();
  g.strokeStyle = sel ? '#fff' : '#0d0e10'; g.lineWidth = sel ? 2 : 1;
  g.strokeRect(x0 + 0.5, y + 0.5, x1 - x0 - 1, TRACK_H - 1);
}

function drawTimeline() {
  if (drawDirty) { drawBase(); drawDirty = false; }
  const W = canvas.clientWidth, H = canvas.clientHeight;
  ctx.clearRect(0, 0, W, H);
  ctx.drawImage(base, 0, 0, W, H);
  const x = Math.round(xOf(ui.t)) + 0.5;
  if (x >= LABEL_W && x <= W) {
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, OUT_Y() + OUT_H); ctx.stroke();
    ctx.fillStyle = '#fff';
    ctx.beginPath(); ctx.moveTo(x - 6, 0); ctx.lineTo(x + 6, 0); ctx.lineTo(x, 8); ctx.fill();
  }
}

// ------------------------------------------------------------------ timeline interaction

let drag = null;
const pos = (e) => { const r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
function trackAt(y) {
  for (let i = 0; i < TRACKS.length; i++) if (y >= trackY(i) && y < trackY(i) + TRACK_H) return TRACKS[i];
  return null;
}
function clipAt(tr, t) {
  for (let i = S.clips.length - 1; i >= 0; i--) {
    const c = S.clips[i];
    if (c.track === tr && t >= c.start && t < clipEnd(c)) return c;
  }
  return null;
}
function cutEdgeAt(x, y) {
  if (ui.compact || y > trackY(1) + TRACK_H) return null;
  for (let i = 0; i < S.cuts.length; i++) {
    const c = S.cuts[i];
    if (Math.abs(xOf(c.start) - x) < 5) return { i, side: 'start' };
    if (Math.abs(xOf(c.end) - x) < 5) return { i, side: 'end' };
  }
  return null;
}

canvas.addEventListener('mousedown', (e) => {
  if (!S) return;
  const { x, y } = pos(e);
  if (x < LABEL_W) {
    const tr = trackAt(y);
    if (tr && e.detail === 2) {
      const n = prompt(`Name for camera ${tr}`, S.tracks[tr].name);
      if (n) { S.tracks[tr].name = n; renderTrackNames(); changed(); }
    }
    return;
  }
  const t = tOf(x);
  const edge = cutEdgeAt(x, y);
  if (edge) {
    drag = { kind: 'edge', ...edge, snap: snapshot(), moved: false, orig: { ...S.cuts[edge.i] } };
    ui.selCut = edge.i;
  } else if (y < RULER) {
    const si = S.splits.findIndex((sp) => Math.abs(xOf(sp) - x) < 5);
    if (e.detail === 2 && si >= 0) { removeSplit(si); return; }
    drag = { kind: 'scrub' };
    seek(t);
  } else {
    const ci = !trackAt(y) ? -1
      : ui.compact ? S.cuts.findIndex((c) => Math.abs(xOf(c.start) - x) < 5) : rangeAt(S.cuts, t);
    const tr = trackAt(y);
    const clip = tr && ci < 0 ? clipAt(tr, t) : null;
    if (ci >= 0) {
      ui.selCut = ci; ui.selClip = null; ui.selPieceT = null;
      drag = { kind: 'click', x0: x, t };
      if (e.detail === 2) removeCut(ci);
    } else if (clip) {
      ui.selClip = clip.id; ui.selCut = -1;
      ui.sel = null; ui.selPieceT = t; updateSelInfo();
      if (ui.compact) { drag = { kind: 'click', x0: x, t }; renderClipInfo(); drawDirty = true; return; }
      const moving = e.altKey ? clipsOf(clip.track) : [clip];
      drag = { kind: 'clip', clip, x0: x, t, snap: snapshot(), moved: false, starts: moving.map((c) => [c, c.start]) };
      renderClipInfo();
    } else {
      ui.selCut = -1; ui.selClip = null; ui.selPieceT = null;
      drag = { kind: 'select', x0: x, t0: t, t };
      renderClipInfo();
    }
  }
  drawDirty = true;
});

window.addEventListener('mousemove', (e) => {
  if (!S) return;
  const { x, y } = pos(e);
  if (!drag) {
    if (e.target !== canvas) return;
    const edge = x >= LABEL_W && cutEdgeAt(x, y);
    const tr = trackAt(y);
    canvas.style.cursor = edge ? 'ew-resize' : y < RULER && x >= LABEL_W ? 'text'
      : tr && rangeAt(S.cuts, tOf(x)) < 0 && clipAt(tr, tOf(x)) ? 'grab' : 'default';
    return;
  }
  const t = tOf(x);
  if (drag.kind === 'scrub') seek(t);
  else if (drag.kind === 'select') {
    if (Math.abs(x - drag.x0) > 3) {
      ui.sel = { start: Math.max(0, Math.min(drag.t0, t)), end: Math.max(drag.t0, t) };
      updateSelInfo();
    }
  } else if (drag.kind === 'clip') {
    if (Math.abs(x - drag.x0) < 3 && !drag.moved) return;
    drag.moved = true;
    canvas.style.cursor = 'grabbing';
    let dt = (x - drag.x0) / ui.view.pps;
    const [c0, s0] = drag.starts[0];
    const moving = new Set(drag.starts.map(([c]) => c));
    const targets = [0, ui.t];
    for (const c of S.clips) if (!moving.has(c)) targets.push(c.start, clipEnd(c));
    let best = 8 / ui.view.pps;
    for (const cand of [s0 + dt, s0 + dt + c0.duration]) {
      for (const tg of targets) if (Math.abs(tg - cand) < best) { best = Math.abs(tg - cand); dt += tg - cand; }
    }
    const minStart = Math.min(...drag.starts.map(([, s]) => s));
    dt = Math.max(dt, -minStart);
    for (const [c, s] of drag.starts) c.start = frameRound(s + dt);
    segCache = null; viewMap = null; drawDirty = true;
    renderClipInfo();
  } else if (drag.kind === 'edge') {
    const c = S.cuts[drag.i];
    drag.moved = true;
    if (drag.side === 'start') c.start = clamp(frameRound(t), 0, c.end - 1 / fps());
    else c.end = clamp(frameRound(t), c.start + 1 / fps(), timelineEnd());
    segCache = null; viewMap = null; drawDirty = true;
  }
});

window.addEventListener('mouseup', () => {
  if (!drag) return;
  if (drag.kind === 'clip') {
    if (drag.moved) { pushUndo(drag.snap); changed(); } else seek(drag.t);
  } else if (drag.kind === 'select' && !ui.sel) {
    seek(drag.t);
  } else if (drag.kind === 'select' && Math.abs(xOf(ui.sel.end) - xOf(ui.sel.start)) < 3) {
    ui.sel = null; seek(drag.t); updateSelInfo();
  } else if (drag.kind === 'click') {
    seek(drag.t);
  } else if (drag.kind === 'edge' && drag.moved) {
    pushUndo(drag.snap);
    const c = S.cuts[drag.i], o = drag.orig;
    if (o.auto) {
      // Shrinking an auto cut keeps the uncovered part; growing it makes it a manual cut.
      if (c.start > o.start) S.keeps.push({ start: o.start, end: c.start });
      if (c.end < o.end) S.keeps.push({ start: c.end, end: o.end });
      S.keeps = mergeRanges(S.keeps);
      if (c.start < o.start || c.end > o.end) c.auto = false;
    }
    S.cuts = normCuts(S.cuts);
    ui.selCut = -1;
    changed();
  }
  drag = null;
  canvas.style.cursor = 'default';
  drawDirty = true;
});

canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  const { x } = pos(e);
  if (e.ctrlKey || e.metaKey) zoomAt(Math.exp(-e.deltaY * 0.01), Math.max(x, LABEL_W));
  else {
    ui.view.start += (e.deltaX + (e.shiftKey ? e.deltaY : e.deltaY)) / ui.view.pps;
    ui.view.start = clamp(ui.view.start, -50 / ui.view.pps, viewEnd());
    drawDirty = true;
  }
}, { passive: false });

// ------------------------------------------------------------------ edit actions

// Pieces: split points (⌘B) divide the timeline; clicking a clip selects the piece under it.
const pieceBounds = () => [0, ...S.splits.filter((x) => x > 0 && x < timelineEnd()), timelineEnd()];
function pieceAt(t) {
  const b = pieceBounds();
  for (let i = 0; i < b.length - 1; i++) if (t >= b[i] && t < b[i + 1]) return { start: b[i], end: b[i + 1] };
  return null;
}
const selectedPiece = () => (ui.selPieceT == null ? null : pieceAt(ui.selPieceT));
// Edit actions apply to the dragged range if any, else to the selected piece.
const target = () => ui.sel || selectedPiece();

function needTarget() {
  const r = target();
  if (!r) $('#selInfo').textContent = 'Select something first: click a piece (split with ⌘B), or drag in an empty part of a track.';
  return r;
}
function clearTarget() { ui.sel = null; ui.selPieceT = null; updateSelInfo(); }

function splitAtPlayhead() {
  const t = frameRound(ui.t);
  if (t <= 0 || t >= timelineEnd() || S.splits.some((x) => Math.abs(x - t) < 0.5 / fps())) return;
  pushUndo();
  S.splits = [...S.splits, t].sort((a, b) => a - b);
  ui.sel = null; ui.selPieceT = t;   // like iMovie: the piece after the split is selected
  updateSelInfo(); changed(false);
}
function removeSplit(i) { pushUndo(); S.splits.splice(i, 1); updateSelInfo(); changed(false); }

function cutSelection() {
  const r = needTarget(); if (!r) return;
  pushUndo();
  S.cuts = normCuts([...S.cuts, { start: r.start, end: r.end, auto: false }]);
  S.keeps = subtractRange(S.keeps, r.start, r.end);
  clearTarget(); changed();
}
function restoreSelection() {
  const r = needTarget(); if (!r) return;
  pushUndo();
  S.cuts = subtractRange(S.cuts, r.start, r.end);
  S.keeps = mergeRanges([...S.keeps, { start: r.start, end: r.end }]);
  changed();
}
function hideSel(tr) {
  const r = needTarget(); if (!r) return;
  pushUndo();
  S.hidden[tr] = mergeRanges([...(S.hidden[tr] || []), { start: r.start, end: r.end }]);
  changed();
}
function unhideSel() {
  const r = needTarget(); if (!r) return;
  pushUndo();
  for (const tr of TRACKS) S.hidden[tr] = subtractRange(S.hidden[tr] || [], r.start, r.end);
  changed();
}
// value: '' (default layout), 'pip:A' / 'pip:B', or a mode name.
function setLayoutHere(value) {
  const r = needTarget(); if (!r) return;
  pushUndo();
  S.overrides = subtractRange(S.overrides, r.start, r.end);
  if (value) {
    const [mode, main] = value.split(':');
    S.overrides = [...S.overrides, { start: r.start, end: r.end, mode, ...(main ? { main } : {}) }]
      .sort((a, b) => a.start - b.start);
  }
  updateSelInfo(); changed(false);
}
function swapHere() {
  const r = needTarget(); if (!r) return;
  const L = layoutAt((r.start + r.end) / 2);
  const main = L.mode === 'pip' ? L.main : S.layout.main;
  setLayoutHere(`pip:${main === 'A' ? 'B' : 'A'}`);
}
function updateSelInfo() {
  const r = target();
  $('#selInfo').textContent = r
    ? `${ui.sel ? 'Range' : 'Piece'} ${fmtVideo(r.start)} → ${fmtVideo(r.end)}  ·  ${fmtDur(r.end - r.start)} video, ${fmtReal(r.end - r.start)} real`
    : 'Click a piece (split with ⌘B), or drag in an empty part of a track to select a range.';
  const o = r && S.overrides.find((x) => x.start <= r.start + 1e-6 && x.end >= r.end - 1e-6);
  $('#pieceLayout').value = o ? (o.main ? `${o.mode}:${o.main}` : o.mode) : '';
  drawDirty = true;
}

// ------------------------------------------------------------------ sync from footage
// Camera clocks can be off by a few minutes on some days. Both cameras film the
// same scene, so their activity graphs move together: for a clip, find the time
// shift against the other camera that makes them match best, window by window.

const ACT = {};   // clip id -> log activity, lightly smoothed (cached)
function activityOf(c) {
  if (ACT[c.id]) return ACT[c.id];
  const sm = DIFFS[c.id].smooth, n = sm.length, out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let s = 0, k = 0;
    for (let j = Math.max(0, i - 2); j <= Math.min(n - 1, i + 2); j++) { s += Math.log1p(sm[j]); k++; }
    out[i] = s / k;
  }
  return (ACT[c.id] = out);
}

// Lag in frames such that an event at master frame m on `ref` shows at m + lag in `clip`.
function measureLag(clip, ref) {
  const f = fps();
  const maxLag = Math.round((15 * 60) / S.settings.interval);   // search ±15 min real
  const win = Math.round((25 * 60) / S.settings.interval);      // 25-min windows
  const a = activityOf(clip), n = a.length;
  const s0 = Math.round(clip.start * f), base = s0 - maxLag, len = n + 2 * maxLag;
  const r = new Float32Array(len).fill(NaN);
  for (const c of clipsOf(ref)) {
    if (!(DIFFS[c.id] && DIFFS[c.id].smooth)) continue;
    const b = activityOf(c), c0 = Math.round(c.start * f) - base;
    for (let i = Math.max(0, -c0); i < b.length && c0 + i < len; i++) r[c0 + i] = b[i];
  }
  const found = [];
  for (let w = 0; w + win / 3 < n; w += win) {
    const w1 = Math.min(n, w + win);
    let best = { corr: -1, lag: 0 };
    for (let lag = -maxLag; lag <= maxLag; lag += 2) {
      let sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0, k = 0;
      for (let i = w; i < w1; i++) {
        const y = r[i - lag + maxLag];
        if (y !== y) continue;   // NaN: no footage on the other camera
        const x = a[i];
        sa += x; sb += y; saa += x * x; sbb += y * y; sab += x * y; k++;
      }
      if (k < 150) continue;
      const va = saa - (sa * sa) / k, vb = sbb - (sb * sb) / k;
      if (va <= 1e-9 || vb <= 1e-9) continue;
      const corr = (sab - (sa * sb) / k) / Math.sqrt(va * vb);
      if (corr > best.corr) best = { corr, lag };
    }
    if (best.corr >= 0.45) found.push(best);
  }
  if (!found.length) return null;
  const lags = found.map((x) => x.lag).sort((x, y) => x - y);
  return { lag: lags[Math.floor(lags.length / 2)], windows: found.length, spread: lags[lags.length - 1] - lags[0] };
}

function syncFromFootage(clips) {
  const pending = S.clips.filter((c) => !(DIFFS[c.id] && DIFFS[c.id].smooth));
  if (pending.length) { alert(`Wait for the analysis of ${pending.length} clip(s) to finish.`); return; }
  const f = fps(), realMin = (frames) => (frames * S.settings.interval) / 60;
  const lines = [];
  const moves = [];
  const fmtMin = (mins) => (Math.abs(mins) < 0.05 ? 'already in sync' : `moved ${mins > 0 ? '+' : ''}${mins.toFixed(1)} min`);
  const measured = clips.map((c) => [c, measureLag(c, c.track === 'A' ? 'B' : 'A')]);
  for (const [c, m] of measured) {
    if (m) {
      const drift = realMin(m.spread) > 1 ? ` (varies by ${realMin(m.spread).toFixed(1)} min inside the clip)` : '';
      lines.push(`${c.name}: ${fmtMin(realMin(-m.lag))}, ${m.windows} window(s) agree${drift}`);
      if (m.lag) moves.push([c, -m.lag / f]);
      continue;
    }
    // No overlap with the other camera: a clock error is the same all day, so use
    // the shift of the nearest measured clip of this camera recorded within 12 h.
    const near = measured.filter(([o, om]) => om && o.track === c.track && c.creation_ts && o.creation_ts
      && Math.abs(o.creation_ts - c.creation_ts) < 12 * 3600)
      .sort(([x], [y]) => Math.abs(x.creation_ts - c.creation_ts) - Math.abs(y.creation_ts - c.creation_ts))[0];
    if (!near) { lines.push(`${c.name}: no activity shared with the other camera, left as is`); continue; }
    lines.push(`${c.name}: ${fmtMin(realMin(-near[1].lag))}, same as ${near[0].name} (no overlap to measure)`);
    if (near[1].lag) moves.push([c, -near[1].lag / f]);
  }
  if (moves.length) {
    pushUndo();
    for (const [c, dt] of moves) c.start = frameRound(c.start + dt);
    const min = Math.min(...S.clips.map((c) => c.start));
    if (min < 0) shiftProject(-min);
    changed();
  }
  $('#syncReport').textContent = lines.join('\n') || 'Nothing to sync.';
  $('#syncDlg').showModal();
}

function alignClips(mode) {
  if (!S.clips.length) return;
  pushUndo();
  const r = ratio();
  const wall = (c) => mode === 'creation' ? (c.creation_ts ?? c.name_ts ?? (c.mtime - c.duration * r))
    : mode === 'mtime' ? c.mtime - c.duration * r
    : mode === 'name' ? c.name_ts : null;
  const known = S.clips.filter((c) => wall(c) != null);
  const origin = known.length ? Math.min(...known.map(wall)) : 0;
  for (const tr of TRACKS) {
    let cursor = 0;
    const list = clipsOf(tr).sort((a, b) => (wall(a) ?? 0) - (wall(b) ?? 0) || a.name.localeCompare(b.name));
    for (const c of list) {
      const w = wall(c);
      c.start = frameRound(w != null ? (w - origin) / r : cursor);
      cursor = clipEnd(c);
    }
  }
  const missing = S.clips.length - known.length;
  $('#detectInfo').textContent = mode !== 'sequential' && missing
    ? `${missing} clip(s) had no timestamp for this mode and were placed back to back.` : $('#detectInfo').textContent;
  zoomFit();
  changed();
}

// Move everything on the master timeline (clips and every edit) by dt seconds.
function shiftProject(dt) {
  const mv = (r) => ({ ...r, start: r.start + dt, end: r.end + dt });
  for (const c of S.clips) c.start = frameRound(c.start + dt);
  S.cuts = S.cuts.map(mv); S.keeps = S.keeps.map(mv); S.overrides = S.overrides.map(mv);
  for (const tr of TRACKS) S.hidden[tr] = (S.hidden[tr] || []).map(mv);
  S.splits = S.splits.map((x) => x + dt);
  ui.t += dt;
}

// New clips go where their recording time says, relative to clips already placed
// (so manual nudges of existing clips are kept); otherwise after the track's last clip.
function placeNewClips() {
  const fresh = S.clips.filter((c) => c.placed === false);
  if (!fresh.length) return;
  const placed = S.clips.filter((c) => c.placed !== false);
  if (!placed.length && S.clips.every((c) => c.creation_ts)) {
    alignClips('creation');
  } else {
    const r = ratio();
    for (const c of fresh) {
      const ref = placed.find((p) => p.track === c.track && p.creation_ts) || placed.find((p) => p.creation_ts);
      if (ref && c.creation_ts) c.start = frameRound(ref.start + (c.creation_ts - ref.creation_ts) / r);
    }
    // A clip recorded before everything else would land before 0: shift the whole
    // project instead of clamping it (clamping broke the alignment).
    const min = Math.min(...S.clips.map((c) => c.start));
    if (min < 0) shiftProject(-min);
  }
  for (const c of fresh) c.placed = true;
  changed();
}

// ------------------------------------------------------------------ inspector

function renderTrackNames() {
  for (const el of $$('.tname')) el.textContent = S.tracks[el.dataset.track].name;
}

function renderLayout() {
  const L = S.layout;
  for (const b of $$('#modeSeg button')) b.classList.toggle('on', b.dataset.mode === L.mode);
  for (const o of $$('.opt')) o.classList.toggle('on', o.dataset.for === L.mode);
  $('#pipMain').value = L.main; $('#pipCorner').value = L.corner;
  $('#pipSize').value = L.size; $('#pipSizeOut').value = `${Math.round(L.size * 100)}%`;
  $('#pipBorder').value = L.border; $('#pipBorderOut').value = `${L.border}px`;
  $('#blendOp').value = L.opacity; $('#blendOpOut').value = `${Math.round(L.opacity * 100)}%`;
  $('#fill').checked = !!L.fill;
}

function renderTransitions() {
  const T = S.transitions;
  const label = (n) => `${n} fr · ${(n / fps()).toFixed(2)} s`;
  $('#trSession').value = T.session; $('#trSessionOut').value = label(T.session);
  $('#trCut').value = T.cut; $('#trCutOut').value = label(T.cut);
  const segs = segments();
  const n = (k) => segs.filter((s) => s.kind === k && s.fade).length;
  $('#trInfo').textContent = `${n('session')} session join(s), ${n('cut') + n('layout')} cut / layout change(s) get a dissolve (◆ on the Output row).`;
}

function renderDetect() {
  const D = S.detect;
  $('#autoCut').checked = !!D.auto;
  $('#sens').value = D.sensitivity; $('#sensOut').value = `${D.sensitivity.toFixed(1)}×`;
  $('#minStill').value = D.minStillMin;
  $('#minStillOut').value = `${D.minStillMin} min`;
  $('#minStill').title = `= ${((D.minStillMin * 60) / ratio()).toFixed(1)} s of video`;
  $('#pad').value = D.padSec; $('#padOut').value = `${D.padSec} s`;
}

function renderClipInfo() {
  const c = S.clips.find((x) => x.id === ui.selClip);
  const box = $('#clipInfo');
  if (!c) { box.className = 'hint'; box.textContent = 'Click a clip on the timeline.'; return; }
  box.className = '';
  const a = AN[c.id] || {};
  const ts = c.creation_ts ? new Date(c.creation_ts * 1000).toLocaleString() : '—';
  box.innerHTML = `
    <div class="kv">
      <span>File</span><span title="${c.path}">${c.name}</span>
      <span>Camera</span><span>${S.tracks[c.track].name}</span>
      <span>Length</span><span>${fmtDur(c.duration)} video · ${fmtReal(c.duration)} real</span>
      <span>Format</span><span>${c.width}×${c.height} ${c.codec || ''} ${Math.round(c.fps)} fps</span>
      <span>Recorded</span><span>${ts}</span>
      <span>Analysis</span><span>${a.status || '—'}${a.status === 'running' ? ` ${Math.round(a.progress * 100)}%` : ''}</span>
    </div>
    <div class="numrow">Start <input type="number" id="clipStart" step="${(1 / fps()).toFixed(4)}" value="${c.start.toFixed(3)}"> s</div>
    <div class="row wrap">
      <button class="btn small" id="clipSwap">Move to ${c.track === 'A' ? 'B' : 'A'}</button>
      <button class="btn small" id="clipSync">Sync to other camera</button>
      <button class="btn small" id="clipReanalyze">Re-analyse</button>
      <button class="btn small" id="clipRemove">Remove</button>
    </div>`;
  $('#clipStart').onchange = (e) => {
    pushUndo(); c.start = Math.max(0, frameRound(parseFloat(e.target.value) || 0)); changed();
  };
  $('#clipSwap').onclick = () => { pushUndo(); c.track = c.track === 'A' ? 'B' : 'A'; computeThresholds(); changed(); renderClipInfo(); };
  $('#clipSync').onclick = () => syncFromFootage([c]);
  $('#clipReanalyze').onclick = async () => {
    delete DIFFS[c.id]; delete ACT[c.id];
    for (const k of [...THUMBS.keys()]) if (k.startsWith(c.id)) THUMBS.delete(k);
    await api('/api/reanalyze', { id: c.id });
  };
  $('#clipRemove').onclick = async () => {
    if (!confirm(`Remove ${c.name} from the project? (the file is not deleted)`)) return;
    const r = await api('/api/remove_clip', { id: c.id });
    S.clips = r.state.clips; ui.selClip = null; undoStack = []; redoStack = [];
    computeThresholds(); changed(); renderClipInfo();
  };
}

function bindInspector() {
  for (const b of $$('#modeSeg button')) b.onclick = () => { S.layout.mode = b.dataset.mode; renderLayout(); scheduleSave(); };
  const bindL = (sel, key, conv = parseFloat) => { $(sel).oninput = (e) => { S.layout[key] = conv(e.target.value); renderLayout(); scheduleSave(); }; };
  bindL('#pipMain', 'main', String); bindL('#pipCorner', 'corner', String);
  bindL('#pipSize', 'size'); bindL('#pipBorder', 'border'); bindL('#blendOp', 'opacity');
  $('#fill').onchange = (e) => { S.layout.fill = e.target.checked; scheduleSave(); };
  const bindD = (sel, key) => {
    $(sel).oninput = (e) => {
      S.detect[key] = parseFloat(e.target.value); renderDetect();
      if (key === 'sensitivity') { computeThresholds(); drawDirty = true; }
      scheduleSave(); scheduleDetect();
    };
  };
  bindD('#sens', 'sensitivity'); bindD('#minStill', 'minStillMin'); bindD('#pad', 'padSec');
  $('#autoCut').onchange = (e) => {
    S.detect.auto = e.target.checked;
    if (S.detect.auto) detectStill(true);
    else {
      pushUndo(); S.cuts = S.cuts.filter((c) => !c.auto); ui.selCut = -1;
      $('#detectInfo').textContent = 'Automatic cuts removed.';
      changed(false);
    }
  };
  $('#forgetKeepsBtn').onclick = () => { pushUndo(); S.keeps = []; changed(); };
  for (const [sel, key] of [['#trSession', 'session'], ['#trCut', 'cut']]) {
    $(sel).oninput = (e) => { S.transitions[key] = parseInt(e.target.value, 10); changed(false); };
  }
  $('#deleteBtn').onclick = () => (ui.selCut >= 0 && !target() ? removeCut(ui.selCut) : cutSelection());
  $('#splitBtn').onclick = splitAtPlayhead;
  $('#splitBtn2').onclick = splitAtPlayhead;
  $('#swapBtn').onclick = swapHere;
  $('#pieceLayout').onchange = (e) => setLayoutHere(e.target.value);
  $('#restoreSelBtn').onclick = restoreSelection;
  $('#hideABtn').onclick = () => hideSel('A');
  $('#hideBBtn').onclick = () => hideSel('B');
  $('#unhideBtn').onclick = unhideSel;
  $('#alignBtn').onclick = () => alignClips($('#alignMode').value);
  $('#syncBtn').onclick = () => syncFromFootage(clipsOf('A').filter((c) => c.duration > 1));
  $('#playBtn').onclick = () => setPlaying(!ui.playing);
  $('#prevBtn').onclick = () => jumpKept(-1);
  $('#nextBtn').onclick = () => jumpKept(1);
  $('#skipCuts').onchange = (e) => { ui.skipCuts = e.target.checked; };
  $('#speed').onchange = (e) => { ui.speed = parseFloat(e.target.value); };
  $('#zoomIn').onclick = () => zoomAt(1.6, xOf(ui.t));
  $('#zoomOut').onclick = () => zoomAt(1 / 1.6, xOf(ui.t));
  $('#zoomFit').onclick = zoomFit;
  $('#compactBtn').onclick = () => setCompact(!ui.compact);
  $('#deleteStillBtn').onclick = () => {
    if (!S.detect.auto) { S.detect.auto = true; renderDetect(); }
    detectStill(true);
    setCompact(true);
  };
}

// ------------------------------------------------------------------ add dialog

let addTrack = 'A';
function openAdd(tr) {
  addTrack = tr;
  $('#addTrackName').textContent = S.tracks[tr].name;
  $('#addErr').textContent = '';
  $('#addDlg').showModal();
}
async function afterAdd(r) {
  $('#addErr').textContent = r.errors && r.errors.length ? r.errors.join('\n') : '';
  const fresh = !S.clips.length;
  S.clips = r.state.clips; AN = r.analysis;
  if (r.added.length) {
    placeNewClips();
    if (fresh) zoomFit();
    changed();
    if (!r.errors.length) $('#addDlg').close();
  } else if (!r.errors.length) $('#addErr').textContent = 'No new video files found.';
}
$('#pickBtn').onclick = async () => afterAdd(await api('/api/pick', { track: addTrack }));
$('#addPathsBtn').onclick = async () => {
  const paths = $('#addPaths').value.split('\n').map((s) => s.trim()).filter(Boolean);
  if (paths.length) afterAdd(await api('/api/add', { track: addTrack, paths }));
};
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-add]');
  if (b) openAdd(b.dataset.add);
  // Keep space bar for play/pause instead of re-clicking the last button.
  const btn = e.target.closest('button');
  if (btn && !btn.closest('dialog')) btn.blur();
});

// ------------------------------------------------------------------ export

function openExport() {
  const segs = segments();
  if (!segs.length) { alert('Nothing to export yet.'); return; }
  const O = S.output;
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '');
  $('#exPath').value = O.lastPath || `${PROJECT_DIR}/timelapse-${stamp}.mp4`;
  $('#exRes').value = `${O.width}x${O.height}`;
  $('#exEnc').value = O.encoder; $('#exSpeed').value = String(O.speed);
  updateExportSummary();
  const ex = lastStatus.export || {};
  if (!ex.running) { $('#exStatus').textContent = ''; $('#exProgress').hidden = true; $('#exReveal').hidden = true; }
  $('#exportDlg').showModal();
}
function readExportForm() {
  const [w, h] = $('#exRes').value.split('x').map(Number);
  Object.assign(S.output, { width: w, height: h, encoder: $('#exEnc').value, speed: parseFloat($('#exSpeed').value) });
  segCache = null; viewMap = null;
}
function updateExportSummary() {
  readExportForm();
  const segs = segments();
  $('#exSummary').textContent = `${segs.length} piece(s) · final length ${fmtDur(outputSeconds())} · layout: ${S.layout.mode}`;
  fitStage(); updateStats(); scheduleSave();
}
for (const id of ['#exRes', '#exEnc', '#exSpeed']) $(id).onchange = updateExportSummary;
$('#exportBtn').onclick = openExport;
$('#exStart').onclick = async () => {
  readExportForm();
  S.output.lastPath = $('#exPath').value;
  scheduleSave();
  const r = await api('/api/export', {
    segments: segments().map(({ dur, a, b, frames, fade, layout }) => ({ dur, a, b, frames, fade, layout })),
    layout: S.layout, output: { ...S.output, path: $('#exPath').value }, fps: fps(),
  });
  if (r.error) $('#exStatus').textContent = r.error;
};
$('#exCancel').onclick = () => api('/api/export/cancel', {});
$('#exReveal').onclick = () => api('/api/reveal', { path: lastStatus.export.output });

function renderExport(ex) {
  const running = !!ex.running;
  $('#exStart').hidden = running; $('#exCancel').hidden = !running;
  if (!ex.step) return;
  $('#exProgress').hidden = false;
  $('#exProgress > div').style.width = `${Math.round((ex.progress || 0) * 100)}%`;
  const el = Math.round((Date.now() / 1000) - (ex.started || Date.now() / 1000));
  $('#exStatus').textContent = ex.error ? `Failed: ${ex.error}`
    : ex.step === 'done' ? `Done → ${ex.output}` : `${ex.step} · ${Math.round((ex.progress || 0) * 100)}% · ${fmtDur(el)} elapsed`;
  $('#exReveal').hidden = ex.step !== 'done';
}

// ------------------------------------------------------------------ status polling

let lastStatus = {};
async function poll() {
  try {
    const st = await api('/api/status');
    lastStatus = st;
    let redraw = false;
    for (const [id, a] of Object.entries(st.analysis)) {
      const prev = AN[id];
      if (!prev || prev.status !== a.status || Math.abs((prev.progress || 0) - (a.progress || 0)) > 0.005) redraw = true;
      if (a.status === 'done' && !DIFFS[id] && S.clips.some((c) => c.id === id)) loadDiffs(id);
    }
    AN = st.analysis;
    if (redraw) { drawDirty = true; if (ui.selClip) renderClipInfo(); }
    renderExport(st.export || {});
  } catch (e) { /* server restarting */ }
  setTimeout(poll, 1000);
}

// ------------------------------------------------------------------ keyboard

window.addEventListener('keydown', (e) => {
  if (!S || (e.target instanceof Element && e.target.closest('input, textarea, select, dialog'))) return;
  const k = e.key;
  if (k === ' ') { e.preventDefault(); setPlaying(!ui.playing); }
  else if ((e.metaKey || e.ctrlKey) && k.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? redo() : undo(); }
  else if (k === 'ArrowRight' || k === 'ArrowLeft') {
    e.preventDefault();
    const step = e.shiftKey ? 1 : 1 / fps();
    seek(ui.t + (k === 'ArrowRight' ? step : -step));
  } else if (k === 'i') { ui.sel = { start: ui.t, end: Math.max(ui.t, ui.sel ? ui.sel.end : timelineEnd()) }; updateSelInfo(); }
  else if (k === 'o') { ui.sel = { start: Math.min(ui.t, ui.sel ? ui.sel.start : 0), end: ui.t }; updateSelInfo(); }
  else if ((e.metaKey || e.ctrlKey) && k.toLowerCase() === 'b') { e.preventDefault(); splitAtPlayhead(); }
  else if (k === 's' && !e.metaKey && !e.ctrlKey) splitAtPlayhead();
  else if (k === 'w' && !e.metaKey && !e.ctrlKey) swapHere();
  else if (k === 'h' && !e.metaKey && !e.ctrlKey) setCompact(!ui.compact);
  else if (k === 'x') cutSelection();
  else if (k === 'Escape') { ui.selCut = -1; clearTarget(); }
  else if (k === 'Backspace' || k === 'Delete') {
    if (ui.selCut >= 0) removeCut(ui.selCut);
    else if (target()) cutSelection();
  } else if (k === '+' || k === '=') zoomAt(1.6, xOf(ui.t));
  else if (k === '-') zoomAt(1 / 1.6, xOf(ui.t));
  else if (k === '0') zoomFit();
  else if (k === 'ArrowUp') jumpKept(-1);
  else if (k === 'ArrowDown') jumpKept(1);
});

// ------------------------------------------------------------------ boot

async function boot() {
  const r = await api('/api/state');
  S = r.state; AN = r.analysis; PROJECT_DIR = r.projectDir;
  S.cuts = normCuts(S.cuts || []);
  S.hidden = S.hidden || { A: [], B: [] };
  S.keeps = S.keeps || [];
  S.splits = S.splits || [];
  S.overrides = S.overrides || [];
  S.transitions = S.transitions || { session: 12, cut: 4 };
  renderTrackNames(); renderLayout(); renderDetect(); bindInspector(); updateSelInfo();
  resizeCanvas(); fitStage(); zoomFit(); updateStats();
  for (const c of S.clips) if (AN[c.id] && AN[c.id].status === 'done') loadDiffs(c.id);
  const f0 = S.clips[0] && S.clips[0].fps;
  if (f0 && S.clips.every((c) => Math.abs(c.fps - f0) < 0.01) && Math.abs(S.settings.fps - f0) > 0.001) {
    S.settings.fps = Math.round(f0 * 1000) / 1000;
    scheduleSave();
  }
  placeNewClips();
  zoomFit();
  $('#saveState').textContent = '';
  poll();
  requestAnimationFrame(tick);
}
window.addEventListener('resize', () => { resizeCanvas(); fitStage(); });
boot();
