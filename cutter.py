#!/usr/bin/env python3
"""Timelapse Cutter — a tiny two-camera timelapse editor.

Local web app: a Python server (stdlib + numpy + ffmpeg) and a browser UI.

    python3 cutter.py                       # project stored in ./project
    python3 cutter.py --project ~/Movies/robot-build --a camA/ --b camB/

Open http://127.0.0.1:8765 (opened automatically).
"""

import argparse
import difflib
import hashlib
import json
import math
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
    for c in st["clips"]:
        if "interval_guess" not in c:
            c["interval_guess"] = guess_interval(c)
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


CHANGES = {"n": 0, "events": [], "checked": 0.0}


def refresh_changed():
    """Pick up video files replaced in place (e.g. re-exported with another framing):
    re-read their metadata and redo their analysis, keeping position and edits."""
    now = time.time()
    with LOCK:
        todo = []
        for c in STATE["clips"]:
            try:
                st = os.stat(c["path"])
            except OSError:
                continue
            # Wait until the file has stopped changing (an export may still be writing it).
            if (st.st_size != c.get("size") or abs(st.st_mtime - c.get("mtime", 0)) > 1) and now - st.st_mtime > 10:
                todo.append(c)
    events = []
    for c in todo:
        try:
            meta = probe(c["path"])
        except Exception:
            continue
        with LOCK:
            ev = {"id": c["id"], "name": c["name"], "framesBefore": c.get("frames"), "framesAfter": meta["frames"],
                  "sizeBefore": [c.get("width"), c.get("height")], "sizeAfter": [meta["width"], meta["height"]]}
            c.update(meta)
            c["interval_guess"] = guess_interval(c)
            c["rev"] = c.get("rev", 0) + 1
            shutil.rmtree(clip_dir(c["id"]), ignore_errors=True)
            ANALYSIS.pop(c["id"], None)
            queue_analysis(c["id"])
            events.append(ev)
    if events:
        with LOCK:
            save_state()
            CHANGES["n"] += 1
            CHANGES["events"] = (CHANGES["events"] + events)[-50:]
        print(f"Replaced file(s) picked up, re-analysing: {', '.join(e['name'] for e in events)}")


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


NICE_INTERVALS = (0.5, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 60)


def guess_interval(clip):
    """Photo interval of a timelapse clip from its file dates: recording start (metadata or
    file name) to the moment the camera closed the file, divided by the number of photos.
    For a re-exported file (e.g. from Insta360 Studio) the camera's original file is looked
    up next to it, by name. Camera clocks often miss a summer/winter time change, so a
    whole hour of difference is allowed. Only clean values (2 s, 5 s…) are trusted."""
    n = clip.get("frames") or 0
    if n < 30:
        return None
    stem = os.path.splitext(clip["name"])[0]
    here = os.path.dirname(clip["path"])
    up = os.path.dirname(here)
    ends = [clip.get("mtime")]
    try:
        dirs = [here] + [os.path.join(up, x) for x in os.listdir(up)]
    except OSError:
        dirs = [here]
    for d in dirs:
        try:
            for f in os.listdir(d):
                if os.path.splitext(f)[0] == stem and os.path.join(d, f) != clip["path"]:
                    ends.append(os.path.getmtime(os.path.join(d, f)))
        except OSError:
            continue
    best = None
    for start in {clip.get("creation_ts"), clip.get("name_ts")} - {None}:
        for end in filter(None, ends):
            for hour in (0, -3600, 3600):
                iv = (end - start + hour) / n
                for nice in NICE_INTERVALS:
                    err = abs(iv - nice) / nice
                    score = (err, hour != 0)
                    if err < 0.01 and (best is None or score < best[0]):
                        best = (score, nice)
    return best[1] if best else None


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
            clip["interval_guess"] = guess_interval(clip)
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


# ---------------------------------------------------------------- music generation (ACE-Step)

ACE_URL = os.environ.get("ACESTEP_URL", "http://127.0.0.1:8001")
ACE_SPACE = "ACE-Step/Ace-Step-v1.5"
MUSICGEN = {}


