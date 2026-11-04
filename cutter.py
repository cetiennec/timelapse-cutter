#!/usr/bin/env python3
"""Timelapse Cutter — a tiny two-camera timelapse editor.

Local web app: a Python server (stdlib + numpy + ffmpeg) and a browser UI.

    python3 cutter.py                       # project stored in ./project
    python3 cutter.py --project ~/Movies/robot-build --a camA/ --b camB/

Open http://127.0.0.1:8765 (opened automatically).
"""

import argparse
import hashlib
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".aif", ".aiff", ".flac", ".ogg", ".opus", ".caf"}

# Analysis frames are downscaled to this size; thumbnails every THUMB_EVERY frames.
AN_W, AN_H = 64, 36
THUMB_EVERY = 15

PROJECT_DIR = None
CACHE_DIR = None
STATE_PATH = None
LOCK = threading.RLock()
STATE = {}
ANALYSIS = {}  # clip id -> {"status", "progress", "error"}
AN_QUEUE = queue.Queue()
EXPORT = {"running": False}


# ---------------------------------------------------------------- state

def default_state():
    return {
        "version": 1,
        "settings": {"interval": 2.0, "fps": 30.0},
        "tracks": {"A": {"name": "Camera A"}, "B": {"name": "Camera B"}},
        "clips": [],
        "cuts": [],
        "keeps": [],
        "splits": [],
        "overrides": [],
        "hidden": {"A": [], "B": []},
        "layout": {"mode": "pip", "main": "A", "corner": "br", "size": 0.32,
                   "border": 4, "opacity": 0.5, "fill": False},
        "detect": {"auto": True, "sensitivity": 2.5, "minStillMin": 3.0, "padSec": 20.0},
        "transitions": {"session": 12, "cut": 4},
        "output": {"width": 1920, "height": 1080, "encoder": "x264", "speed": 1.0},
        # Soundtrack: songs played back to back from `offset` s into the first one,
        # looped if shorter than the video, faded in/out at the video's ends.
        "music": {"tracks": [], "volume": 0.8, "fadeIn": 1.0, "fadeOut": 3.0, "offset": 0.0, "loop": True},
        # Blurred areas (e.g. a computer screen), per clip: {clip id: [{x, y, w, h}]} as
        # fractions of the source frame.
        "blurs": {},
    }


def load_state():
    global STATE
    st = default_state()
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                saved = json.load(f)
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(st.get(k), dict):
                    st[k].update(v)
                else:
                    st[k] = v
        except Exception:
            traceback.print_exc()
    # Older projects may contain sidecar files (e.g. DJI .LRF low-res proxies) that
    # duplicate the real video; drop them.
    dropped = [c["name"] for c in st["clips"] if os.path.splitext(c["path"])[1].lower() not in VIDEO_EXT]
    if dropped:
        st["clips"] = [c for c in st["clips"] if os.path.splitext(c["path"])[1].lower() in VIDEO_EXT]
        print(f"Removed {len(dropped)} non-video clip(s) from the project: {', '.join(dropped[:6])}…")
    STATE = st


def save_state():
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(STATE, f, indent=1)
    os.replace(tmp, STATE_PATH)


def clip_by_id(cid):
    for c in STATE["clips"]:
        if c["id"] == cid:
            return c
    return None


def missing_ids():
    return [c["id"] for c in STATE["clips"] if not os.path.isfile(c["path"])]


def search_dirs():
    """Folders to look in when Spotlight finds nothing: the other clips' folders and
    their parents' subfolders, Desktop, Downloads, and Movies (two levels deep)."""
    home = os.path.expanduser("~")
    dirs = {os.path.dirname(c["path"]) for c in STATE["clips"]}
    roots = {os.path.dirname(d) for d in dirs} | {os.path.join(home, "Movies")}
    dirs |= {os.path.join(home, "Desktop"), os.path.join(home, "Downloads")}
    for root in roots:
        for dp, dn, _ in os.walk(root):
            if dp[len(root):].count(os.sep) >= 2:
                dn[:] = []
            dirs.add(dp)
    return sorted(dirs)