def ace_request(path, body=None, timeout=30):
    import urllib.request
    req = urllib.request.Request(ACE_URL + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if path.startswith("/v1/audio") else json.loads(data)


def ace_local_running():
    try:
        ace_request("/health", timeout=2)
        return True
    except Exception:
        return False


def ace_generate_local(prompt, lyrics, dur, out):
    r = ace_request("/release_task", {"prompt": prompt, "lyrics": lyrics, "audio_duration": dur, "batch_size": 1,
                                      "audio_format": "mp3", "thinking": True})
    if r.get("error"):
        raise RuntimeError(r["error"])
    tid = r["data"]["task_id"]
    while True:
        if MUSICGEN.get("cancel"):
            raise RuntimeError("cancelled")
        time.sleep(2)
        q = ace_request("/query_result", {"task_id_list": [tid]})["data"][0]
        if q["status"] == 2:
            raise RuntimeError(f"ACE-Step failed: {str(q.get('result'))[:300]}")
        if q["status"] == 1:
            res = json.loads(q["result"]) if isinstance(q["result"], str) else q["result"]
            with open(out, "wb") as f:
                f.write(ace_request(res[0]["file"], timeout=120))
            return


def ace_generate_space(prompt, lyrics, dur, out):
    try:
        from gradio_client import Client
        from huggingface_hub import get_token
    except ImportError:
        raise RuntimeError("ACE-Step isn't running on this Mac, and the online fallback needs "
                           "python3 -m pip install gradio_client")
    c = Client(ACE_SPACE, token=get_token(), verbose=False)
    res = c.predict(selected_model="acestep-v15-turbo", generation_mode="custom", simple_query_input="",
                    param_4=prompt, param_5=lyrics, param_14=None, param_15=dur, param_16=1, param_17=None,
                    param_18="", param_32=False, param_47=None, api_name="/generation_wrapper")
    src = res[0] if isinstance(res[0], str) else (res[0] or {}).get("path")
    if not src:
        raise RuntimeError(f"the online ACE-Step returned nothing: {res[10]}")
    shutil.copy(src, out)


def trim_trailing_silence(path):
    tmp = path + ".trim.mp3"
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", path, "-af",
                        "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.3,areverse",
                        "-c:a", "libmp3lame", "-q:a", "2", tmp], capture_output=True)
    if r.returncode == 0 and os.path.getsize(tmp) > 1000:
        os.replace(tmp, path)
    elif os.path.exists(tmp):
        os.remove(tmp)


def music_generate_worker(job):
    try:
        prompt = job["prompt"].strip() or "upbeat electronic, instrumental"
        lyrics = "[Instrumental]" if job.get("instrumental", True) else job.get("lyrics", "")
        durs = [min(600.0, max(10.0, float(d))) for d in job["durations"]]
        local = ace_local_running()
        if not local and not job.get("allowSpace"):
            raise RuntimeError("ACE-Step isn't running on this Mac")
        folder = os.path.join(PROJECT_DIR, "music")
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40] or "music"
        paths = []
        for i, d in enumerate(durs):
            MUSICGEN.update(step=f"song {i + 1}/{len(durs)} ({int(d // 60)}m{int(d % 60):02d})"
                                 + ("" if local else " on the online Space"), progress=i / len(durs))
            out = os.path.join(folder, f"{slug}-{stamp}-{i + 1}.mp3")
            (ace_generate_local if local else ace_generate_space)(prompt, lyrics, d, out)
            trim_trailing_silence(out)   # songs often end early; no gaps when they play back to back
            paths.append(out)
        added, errors = add_music(paths)
        if errors:
            raise RuntimeError("; ".join(errors))
        MUSICGEN.update(running=False, progress=1.0, step="done", added=[t["id"] for t in added])
    except Exception as e:
        traceback.print_exc()
        MUSICGEN.update(running=False, step="failed", error=str(e))


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

SCREEN = {"model": None, "sam": None, "lock": threading.Lock()}
SCREEN_MODEL = "google/owlv2-base-patch16-ensemble"
OUTLINE_MODEL = "facebook/sam-vit-base"   # outlines each found screen (~375 MB once)
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


def outline_model():
    if SCREEN["sam"] is None:
        from transformers import SamModel, SamProcessor
        torch, _, _, dev = screen_model()
        SCREEN["sam"] = (SamProcessor.from_pretrained(OUTLINE_MODEL),
                         SamModel.from_pretrained(OUTLINE_MODEL).to(dev).eval())
    return SCREEN["sam"]


def outline_screens(im, boxes):
    """Mask of each box's screen in image `im` (segment-anything with the box as prompt)."""
    torch, _, _, dev = screen_model()
    proc, model = outline_model()
    inp = proc(im, input_boxes=[boxes], return_tensors="pt")
    feed = {k: (v.float() if v.is_floating_point() else v).to(dev)
            for k, v in inp.items() if k in ("pixel_values", "input_boxes")}   # MPS has no float64
    with torch.no_grad():
        out = model(**feed)
    masks = proc.image_processor.post_process_masks(out.pred_masks.cpu(), inp["original_sizes"].cpu(),
                                                    inp["reshaped_input_sizes"].cpu())[0].numpy()
    ious = out.iou_scores.cpu().numpy()[0]
    res = []
    for i, b in enumerate(boxes):
        barea = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
        ok = [k for k in range(masks.shape[1]) if masks[i, k].sum() > 0.2 * barea]
        res.append(masks[i, max(ok, key=lambda k: ious[i, k])] if ok else None)
    return res


def grab_frame(path, t):
    from PIL import Image
    import io
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0, t):.3f}", "-i", path,
                        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def hull(pts):
    """Convex hull (monotone chain) of an (n, 2) array, counter-clockwise in math axes."""
    pts = sorted(set(map(tuple, pts)))
    if len(pts) < 3:
        return pts
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0:
            hi.pop()
        hi.append(p)
    return lo[:-1] + hi[:-1]


def cover(poly, pts, margin):
    """Push each side of convex polygon `poly` out until it covers all `pts`, plus `margin`."""
    cen = np.mean(poly, axis=0)
    lines = []
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        n = np.array([b[1] - a[1], a[0] - b[0]])
        n /= np.linalg.norm(n) or 1
        if np.dot(cen - a, n) > 0:
            n = -n   # outward
        lines.append((n, max(np.dot(pts - a, n).max(), 0.0) + np.dot(a, n) + margin))
    out = []
    for i in range(len(lines)):
        (n1, d1), (n2, d2) = lines[i - 1], lines[i]
        m = np.array([n1, n2])
        if abs(np.linalg.det(m)) < 1e-6:
            return None
        out.append(np.linalg.solve(m, [d1, d2]))
    return out