def relink_missing():
    """Find clips whose file was moved: same file name and same size, via Spotlight."""
    found, lost = [], []
    for c in STATE["clips"]:
        if os.path.isfile(c["path"]):
            continue
        r = subprocess.run(["mdfind", "-name", c["name"]], capture_output=True, text=True)
        cands = r.stdout.splitlines() + [os.path.join(d, c["name"]) for d in search_dirs()]
        hit = next((p for p in cands
                    if os.path.basename(p) == c["name"] and os.path.isfile(p)
                    and os.path.getsize(p) == c.get("size")), None)
        if hit:
            c["path"] = hit
            found.append(f"{c['name']} → {os.path.dirname(hit)}")
        else:
            lost.append(c["name"])
    save_state()
    return found, lost


def clip_dir(cid):
    return os.path.join(CACHE_DIR, "clips", cid)


# ---------------------------------------------------------------- probing

def parse_rate(s):
    try:
        if "/" in s:
            n, d = s.split("/")
            return float(n) / float(d) if float(d) else 0.0
        return float(s)
    except Exception:
        return 0.0


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


NAME_TS = re.compile(r"(20\d\d)[-_.]?(\d\d)[-_.]?(\d\d)[-_ T.]?(\d\d)[-_.:h]?(\d\d)[-_.:m]?(\d\d)")


def name_timestamp(name):
    m = NAME_TS.search(name)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups())).timestamp()
    except ValueError:
        return None


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    info = json.loads(out.stdout)
    vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not vs:
        raise RuntimeError("no video stream")
    fmt = info.get("format", {})
    fps = parse_rate(vs.get("avg_frame_rate", "")) or parse_rate(vs.get("r_frame_rate", "")) or 30.0
    dur = float(vs.get("duration") or fmt.get("duration") or 0)
    nb = int(vs.get("nb_frames") or 0) or int(round(dur * fps))
    tags = {**fmt.get("tags", {}), **vs.get("tags", {})}
    st = os.stat(path)
    return {
        "path": path,
        "name": os.path.basename(path),
        "duration": dur,
        "fps": fps,
        "frames": nb,
        "width": int(vs.get("width", 0)),
        "height": int(vs.get("height", 0)),
        "codec": vs.get("codec_name"),
        "creation_ts": parse_iso(tags.get("creation_time")),
        "mtime": st.st_mtime,
        "name_ts": name_timestamp(os.path.basename(path)),
        "size": st.st_size,
    }


def expand_paths(paths):
    files = []
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p.strip().strip('"').strip("'")))
        if not p:
            continue
        if os.path.isdir(p):
            for n in sorted(os.listdir(p)):
                if os.path.splitext(n)[1].lower() in VIDEO_EXT and not n.startswith("."):
                    files.append(os.path.join(p, n))
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXT:
            # Picked files are filtered too: skips camera sidecars such as DJI .LRF proxies.
            files.append(p)
    return files


def add_clips(track, paths):
    added, errors = [], []
    for p in expand_paths(paths):
        try:
            meta = probe(p)
        except Exception as e:
            errors.append(f"{os.path.basename(p)}: {e}")
            continue
        cid = hashlib.sha1(f"{p}|{meta['size']}|{meta['mtime']}".encode()).hexdigest()[:12]
        with LOCK:
            if clip_by_id(cid):
                continue
            # Default placement: right after the last clip of that track.
            end = max([c["start"] + c["duration"] for c in STATE["clips"] if c["track"] == track] or [0])
            clip = {"id": cid, "track": track, "start": end, "placed": False, **meta}
            if not STATE["clips"]:
                STATE["settings"]["fps"] = round(meta["fps"], 3)   # real-time ratio = interval × fps
            STATE["clips"].append(clip)
            added.append(clip)
        queue_analysis(cid)
    with LOCK:
        save_state()
    return added, errors


def add_music(paths):
    added, errors = [], []
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p.strip()))
        if os.path.splitext(p)[1].lower() not in AUDIO_EXT:
            errors.append(f"{os.path.basename(p)}: not an audio file")
            continue
        r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", p],
                           capture_output=True, text=True)
        try:
            dur = float(json.loads(r.stdout)["format"]["duration"])
        except Exception:
            errors.append(f"{os.path.basename(p)}: can't read it")
            continue
        st = os.stat(p)
        mid = hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:12]
        with LOCK:
            if any(t["id"] == mid for t in STATE["music"]["tracks"]):
                continue
            t = {"id": mid, "path": p, "name": os.path.basename(p), "duration": dur}
            STATE["music"]["tracks"].append(t)
            added.append(t)
    with LOCK:
        save_state()
    return added, errors


def music_missing():
    return [t["id"] for t in STATE["music"]["tracks"] if not os.path.isfile(t["path"])]


def mux_music(video, out, music):
    """Lay the soundtrack under `video` (copied as is) and write `out`."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
                       capture_output=True, text=True)
    dur = float(r.stdout.strip())
    tracks = music["tracks"]
    total = sum(t["duration"] for t in tracks)
    offset = min(max(0.0, float(music.get("offset", 0))), max(0.0, total - 0.1))
    reps = max(1, int(-(-(offset + dur) // total))) if music.get("loop", True) else 1
    seq = (tracks * reps)[:200]
    inputs, chain = [], []
    for i, t in enumerate(seq):
        inputs += ["-i", t["path"]]
        chain.append(f"[{i + 1}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[m{i}]")
    fi, fo = max(0.0, float(music.get("fadeIn", 0))), max(0.0, float(music.get("fadeOut", 0)))
    vol = max(0.0, float(music.get("volume", 0.8)))
    a = "".join(f"[m{i}]" for i in range(len(seq)))
    post = f"atrim=start={offset:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,volume={vol:.3f}"
    if fi:
        post += f",afade=t=in:st=0:d={min(fi, dur):.3f}"
    if fo:
        post += f",afade=t=out:st={max(0.0, dur - fo):.3f}:d={min(fo, dur):.3f}"
    chain.append(f"{a}concat=n={len(seq)}:v=0:a=1,{post},apad[aout]")
    run_ffmpeg(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", video, *inputs,
                "-filter_complex", ";".join(chain), "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{dur:.3f}",
                "-movflags", "+faststart", out], "music")


# ---------------------------------------------------------------- screen detection

SCREEN = {"model": None, "lock": threading.Lock()}
SCREEN_MODEL = "google/owlv2-base-patch16-ensemble"
SCREEN_QUERIES = ["a computer monitor", "a laptop screen", "a television screen", "a phone screen"]


def screen_model():
    """Open-vocabulary detector (OWLv2), loaded on first use (downloads ~600 MB once)."""
    if SCREEN["model"] is None:
        try:
            import torch
            from transformers import Owlv2ForObjectDetection, Owlv2Processor
        except ImportError:
            raise RuntimeError("screen detection needs PyTorch and transformers: "
                               "python3 -m pip install torch transformers")
        dev = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
        proc = Owlv2Processor.from_pretrained(SCREEN_MODEL)
        model = Owlv2ForObjectDetection.from_pretrained(SCREEN_MODEL).to(dev).eval()
        SCREEN["model"] = (torch, proc, model, dev)
    return SCREEN["model"]


def grab_frame(path, t):
    from PIL import Image
    import io
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0, t):.3f}", "-i", path,
                        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def detect_screens(clip, t0, t1, threshold=0.3):
    """Screens visible in clip between t0 and t1 (clip seconds), as boxes in fractions of
    the frame. Samples a few frames and keeps what shows up in most of them: screens
    don't move while the camera stands still, stray detections do."""
    n = int(min(12, max(3, (t1 - t0) // 20 + 1)))
    times = [t0 + (t1 - t0) * (i + 0.5) / n for i in range(n)]
    with SCREEN["lock"]:
        torch, proc, model, dev = screen_model()
        dets, seen, size = [], 0, None
        for k, t in enumerate(times):
            im = grab_frame(clip["path"], t)
            if im is None:
                continue
            seen += 1
            size = im.size
            inp = proc(text=[SCREEN_QUERIES], images=im, return_tensors="pt").to(dev)
            with torch.no_grad():
                out = model(**inp)
            side = max(im.size)   # OWLv2 pads the image to a square
            res = proc.post_process_grounded_object_detection(out, threshold=threshold,
                                                              target_sizes=torch.tensor([[side, side]]))[0]
            for sc, lb, bx in zip(res["scores"].tolist(), res["labels"].tolist(), res["boxes"].tolist()):
                dets.append((k, sc, SCREEN_QUERIES[lb], bx))
    if not seen:
        raise RuntimeError("could not read frames from the video")
    W, H = size

    def overlap(a, b):
        ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        area = lambda r: max(1e-6, (r[2] - r[0]) * (r[3] - r[1]))
        return max(inter / (area(a) + area(b) - inter), inter / min(area(a), area(b)) * 0.5)

    def union(a, b):
        return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]

    clusters = []   # {box, frames, score, label}
    for k, sc, lb, bx in sorted(dets, key=lambda d: -d[1]):
        c = next((c for c in clusters if overlap(c["box"], bx) > 0.3), None)
        if c:
            c["box"] = union(c["box"], bx); c["frames"].add(k)
        else:
            clusters.append({"box": list(bx), "frames": {k}, "score": sc, "label": lb})
    need = max(1, -(-seen * 3 // 10))   # in at least 30% of the frames
    keep = [c for c in clusters if len(c["frames"]) >= need]
    merged = True
    while merged:   # boxes that ended up overlapping become one
        merged = False
        for i in range(len(keep)):
            for j in range(i + 1, len(keep)):
                if overlap(keep[i]["box"], keep[j]["box"]) > 0.3:
                    keep[i]["box"] = union(keep[i]["box"], keep[j]["box"])
                    keep[i]["score"] = max(keep[i]["score"], keep[j]["score"])
                    del keep[j]; merged = True
                    break
            if merged:
                break
    boxes = []
    for c in keep:
        x0, y0, x1, y1 = c["box"]
        px, py = 0.02 * W, 0.02 * H   # a little margin around the screen
        x0, y0 = max(0.0, x0 - px), max(0.0, y0 - py)
        x1, y1 = min(W, x1 + px), min(H, y1 + py)
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        boxes.append({"x": x0 / W, "y": y0 / H, "w": (x1 - x0) / W, "h": (y1 - y0) / H,
                      "score": round(c["score"], 2), "label": c["label"]})
    return {"boxes": boxes, "frames": seen}


# ---------------------------------------------------------------- analysis

def queue_analysis(cid):
    if os.path.exists(os.path.join(clip_dir(cid), "diffs.json")):
        ANALYSIS[cid] = {"status": "done", "progress": 1.0}
        return
    ANALYSIS[cid] = {"status": "queued", "progress": 0.0}
    AN_QUEUE.put(cid)


def run_analysis(clip, hwaccel):
    cid = clip["id"]
    d = clip_dir(cid)
    tdir = os.path.join(d, "thumbs")
    shutil.rmtree(tdir, ignore_errors=True)
    os.makedirs(tdir, exist_ok=True)
    fc = (f"[0:v]scale=320:-2:flags=fast_bilinear,split=2[s1][s2];"
          f"[s1]scale={AN_W}:{AN_H},format=gray[g];"
          f"[s2]select='not(mod(n\\,{THUMB_EVERY}))'[t]")
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if hwaccel:
        cmd += ["-hwaccel", "videotoolbox"]
    cmd += ["-i", clip["path"], "-filter_complex", fc,
            "-map", "[g]", "-f", "rawvideo", "pipe:1",
            "-map", "[t]", "-fps_mode", "passthrough", "-q:v", "5",
            os.path.join(tdir, "%06d.jpg")]
    errf = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf)
    fsize = AN_W * AN_H
    total = max(1, clip["frames"])
    diffs = []
    prev = None
    buf = b""
    while True:
        chunk = proc.stdout.read(fsize * 64)
        if not chunk:
            break
        buf += chunk
        n = len(buf) // fsize
        if not n:
            continue
        frames = np.frombuffer(buf[:n * fsize], np.uint8).reshape(n, fsize).astype(np.float32)
        buf = buf[n * fsize:]
        # Remove per-frame mean so global exposure flicker does not count as motion.
        frames -= frames.mean(axis=1, keepdims=True)
        if prev is None:
            seq = frames
            diffs.append(0.0)
        else:
            seq = np.vstack([prev[None], frames])
        diffs.extend(np.abs(np.diff(seq, axis=0)).mean(axis=1).round(3).tolist())
        prev = frames[-1]
        ANALYSIS[cid]["progress"] = min(0.999, len(diffs) / total)
    proc.wait()
    if proc.returncode != 0 or not diffs:
        errf.seek(0)
        raise RuntimeError(errf.read().decode(errors="replace").strip()[-500:] or "ffmpeg failed")
    with open(os.path.join(d, "diffs.json"), "w") as f:
        json.dump({"fps": clip["fps"], "thumbEvery": THUMB_EVERY, "diffs": diffs}, f)


def analysis_worker():
    while True:
        cid = AN_QUEUE.get()
        with LOCK:
            clip = clip_by_id(cid)
        if not clip:
            continue
        ANALYSIS[cid] = {"status": "running", "progress": 0.0}
        try:
            try:
                run_analysis(clip, hwaccel=True)
            except Exception:
                ANALYSIS[cid]["progress"] = 0.0
                run_analysis(clip, hwaccel=False)
            ANALYSIS[cid] = {"status": "done", "progress": 1.0}
        except Exception as e:
            ANALYSIS[cid] = {"status": "error", "progress": 0.0, "error": str(e)}
            traceback.print_exc()


# ---------------------------------------------------------------- export

def even(x):
    return max(2, int(round(x / 2)) * 2)


def fit(inp, w, h, fill, out, fps, speed):
    if fill:
        f = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    else:
        f = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
    return f"[{inp}]setpts=(PTS-STARTPTS)/{speed},fps={fps},{f},setsar=1,format=yuv420p[{out}]"


def blur_filter(src, boxes, sigma, out):
    """Blur each box (fractions of the frame) of input `src` into label `out`."""
    parts, cur = [], src
    for j, b in enumerate(boxes):
        x, y, w, h = (min(1.0, max(0.0, float(b[k]))) for k in ("x", "y", "w", "h"))
        if w < 0.002 or h < 0.002:
            continue
        p = f"{out}_{j}"
        parts.append(f"[{cur}]split=2[{p}m][{p}c];"
                     f"[{p}c]crop=w=iw*{w:.5f}:h=ih*{h:.5f}:x=iw*{x:.5f}:y=ih*{y:.5f},gblur=sigma={sigma:.1f}[{p}b];"
                     f"[{p}m][{p}b]overlay=x=W*{x:.5f}:y=H*{y:.5f}[{p}o]")
        cur = f"{p}o"
    if not parts:
        return None
    return ";".join(parts) + f";[{cur}]null[{out}]"


def segment_filter(seg, lay, W, H, fps, speed, blurs=None):
    """Build a filter graph for one segment. Inputs: 0 = A (if present), next = B.
    `blurs` maps "A"/"B" to (boxes, sigma) for the areas to blur in that source."""
    srcs = []
    if seg.get("a"):
        srcs.append("A")
    if seg.get("b"):
        srcs.append("B")
    idx = {s: f"{i}:v" for i, s in enumerate(srcs)}
    fill = bool(lay.get("fill"))
    mode = lay.get("mode", "pip")
    if mode in ("A", "B") and mode in idx:
        srcs = [mode]
    pre = []
    for s_ in srcs:
        if blurs and blurs.get(s_):
            f = blur_filter(idx[s_], *blurs[s_], f"blur{s_}")
            if f:
                pre.append(f)
                idx[s_] = f"blur{s_}"
    return ";".join(pre + [layout_filter(srcs, idx, lay, W, H, fps, speed, fill, mode)])


def layout_filter(srcs, idx, lay, W, H, fps, speed, fill, mode):
    if len(srcs) == 1:
        return fit(idx[srcs[0]], W, H, fill, "out", fps, speed)
    if mode == "sbs":
        w2 = even(W / 2)
        return ";".join([fit(idx["A"], w2, H, fill, "l", fps, speed),
                         fit(idx["B"], W - w2, H, fill, "r", fps, speed),
                         "[l][r]hstack=inputs=2[out]"])
    if mode == "stack":
        h2 = even(H / 2)
        return ";".join([fit(idx["A"], W, h2, fill, "t", fps, speed),
                         fit(idx["B"], W, H - h2, fill, "b", fps, speed),
                         "[t][b]vstack=inputs=2[out]"])
    if mode == "blend":
        op = float(lay.get("opacity", 0.5))
        return ";".join([fit(idx["A"], W, H, fill, "a", fps, speed),
                         fit(idx["B"], W, H, fill, "b", fps, speed),
                         f"[b][a]blend=all_mode=normal:all_opacity={op:.3f}[out]"])
    # picture-in-picture
    main = lay.get("main", "A")
    inset = "B" if main == "A" else "A"
    size = float(lay.get("size", 0.32))
    bw = int(lay.get("border", 0))
    iw, ih = even(W * size), even(H * size)
    margin = even(min(W, H) * 0.03)
    corner = lay.get("corner", "br")
    x = f"{margin}" if "l" in corner else f"W-w-{margin}"
    y = f"{margin}" if "t" in corner else f"H-h-{margin}"
    border = f",pad=iw+{2*bw}:ih+{2*bw}:{bw}:{bw}:white" if bw > 0 else ""
    return ";".join([fit(idx[main], W, H, fill, "m", fps, speed),
                     fit(idx[inset], iw, ih, True, "i0", fps, speed),
                     f"[i0]null{border}[i]",
                     f"[m][i]overlay=x={x}:y={y}:shortest=1[out]"])


def encoder_args(enc):
    if enc == "videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", "25M", "-profile:v", "high"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]


LOSSLESS = ["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0"]


def run_ffmpeg(cmd, what):
    EXPORT["proc"] = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
    _, err = EXPORT["proc"].communicate()
    if EXPORT["proc"].returncode != 0:
        if EXPORT.get("cancel"):
            raise RuntimeError("cancelled")
        raise RuntimeError(f"{what}: {err.strip()[-800:]}")


def render_segment(seg, first, nframes, out, ctx, enc):
    """Render output frames [first, first + nframes) of one segment."""
    fps, speed = ctx["fps"], ctx["speed"]
    skip = first * speed / fps
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    blurs = {}
    for key in ("a", "b"):
        s = seg.get(key)
        if s:
            with LOCK:
                clip = clip_by_id(s["clip"])
                boxes = list(STATE.get("blurs", {}).get(s["clip"], []))
            if not clip:
                raise RuntimeError(f"unknown clip {s['clip']}")
            if "blur" in seg:   # the UI sends the boxes active in this segment
                boxes = list((seg["blur"] or {}).get(key) or [])
            if boxes:
                blurs[key.upper()] = (boxes, max(6.0, 0.02 * min(clip["width"] or 1080, clip["height"] or 1080)))
            cmd += ["-ss", f"{max(0, s['t'] + skip):.4f}", "-t", f"{(nframes + 2) * speed / fps:.4f}",
                    "-i", clip["path"]]
    lay = seg.get("layout") or ctx["layout"]   # per-part layout overrides come with the segment
    cmd += ["-filter_complex", segment_filter(seg, lay, ctx["W"], ctx["H"], fps, speed, blurs),
            "-map", "[out]", "-frames:v", str(nframes), "-r", f"{fps:g}", "-an",
            *enc, "-pix_fmt", "yuv420p", "-video_track_timescale", "30000", out]
    run_ffmpeg(cmd, "segment")


def render_dissolve(prev, cur, d, out, ctx):
    """Cross-fade the last d frames of `prev` into the first d frames of `cur`."""
    tmp = os.path.dirname(out)
    a, b = os.path.join(tmp, "xa.mp4"), os.path.join(tmp, "xb.mp4")
    render_segment(prev, prev["frames"] - d, d, a, ctx, LOSSLESS)
    render_segment(cur, 0, d, b, ctx, LOSSLESS)
    fps = ctx["fps"]
    run_ffmpeg(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", a, "-i", b, "-filter_complex",
                f"[0:v][1:v]xfade=transition=fade:duration={d / fps:.4f}:offset=0,format=yuv420p[out]",
                "-map", "[out]", "-frames:v", str(d), "-r", f"{fps:g}", "-an",
                *encoder_args(ctx["encoder"]), "-pix_fmt", "yuv420p",
                "-video_track_timescale", "30000", out], "dissolve")


def export_worker(job):
    try:
        out = job["output"]
        ctx = {"layout": job["layout"], "W": even(out["width"]), "H": even(out["height"]),
               "fps": float(job.get("fps", 30.0)), "speed": max(0.1, float(out.get("speed", 1.0))),
               "encoder": out.get("encoder")}
        # Each segment carries its output length in frames and `fade`, the number of
        # frames it cross-fades with the previous segment (computed by the UI).
        segs = [s for s in job["segments"] if int(s.get("frames", 0)) >= 1]
        with LOCK:
            used = {s[k]["clip"] for s in segs for k in ("a", "b") if s.get(k)}
            gone = [c["name"] for c in STATE["clips"] if c["id"] in used and not os.path.isfile(c["path"])]
        if gone:
            raise RuntimeError("video file(s) not found — moved or renamed? Use “Find moved files”: "
                               + ", ".join(gone))
        with LOCK:
            music = json.loads(json.dumps(STATE["music"]))
        lost = [t["name"] for t in music["tracks"] if not os.path.isfile(t["path"])]
        if lost:
            raise RuntimeError("music file(s) not found — moved or renamed? Remove and add them again: "
                               + ", ".join(lost))
        for i, s in enumerate(segs):
            s["frames"] = int(s["frames"])
            s["fade"] = int(s.get("fade", 0)) if i else 0
        path = os.path.abspath(os.path.expanduser(out["path"]))
        tmp = os.path.join(CACHE_DIR, "export_tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        total = sum(s["frames"] for s in segs) or 1
        done = 0
        parts = []
        for i, seg in enumerate(segs):
            if EXPORT.get("cancel"):
                raise RuntimeError("cancelled")
            if seg["fade"]:
                part = os.path.join(tmp, f"x{i:05d}.mp4")
                render_dissolve(segs[i - 1], seg, seg["fade"], part, ctx)
                parts.append(part)
            head = seg["fade"]
            tail = segs[i + 1]["fade"] if i + 1 < len(segs) else 0
            body = seg["frames"] - head - tail
            if body >= 1:
                part = os.path.join(tmp, f"s{i:05d}.mp4")
                render_segment(seg, head, body, part, ctx, encoder_args(ctx["encoder"]))
                parts.append(part)
            done += seg["frames"]
            EXPORT.update(progress=0.98 * done / total, step=f"segment {i+1}/{len(segs)}")
        if not parts:
            raise RuntimeError("nothing to export (everything is cut?)")
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w") as f:
            f.writelines(f"file '{p}'\n" for p in parts)
        EXPORT["step"] = "joining"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        joined = os.path.join(tmp, "joined.mp4") if music["tracks"] else path
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                            "-i", lst, "-c", "copy", "-movflags", "+faststart", joined],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-800:])
        if music["tracks"]:
            EXPORT["step"] = "adding music"
            mux_music(joined, path, music)
        shutil.rmtree(tmp, ignore_errors=True)
        EXPORT.update(running=False, progress=1.0, step="done", output=path)
    except Exception as e:
        EXPORT.update(running=False, error=str(e), step="failed")
        traceback.print_exc()