def poly_area(p):
    x, y = np.array(p).T
    return 0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))


def mask_polygon(mask, margin, max_pts=8, slack=1.08):
    """Corners (x, y in pixels; top-left first, then clockwise on screen) of a convex polygon
    covering a mask, each side pushed out by `margin` pixels. A screen seen at an angle is a
    trapezoid: 4 corners when that fits (within `slack` of the outline's area), more when the
    lens bends its edges."""
    rows = np.nonzero(mask.any(axis=1))[0]
    if len(rows) < 3:
        return None
    pts = []
    for y in rows:   # the outline is enough for a hull: leftmost and rightmost pixel per row
        xs = np.nonzero(mask[y])[0]
        pts += [(xs[0], y), (xs[-1] + 1, y), (xs[0], y + 1), (xs[-1] + 1, y + 1)]
    h = np.array(hull(np.array(pts)), float)
    if len(h) < 4:
        return None
    tri = lambda a, b, c: abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
    q = list(h)
    while len(q) > 12:   # simplify: drop the corner whose removal loses the least area
        i = min(range(len(q)), key=lambda i: tri(q[i - 1], q[i], q[(i + 1) % len(q)]))
        del q[i]
    target = poly_area(h)
    best = None
    for k in range(4, max_pts + 1):
        # the k corners of the simplified outline enclosing the most area, grown to cover it
        n = len(q)
        if n < k:
            break
        if k == 4 and n <= 12:
            from itertools import combinations
            cand = max(combinations(range(n), 4), key=lambda c: poly_area([q[i] for i in c]))
            sub = [q[i] for i in cand]
        else:
            sub = list(q)
            while len(sub) > k:
                i = min(range(len(sub)), key=lambda i: tri(sub[i - 1], sub[i], sub[(i + 1) % len(sub)]))
                del sub[i]
        out = cover(sub, h, 0)
        if out is None:
            continue
        best = (k, sub)
        if poly_area(out) <= slack * target:
            break
    if best is None:
        return None
    out = cover(best[1], h, margin)
    if out is None:
        return None
    cen = np.mean(out, axis=0)
    out.sort(key=lambda p: np.arctan2(p[1] - cen[1], p[0] - cen[0]))   # clockwise on screen (y down)
    k = min(range(len(out)), key=lambda i: out[i][0] + out[i][1])
    return [tuple(map(float, p)) for p in out[k:] + out[:k]]


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

    def iou(a, b):
        ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        area = lambda r: max(1e-6, (r[2] - r[0]) * (r[3] - r[1]))
        return inter / (area(a) + area(b) - inter)

    clusters = []   # the same screen across frames: {best detection, frames}
    for k, sc, lb, bx in sorted(dets, key=lambda d: -d[1]):
        c = next((c for c in clusters if iou(c["box"], bx) > 0.4), None)
        if c:
            c["frames"].add(k)
            c["boxes"].setdefault(k, list(bx))
        else:
            clusters.append({"box": list(bx), "boxes": {k: list(bx)}, "frames": {k}, "score": sc, "label": lb})
    need = max(1, -(-seen * 3 // 10))   # in at least 30% of the frames
    keep = [c for c in clusters if len(c["frames"]) >= need]
    # Outline each screen in every frame it was found in: a screen seen at an angle is a
    # trapezoid, so the blur follows its corners instead of its bounding box. The outlines
    # are merged, in case the camera or the screen moved during the part.
    note = None
    try:
        with SCREEN["lock"]:
            for k in sorted({k for c in keep for k in c["boxes"]}):
                cs = [c for c in keep if k in c["boxes"]]
                im = grab_frame(clip["path"], times[k])
                if im is None:
                    continue
                for c, m in zip(cs, outline_screens(im, [c["boxes"][k] for c in cs])):
                    if m is not None:
                        c["mask"] = m if c.get("mask") is None else c["mask"] | m
    except Exception as e:
        note = f"outlines unavailable ({e}), using rectangles"
        print("Screen outlines:", note)
    # A screen inside another one (a window on a monitor) adds nothing.
    for c in keep:
        m = c.get("mask")
        if m is not None and any(o is not c and o.get("mask") is not None and not o.get("inside")
                                 and (m & o["mask"]).sum() > 0.85 * m.sum() and o["mask"].sum() > m.sum()
                                 for o in keep):
            c["inside"] = True
    boxes = []
    margin = 0.012 * min(W, H)
    for c in keep:
        if c.get("inside"):
            continue
        pts = mask_polygon(c["mask"], margin) if c.get("mask") is not None else None
        if pts is None:
            x0, y0, x1, y1 = c["box"]
            px, py = 0.02 * W, 0.02 * H   # a little margin around the screen
            pts = [(x0 - px, y0 - py), (x1 + px, y0 - py), (x1 + px, y1 + py), (x0 - px, y1 + py)]
        pts = [(min(1.0, max(0.0, x / W)), min(1.0, max(0.0, y / H))) for x, y in pts]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if (max(xs) - min(xs)) * W < 4 or (max(ys) - min(ys)) * H < 4:
            continue
        boxes.append({"x": min(xs), "y": min(ys), "w": max(xs) - min(xs), "h": max(ys) - min(ys),
                      "pts": [[round(x, 5), round(y, 5)] for x, y in pts],
                      "score": round(c["score"], 2), "label": c["label"]})
    return {"boxes": boxes, "frames": seen, "note": note}


# ---------------------------------------------------------------- readable text

# Frames are read with Apple Vision through a small Swift helper (ocr.swift), compiled on
# first use. They are scaled to the export size first: text too small to read there is
# too small for viewers too.
TEXTSCAN = {}
TEXT_KINDS = [   # (kind, pattern), first match wins
    ("key", re.compile(r"\b(sk|pk|rk)[-_](live|test|proj|ant)?[-_]?[A-Za-z0-9]{12,}|\bgh[pousr]_[A-Za-z0-9]{20,}"
                       r"|\bgithub_pat_\w{20,}|\bhf_[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b|\bxox[abpr]-[\w-]{10,}"
                       r"|\bAIza[\w-]{30,}|\beyJ[\w-]{10,}\.[\w-]{10,}")),
    ("password", re.compile(r"pass(word|wd)?\b|mot de passe|\bmdp\b|secret|\btoken\b|api[ _-]?key|\bpin\b"
                            r"|\blogin\b|username|identifiant|2fa|\botp\b", re.I)),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}\b")),
    ("phone", re.compile(r"(?:\+\d{1,3}[ .-]?)?(?:\(?\d{1,4}\)?[ .-]?){3,5}\d{2,4}")),
    ("url", re.compile(r"https?://\S+|\bwww\.\S+|\b[\w-]+\.(com|fr|io|ai|co|org|net|dev|app)(/\S*)?\b", re.I)),
    ("token", re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{24,}\b")),
]


def luhn(digits):
    s = 0
    for i, d in enumerate(map(int, reversed(digits))):
        s += d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2)
    return s % 10 == 0


def text_kind(s):
    """What a piece of text found in a frame looks like: something private (key, password,
    email…), plain "text", or "minor" (a few characters, numbers: labels on parts and tools)."""
    digits = re.sub(r"[ .-]", "", s)
    if re.fullmatch(r"\d{13,19}", digits) and luhn(digits):
        return "card"
    for kind, rx in TEXT_KINDS:
        m = rx.search(s)
        if m and (kind != "phone" or len(re.sub(r"\D", "", m.group())) >= 9):
            return kind
    letters = re.findall(r"[A-Za-zÀ-ſ]", s)
    return "minor" if len(letters) < 4 else "text"


def ocr_binary():
    src = os.path.join(HERE, "ocr.swift")
    exe = os.path.join(CACHE_DIR, "ocr")
    if not os.path.exists(exe) or os.path.getmtime(exe) < os.path.getmtime(src):
        if not shutil.which("swiftc"):
            raise RuntimeError("the text reader needs the Swift compiler: run  xcode-select --install")
        r = subprocess.run(["swiftc", "-O", src, "-o", exe], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("could not build the text reader: " + r.stderr.strip()[-400:])
    return exe


def text_scan_worker(job):
    """Read the text in frames sampled from the kept parts. job["jobs"]: [{clip, s0, s1, step}]
    in clip seconds; job["width"], job["height"]: the export size."""
    tmp = os.path.join(CACHE_DIR, "textscan_tmp")
    try:
        exe = ocr_binary()
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        frames = []   # (path, clip id, clip time)
        jobs = job["jobs"]
        for i, j in enumerate(jobs):
            if TEXTSCAN.get("cancel"):
                raise RuntimeError("cancelled")
            with LOCK:
                clip = clip_by_id(j["clip"])
            if not clip or not os.path.isfile(clip["path"]):
                continue
            s0, s1, step = float(j["s0"]), float(j["s1"]), max(0.05, float(j["step"]))
            W, H = even(j.get("w", 1920)), even(j.get("h", 1080))   # its size in the export
            TEXTSCAN.update(step=f"taking frames from {clip['name']}", progress=0.2 * i / len(jobs))
            pat = os.path.join(tmp, f"j{i:04d}_%05d.jpg")
            r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{s0:.3f}", "-t", f"{max(step, s1 - s0):.3f}",
                                "-i", clip["path"], "-vf",
                                f"fps=1/{step:.4f},scale={W}:{H}:force_original_aspect_ratio=decrease",
                                "-q:v", "2", pat], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"{clip['name']}: " + r.stderr.strip()[-300:])
            n = len([f for f in os.listdir(tmp) if f.startswith(f"j{i:04d}_")])
            frames += [(pat % (k + 1), clip["id"], min(s1, s0 + k * step)) for k in range(n)]
        if not frames:
            raise RuntimeError("no frames to read")
        where = {p: (cid, t) for p, cid, t in frames}
        proc = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        threading.Thread(target=lambda: (proc.stdin.write("".join(p + "\n" for p, _, _ in frames)),
                                         proc.stdin.close()), daemon=True).start()
        found = []    # (clip id, t, text, box, conf)
        for k, line in enumerate(proc.stdout):
            if TEXTSCAN.get("cancel"):
                proc.kill()
                raise RuntimeError("cancelled")
            d = json.loads(line)
            cid, t = where[d["path"]]
            for x in d["lines"]:
                if x["text"].strip():
                    found.append((cid, t, x["text"].strip(), x["box"], x["conf"]))
            TEXTSCAN.update(step=f"reading frame {k + 1}/{len(frames)}",
                            progress=0.2 + 0.8 * (k + 1) / len(frames))
        proc.wait()
        # The same text in the same place on nearby samples is one finding, even when the
        # reader gets a letter or two wrong on some frames.
        hits = []
        steps = {j["clip"]: max(0.05, float(j["step"])) for j in jobs}
        norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())

        def same(h, cid, t, text, box):
            if h["clip"] != cid or t - h["s1"] > 4.5 * steps.get(cid, 1):
                return False
            a, b, m = h["box"], box, max(h["box"][3], box[3])
            if b[0] > a[0] + a[2] + m or a[0] > b[0] + b[2] + m or b[1] > a[1] + a[3] + m or a[1] > b[1] + b[3] + m:
                return False
            y = norm(text)
            return bool(y) and any(x and (x in y or y in x or difflib.SequenceMatcher(None, x, y).ratio() >= 0.7)
                                   for x in map(norm, h["reads"]))

        rank = lambda k: (k in ("text", "minor"), k == "minor")   # private kinds first
        for cid, t, text, box, conf in sorted(found, key=lambda f: (f[0], f[1])):
            h = next((h for h in reversed(hits[-80:]) if same(h, cid, t, text, box)), None)
            if h:
                h["s1"] = t
                h["n"] += 1
                h["reads"][text] = h["reads"].get(text, 0) + 1
                a = h["box"]
                x0, y0 = min(a[0], box[0]), min(a[1], box[1])
                h["box"] = [x0, y0, max(a[0] + a[2], box[0] + box[2]) - x0, max(a[1] + a[3], box[1] + box[3]) - y0]
            else:
                hits.append({"clip": cid, "s0": t, "s1": t, "box": list(box), "conf": round(conf, 2),
                             "n": 1, "reads": {text: 1}})
        for h in hits:
            reads = h.pop("reads")
            h["text"] = max(reads, key=lambda x: (reads[x], len(x)))
            h["kind"] = min((text_kind(x) for x in reads), key=rank)
            h["box"] = [round(v, 5) for v in h["box"]]
            if len(reads) > 1:
                h["also"] = sorted((x for x in reads if x != h["text"]), key=lambda x: -reads[x])[:5]
        TEXTSCAN.update(running=False, progress=1.0, step="done", hits=hits, frames=len(frames),
                        steps=steps)
        with open(os.path.join(CACHE_DIR, "textscan.json"), "w") as f:
            json.dump({"hits": hits, "frames": len(frames), "steps": steps, "at": time.time()}, f)
    except Exception as e:
        TEXTSCAN.update(running=False, error=str(e), step="failed")
        traceback.print_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