# ---------------------------------------------------------------- macOS helpers

def pick_files(prompt="Choose timelapse videos", types=None):
    of_type = f' of type {{"{types}"}}' if types else ""
    script = f'''
set fs to choose file with prompt "{prompt}"{of_type} with multiple selections allowed
set out to ""
repeat with f in fs
  set out to out & POSIX path of f & linefeed
end repeat
return out'''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return [l for l in r.stdout.splitlines() if l.strip()]


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/" in self.path and "status" not in self.path:
            sys.stderr.write("%s\n" % (fmt % args))

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def send_file(self, path, ctype=None, cache=False):
        if not os.path.isfile(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-cache")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        p = unquote(url.path)
        if p == "/":
            return self.send_file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
        if p.startswith("/static/"):
            f = os.path.normpath(os.path.join(STATIC, p[len("/static/"):]))
            if not f.startswith(STATIC):
                return self.send_error(403)
            return self.send_file(f)
        if p == "/api/state":
            with LOCK:
                return self.send_json({"state": STATE, "analysis": ANALYSIS, "projectDir": PROJECT_DIR,
                                       "missing": missing_ids(), "musicMissing": music_missing()})
        if p == "/api/status":
            ex = {k: v for k, v in EXPORT.items() if k not in ("proc",)}
            with LOCK:
                miss = missing_ids()
            return self.send_json({"analysis": ANALYSIS, "export": ex, "missing": miss})
        m = re.fullmatch(r"/api/diffs/([0-9a-f]{12})", p)
        if m:
            return self.send_file(os.path.join(clip_dir(m.group(1)), "diffs.json"), "application/json")
        m = re.fullmatch(r"/media/([0-9a-f]{12})", p)
        if m:
            with LOCK:
                clip = clip_by_id(m.group(1))
            if not clip:
                return self.send_error(404)
            ext = os.path.splitext(clip["path"])[1].lower()
            ctype = "video/webm" if ext == ".webm" else "video/mp4"
            return self.send_file(clip["path"], ctype)
        m = re.fullmatch(r"/audio/([0-9a-f]{12})", p)
        if m:
            with LOCK:
                t = next((t for t in STATE["music"]["tracks"] if t["id"] == m.group(1)), None)
            if not t:
                return self.send_error(404)
            return self.send_file(t["path"])
        m = re.fullmatch(r"/thumb/([0-9a-f]{12})/(\d+)\.jpg", p)
        if m:
            f = os.path.join(clip_dir(m.group(1)), "thumbs", f"{int(m.group(2)):06d}.jpg")
            return self.send_file(f, "image/jpeg", cache=True)
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            data = self.read_json()
        except Exception:
            return self.send_json({"error": "bad json"}, 400)
        if p == "/api/state":
            with LOCK:
                for k in ("cuts", "keeps", "splits", "overrides", "hidden", "layout", "detect", "transitions", "output", "settings", "tracks", "music", "blurs"):
                    if k in data:
                        STATE[k] = data[k]
                if "clips" in data:
                    upd = {c["id"]: c for c in data["clips"]}
                    for c in STATE["clips"]:
                        if c["id"] in upd:
                            c["start"] = float(upd[c["id"]]["start"])
                            c["track"] = upd[c["id"]]["track"]
                            c["placed"] = True
                save_state()
            return self.send_json({"ok": True})
        if p in ("/api/add", "/api/pick"):
            track = data.get("track", "A")
            paths = pick_files() if p == "/api/pick" else data.get("paths", [])
            added, errors = add_clips(track, paths)
            with LOCK:
                return self.send_json({"added": [c["id"] for c in added], "errors": errors,
                                       "state": STATE, "analysis": ANALYSIS})
        if p in ("/api/music/add", "/api/music/pick"):
            paths = pick_files("Choose music", "public.audio") if p == "/api/music/pick" else data.get("paths", [])
            added, errors = add_music(paths)
            with LOCK:
                return self.send_json({"added": [t["id"] for t in added], "errors": errors,
                                       "music": STATE["music"], "musicMissing": music_missing()})
        if p == "/api/detect_screens":
            with LOCK:
                clip = clip_by_id(data.get("clip", ""))
            if not clip:
                return self.send_json({"error": "unknown clip"}, 404)
            if not os.path.isfile(clip["path"]):
                return self.send_json({"error": f"file missing: {clip['name']}"}, 404)
            t0 = max(0.0, float(data.get("t0", 0)))
            t1 = min(clip["duration"], float(data.get("t1", clip["duration"])))
            try:
                return self.send_json(detect_screens(clip, t0, max(t0, t1)))
            except Exception as e:
                traceback.print_exc()
                return self.send_json({"error": str(e)}, 500)
        if p == "/api/relink":
            with LOCK:
                found, lost = relink_missing()
                return self.send_json({"found": found, "lost": lost, "state": STATE, "missing": missing_ids()})
        if p == "/api/remove_clip":
            with LOCK:
                STATE["clips"] = [c for c in STATE["clips"] if c["id"] != data.get("id")]
                save_state()
                return self.send_json({"state": STATE})
        if p == "/api/reanalyze":
            cid = data.get("id")
            shutil.rmtree(clip_dir(cid), ignore_errors=True)
            queue_analysis(cid)
            return self.send_json({"ok": True})
        if p == "/api/export":
            if EXPORT.get("running"):
                return self.send_json({"error": "an export is already running"}, 409)
            EXPORT.clear()
            EXPORT.update(running=True, progress=0.0, step="starting", started=time.time())
            threading.Thread(target=export_worker, args=(data,), daemon=True).start()
            return self.send_json({"ok": True})
        if p == "/api/export/cancel":
            EXPORT["cancel"] = True
            proc = EXPORT.get("proc")
            if proc and proc.poll() is None:
                proc.terminate()
            return self.send_json({"ok": True})
        if p == "/api/reveal":
            path = data.get("path", "")
            if os.path.exists(path):
                subprocess.Popen(["open", "-R", path])
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)


def main():
    global PROJECT_DIR, CACHE_DIR, STATE_PATH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=os.path.join(HERE, "project"),
                    help="folder holding the project file, cache and exports")
    ap.add_argument("--a", nargs="*", default=[], help="videos or folders for camera A")
    ap.add_argument("--b", nargs="*", default=[], help="videos or folders for camera B")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    PROJECT_DIR = os.path.abspath(os.path.expanduser(args.project))
    CACHE_DIR = os.path.join(PROJECT_DIR, ".cutter")
    STATE_PATH = os.path.join(PROJECT_DIR, "project.json")
    os.makedirs(CACHE_DIR, exist_ok=True)
    load_state()

    threading.Thread(target=analysis_worker, daemon=True).start()
    with LOCK:
        for c in STATE["clips"]:
            queue_analysis(c["id"])
    if args.a:
        add_clips("A", args.a)
    if args.b:
        add_clips("B", args.b)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Timelapse Cutter running at {url}  (project: {PROJECT_DIR})")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