def fit(inp, w, h, fill, out, fps, timing):
    """`timing` = (speed, off): play `speed` times faster; the first decoded frame belongs
    `off` seconds (of the file) after the wanted start, which may be negative."""
    speed, off = timing
    if fill:
        f = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    else:
        f = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
    # tpad: hold the last frame if the file ends a frame short of what is asked.
    # fps from time 0 keeps whole-frame steps in the same place however a part is split.
    return (f"[{inp}]setpts=(PTS-STARTPTS+{off:.6f}/TB)/{speed:.6f},tpad=stop_mode=clone:stop_duration=2,"
            f"fps={fps}:start_time=0,{f},setsar=1,format=yuv420p[{out}]")


def shape_mask(b, size, folder):
    """Pixel crop (x, y, w, h) around an area with corners, and a mask image of its shape."""
    W, H = size
    xs = [min(1.0, max(0.0, float(p[0]))) * W for p in b["pts"]]
    ys = [min(1.0, max(0.0, float(p[1]))) * H for p in b["pts"]]
    x0, y0 = int(min(xs)) // 2 * 2, int(min(ys)) // 2 * 2
    cw = min(W - x0, max(2, -(-int(math.ceil(max(xs)) - x0) // 2) * 2))
    ch = min(H - y0, max(2, -(-int(math.ceil(max(ys)) - y0) // 2) * 2))
    path = os.path.join(folder, "mask-" + hashlib.sha1(f"{b['pts']}|{size}".encode()).hexdigest()[:12] + ".png")
    if not os.path.exists(path):
        from PIL import Image, ImageDraw
        m = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(m).polygon([(x - x0, y - y0) for x, y in zip(xs, ys)], fill=255)
        m.save(path)
    return (x0, y0, cw, ch), path


def blur_filter(src, boxes, sigma, out):
    """Blur each box (fractions of the frame) of input `src` into label `out`. A box with
    corners ("pts") comes with its pixel crop ("px") and a mask input ("mask")."""
    parts, cur = [], src
    for j, b in enumerate(boxes):
        p = f"{out}_{j}"
        if b.get("mask"):
            x0, y0, cw, ch = b["px"]
            parts.append(f"[{cur}]split=2[{p}m][{p}c];"
                         f"[{p}c]crop={cw}:{ch}:{x0}:{y0},gblur=sigma={sigma:.1f},format=yuva420p[{p}b];"
                         f"[{b['mask']}]format=gray,scale={cw}:{ch}[{p}k];[{p}b][{p}k]alphamerge[{p}a];"
                         f"[{p}m][{p}a]overlay=x={x0}:y={y0}:shortest=1[{p}o]")
            cur = f"{p}o"
            continue
        x, y, w, h = (min(1.0, max(0.0, float(b[k]))) for k in ("x", "y", "w", "h"))
        if w < 0.002 or h < 0.002:
            continue
        parts.append(f"[{cur}]split=2[{p}m][{p}c];"
                     f"[{p}c]crop=w=iw*{w:.5f}:h=ih*{h:.5f}:x=iw*{x:.5f}:y=ih*{y:.5f},gblur=sigma={sigma:.1f}[{p}b];"
                     f"[{p}m][{p}b]overlay=x=W*{x:.5f}:y=H*{y:.5f}[{p}o]")
        cur = f"{p}o"
    if not parts:
        return None
    return ";".join(parts) + f";[{cur}]null[{out}]"


def segment_filter(seg, lay, W, H, fps, speed, blurs=None, offs=None):
    """Build a filter graph for one segment. Inputs: 0 = A (if present), next = B.
    `blurs` maps "A"/"B" to (boxes, sigma) for the areas to blur in that source.
    A source shot at a longer photo interval (`stretch` > 1) is played slower."""
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
    sp = {s_: (speed / float((seg.get(s_.lower()) or {}).get("stretch") or 1), (offs or {}).get(s_, 0.0))
          for s_ in srcs}
    return ";".join(pre + [layout_filter(srcs, idx, lay, W, H, fps, sp, fill, mode)])


def layout_filter(srcs, idx, lay, W, H, fps, sp, fill, mode):
    if len(srcs) == 1:
        return fit(idx[srcs[0]], W, H, fill, "out", fps, sp[srcs[0]])
    if mode == "sbs":
        w2 = even(W / 2)
        return ";".join([fit(idx["A"], w2, H, fill, "l", fps, sp["A"]),
                         fit(idx["B"], W - w2, H, fill, "r", fps, sp["B"]),
                         "[l][r]hstack=inputs=2[out]"])
    if mode == "stack":
        h2 = even(H / 2)
        return ";".join([fit(idx["A"], W, h2, fill, "t", fps, sp["A"]),
                         fit(idx["B"], W, H - h2, fill, "b", fps, sp["B"]),
                         "[t][b]vstack=inputs=2[out]"])
    if mode == "blend":
        op = float(lay.get("opacity", 0.5))
        return ";".join([fit(idx["A"], W, H, fill, "a", fps, sp["A"]),
                         fit(idx["B"], W, H, fill, "b", fps, sp["B"]),
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
    return ";".join([fit(idx[main], W, H, fill, "m", fps, sp[main]),
                     fit(idx[inset], iw, ih, True, "i0", fps, sp[inset]),
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
    fps, speed = ctx["fps"], float(seg.get("speed") or ctx["speed"])   # parts can be sped up
    skip = first * speed / fps
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    blurs, offs = {}, {}
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
            sigma = max(6.0, 0.02 * min(clip["width"] or 1080, clip["height"] or 1080)) * ctx.get("blur", 1.0)
            if boxes and sigma >= 0.5:
                blurs[key.upper()] = (boxes, sigma, (clip["width"] or 1920, clip["height"] or 1080))
            k = float(s.get("stretch") or 1)   # timeline seconds per second of this file
            # Start on the file's frame shown at the wanted time, seeking half a frame before
            # it: seeking to the exact time drops a frame shown since just before (always the
            # case for a stretched clip, and at the very end of a clip it left nothing).
            cf = clip.get("fps") or fps
            want = s["t"] + skip / k
            n0 = min(int(math.floor(want * cf + 0.01)), max(0, (clip.get("frames") or 1) - 1))
            offs[key.upper()] = n0 / cf - want
            cmd += ["-ss", f"{max(0, (n0 - 0.5) / cf):.4f}", "-t", f"{(nframes + 2) * speed / fps / k + 1 / cf:.4f}",
                    "-i", clip["path"]]
    # Areas with corners (a screen seen at an angle) are blurred through a mask image of
    # their shape, one more input each.
    n_in = sum(1 for key in ("a", "b") if seg.get(key))
    for src, (boxes, sigma, size) in list(blurs.items()):
        out_boxes = []
        for b in boxes:
            b = dict(b)
            if b.get("pts") and len(b["pts"]) >= 3:
                b["px"], mpath = shape_mask(b, size, os.path.dirname(out))
                b["mask"] = f"{n_in}:v"
                n_in += 1
                cmd += ["-loop", "1", "-i", mpath]
            out_boxes.append(b)
        blurs[src] = (out_boxes, sigma)
    lay = seg.get("layout") or ctx["layout"]   # per-part layout overrides come with the segment
    cmd += ["-filter_complex", segment_filter(seg, lay, ctx["W"], ctx["H"], fps, speed, blurs, offs),
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


# ---------------------------------------------------------------- titles and counter

FONT = "/System/Library/Fonts/HelveticaNeue.ttc"   # regular = face 0, bold = face 1 (same as the preview)


def font(size, bold=False):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(FONT, int(size), index=1 if bold else 0)
    except OSError:
        return ImageFont.load_default(size=int(size))


def text_card(card, W, H, path):
    """A transparent W x H picture of one title: the intro and the end notes over a
    darkened frame, chapter titles at the bottom left."""
    from PIL import Image, ImageDraw
    k = H / 1080
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    shadow = (0, 0, 0, 150)

    def line(xy, text, f, fill=(255, 255, 255, 255), anchor="la"):
        d.text((xy[0] + 2 * k, xy[1] + 2 * k), text, font=f, fill=shadow, anchor=anchor)
        d.text(xy, text, font=f, fill=fill, anchor=anchor)

    if card["kind"] == "chapter":
        band = Image.new("L", (1, H), 0)   # soft shade under the title
        for y in range(H):
            band.putpixel((0, y), int(170 * max(0.0, (y - 0.55 * H) / (0.45 * H)) ** 1.4))
        im.paste(Image.new("RGBA", (W, H), (0, 0, 0, 255)), (0, 0), band.resize((W, H)))
        x, y = 80 * k, H - 90 * k
        if card.get("num"):
            line((x, y - 78 * k), card["num"].upper(), font(26 * k, True), (255, 214, 10, 255), "ls")
        line((x, y), card.get("title", ""), font(64 * k, True), anchor="ls")
    else:
        im.paste((0, 0, 0, 175 if card["kind"] == "outro" else 140), (0, 0, W, H))
        lines = [card.get("title", "")] + ([card["sub"]] if card.get("sub") else []) if card["kind"] == "intro" \
            else [l for l in (card.get("lines") or "").split("\n")]
        sizes = [(92 if card["kind"] == "intro" else 64) * k] + [40 * k] * (len(lines) - 1)
        gaps = [s * 1.35 for s in sizes]
        y = H / 2 - sum(gaps) / 2 + sizes[0] * 0.8
        for i, (l, sz) in enumerate(zip(lines, sizes)):
            if l.strip():
                line((W / 2, y), l, font(sz, i == 0), (255, 255, 255, 255) if i == 0 else (225, 228, 232, 255), "ms")
            y += gaps[i] if i + 1 >= len(gaps) else (gaps[i] + gaps[i + 1]) / 2
    im.save(path)


def counter_image(minutes, W, H, corner, path):
    from PIL import Image, ImageDraw
    k = H / 1080
    w, h = int(250 * k), int(112 * k)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if minutes is not None:
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=int(14 * k), fill=(0, 0, 0, 105))
        right = corner.endswith("r")
        x, anchor = (w - 22 * k, "r") if right else (22 * k, "l")
        for dx, fill in ((2 * k, (0, 0, 0, 150)), (0, (255, 255, 255, 235))):
            d.text((x + dx, 34 * k + dx), "BUILD TIME", font=font(20 * k, True),
                   fill=(255, 214, 10, 255) if dx == 0 else fill, anchor=anchor + "s")
            d.text((x + dx, 92 * k + dx), f"{minutes // 60} h {minutes % 60:02d}", font=font(56 * k, True),
                   fill=fill, anchor=anchor + "s")
    im.save(path)


def add_overlays(video, out, ov, ctx, tmp):
    """Draw the title cards and the build-time counter over the joined video."""
    W, H, fps = ctx["W"], ctx["H"], ctx["fps"]
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
                       capture_output=True, text=True)
    dur = float(r.stdout.strip())
    folder = os.path.join(tmp, "titles")
    os.makedirs(folder, exist_ok=True)
    inputs, chain, cur, n = [], [], "0:v", 1
    for i, c in enumerate(ov.get("cards") or []):
        t0, d = max(0.0, float(c["t0"])), float(c["dur"])
        d = min(d, dur - t0)
        if d <= 0.05:
            continue
        png = os.path.join(folder, f"card{i}.png")
        text_card(c, W, H, png)
        fi, fo = min(float(c.get("fadeIn", 0.6)), d / 2), min(float(c.get("fadeOut", 0.6)), d / 2)
        inputs += ["-loop", "1", "-framerate", f"{fps:g}", "-t", f"{d:.3f}", "-i", png]
        chain.append(f"[{n}:v]format=rgba,fade=t=in:st=0:d={fi:.3f}:alpha=1,"
                     f"fade=t=out:st={d - fo:.3f}:d={fo:.3f}:alpha=1,setpts=PTS-STARTPTS+{t0:.3f}/TB[c{n}];"
                     f"[{cur}][c{n}]overlay=eof_action=pass:enable='between(t,{t0:.3f},{t0 + d:.3f})'[v{n}]")
        cur, n = f"v{n}", n + 1
    cnt = ov.get("counter") or {}
    events = sorted((float(t), m) for t, m in cnt.get("events") or [])
    if events:
        corner = cnt.get("corner", "tl")
        lst, made = [], {}
        times = [0.0] + [t for t, _ in events] + [dur]
        values = [None] + [None if m is None or m < 0 else m for _, m in events]
        for (a, b), m in zip(zip(times, times[1:]), values):
            if b - a <= 1e-3:
                continue
            key = "off" if m is None else int(m)
            if key not in made:
                made[key] = os.path.join(folder, f"n_{key}.png")
                counter_image(None if m is None else int(m), W, H, corner, made[key])
            lst.append(f"file '{made[key]}'\nduration {b - a:.4f}\n")
        lst.append(f"file '{made[key]}'\n")   # the concat demuxer needs the last file twice
        txt = os.path.join(folder, "counter.txt")
        with open(txt, "w") as f:
            f.writelines(lst)
        inputs += ["-f", "concat", "-safe", "0", "-i", txt]
        m = int(60 * H / 1080)
        x = f"W-w-{m}" if corner.endswith("r") else str(m)
        y = f"H-h-{m}" if corner.startswith("b") else str(int(40 * H / 1080))
        chain.append(f"[{n}:v]format=rgba[k{n}];[{cur}][k{n}]overlay=x={x}:y={y}:eof_action=repeat[v{n}]")
        cur, n = f"v{n}", n + 1
    if cur == "0:v":
        shutil.copy(video, out)
        return
    run_ffmpeg(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", video, *inputs,
                "-filter_complex", ";".join(chain), "-map", f"[{cur}]", "-t", f"{dur:.3f}",
                *encoder_args(ctx["encoder"]), "-pix_fmt", "yuv420p", "-movflags", "+faststart", out], "titles")


def export_worker(job):
    try:
        out = job["output"]
        ctx = {"layout": job["layout"], "W": even(out["width"]), "H": even(out["height"]),
               "fps": float(job.get("fps", 30.0)), "speed": max(0.1, float(out.get("speed", 1.0))),
               "encoder": out.get("encoder"), "blur": max(0.0, float(job.get("blurStrength", 1.0)))}
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
        ov = job.get("overlays") or {}
        has_ov = bool(ov.get("cards") or (ov.get("counter") or {}).get("events"))
        joined = os.path.join(tmp, "joined.mp4") if music["tracks"] or has_ov else path
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                            "-i", lst, "-c", "copy", "-movflags", "+faststart", joined],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-800:])
        if has_ov:
            EXPORT["step"] = "adding titles"
            titled = os.path.join(tmp, "titled.mp4") if music["tracks"] else path
            add_overlays(joined, titled, ov, ctx, tmp)
            joined = titled
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
                                       "missing": missing_ids(), "musicMissing": music_missing(),
                                       "changes": CHANGES["n"]})
        if p == "/api/status":
            if time.time() - CHANGES["checked"] > 3:
                CHANGES["checked"] = time.time()
                refresh_changed()
            ex = {k: v for k, v in EXPORT.items() if k not in ("proc",)}
            with LOCK:
                miss = missing_ids()
            scan = {k: v for k, v in TEXTSCAN.items() if k not in ("hits", "steps")}
            return self.send_json({"analysis": ANALYSIS, "export": ex, "missing": miss,
                                   "changes": CHANGES["n"], "changed": CHANGES["events"],
                                   "musicgen": MUSICGEN, "textscan": scan})
        if p == "/api/textscan":
            if "hits" in TEXTSCAN:
                return self.send_json({k: TEXTSCAN[k] for k in ("hits", "frames", "steps")})
            f = os.path.join(CACHE_DIR, "textscan.json")
            if os.path.exists(f):
                return self.send_file(f, "application/json")
            return self.send_json({"hits": None})
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
                for k in ("cuts", "keeps", "splits", "overrides", "speeds", "hidden", "layout", "detect", "transitions",
                          "output", "settings", "tracks", "music", "blurs", "titles"):
                    if k in data:
                        STATE[k] = data[k]
                if "clips" in data:
                    upd = {c["id"]: c for c in data["clips"]}
                    for c in STATE["clips"]:
                        if c["id"] in upd:
                            c["start"] = float(upd[c["id"]]["start"])
                            c["track"] = upd[c["id"]]["track"]
                            c["interval"] = upd[c["id"]].get("interval") or None
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
        if p == "/api/textscan":
            if TEXTSCAN.get("running"):
                return self.send_json({"error": "a text check is already running"}, 409)
            if not data.get("jobs"):
                return self.send_json({"error": "no footage to check"}, 400)
            TEXTSCAN.clear()
            TEXTSCAN.update(running=True, progress=0.0, step="starting", started=time.time())
            threading.Thread(target=text_scan_worker, args=(data,), daemon=True).start()
            return self.send_json({"ok": True})
        if p == "/api/textscan/cancel":
            TEXTSCAN["cancel"] = True
            return self.send_json({"ok": True})
        if p == "/api/music/generate":
            if MUSICGEN.get("running"):
                return self.send_json({"error": "music is already being generated"}, 409)
            MUSICGEN.clear()
            MUSICGEN.update(running=True, progress=0.0, step="starting", started=time.time())
            threading.Thread(target=music_generate_worker, args=(data,), daemon=True).start()
            return self.send_json({"ok": True})
        if p == "/api/music/generate/cancel":
            MUSICGEN["cancel"] = True
            return self.send_json({"ok": True})
        if p == "/api/music/backend":
            return self.send_json({"local": ace_local_running(), "url": ACE_URL})
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
