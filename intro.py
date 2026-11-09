"""Opening sequence and end card, in Claude's look: serif titles, one warm accent, calm
backgrounds, quick confident motion.

Two looks: "dark" (near-black like the robot, its orange as the accent, cream text) and
"cream" (warm cream, coral accent, dark text).

The opening, over `dur` seconds (about 10):
  1. the hook: one or two videos (e.g. the first walk) popping in as rounded cards, at real speed;
  2. a photo (e.g. all the parts on the floor) with a question landing word by word;
  3. the title and subtitle, then a fade into the first frame of the timelapse.
Frames are drawn with PIL and piped to ffmpeg.
"""

import subprocess

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

LOOKS = {
    "dark": {"bg": (22, 21, 20), "ink": (240, 238, 230), "muted": (165, 159, 147), "accent": (242, 106, 33),
             "shadow": 120, "edge": (60, 57, 53)},
    "cream": {"bg": (240, 238, 230), "ink": (20, 20, 19), "muted": (110, 106, 96), "accent": (217, 119, 87),
              "shadow": 70, "edge": None},
}
SERIF = "/System/Library/Fonts/NewYork.ttf"
SANS = "/System/Library/Fonts/HelveticaNeue.ttc"


def serif(size, weight=400):
    """New York at an optical size for text: its display cut has hairlines too thin for
    video (the H's crossbar vanishes)."""
    try:
        f = ImageFont.truetype(SERIF, int(size))
        f.set_variation_by_axes([min(256, max(12, size * 0.35)), weight, 0])
        return f
    except (OSError, ValueError):
        return ImageFont.truetype(SANS, int(size))


def clamp01(x):
    return min(1.0, max(0.0, x))


def ease(x):   # smooth start and end
    x = clamp01(x)
    return x * x * (3 - 2 * x)


def snap(x):   # fast out, soft landing
    x = clamp01(x)
    return 1 - (1 - x) ** 3


def pop(x):    # a little overshoot
    x = clamp01(x)
    c = 1.9
    return 1 + (c + 1) * (x - 1) ** 3 + c * (x - 1) ** 2


def ramp(t, a, b, curve=ease):   # 0 before a, 1 after b
    return curve((t - a) / (b - a)) if b > a else float(t >= a)


def mix(c0, c1, a):
    return tuple(int(c0[j] + (c1[j] - c0[j]) * a) for j in range(3))


def rounded_mask(w, h, r):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=255)
    return m


class Card:
    """A rounded picture with a soft shadow (and a thin edge on dark), pasted with an
    opacity, a scale and an offset."""

    def __init__(self, w, h, k, look):
        self.w, self.h, self.k, self.look = w, h, k, look
        self.r = int(26 * k)
        self.mask = rounded_mask(w, h, self.r)
        pad = self.pad = int(40 * k)
        s = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
        ImageDraw.Draw(s).rounded_rectangle((pad, pad + int(12 * k), pad + w, pad + h + int(12 * k)),
                                            radius=self.r, fill=look["shadow"])
        self.shadow = s.filter(ImageFilter.GaussianBlur(20 * k))

    def paste(self, canvas, img, cx, cy, alpha, scale=1.0):
        """Centred on (cx, cy)."""
        if alpha <= 0.01:
            return
        mask, shadow = self.mask, self.shadow
        if abs(scale - 1) > 0.004:
            w, h = max(2, int(self.w * scale)), max(2, int(self.h * scale))
            img, mask = img.resize((w, h), Image.BILINEAR), mask.resize((w, h), Image.BILINEAR)
            shadow = shadow.resize((w + 2 * self.pad, h + 2 * self.pad), Image.BILINEAR)
        w, h = mask.size
        x, y = int(cx - w / 2), int(cy - h / 2)
        canvas.paste((0, 0, 0), (x - self.pad, y - self.pad), shadow.point(lambda v: int(v * alpha)))
        canvas.paste(img, (x, y), mask if alpha >= 0.99 else mask.point(lambda v: int(v * alpha)))
        if self.look["edge"]:
            ImageDraw.Draw(canvas).rounded_rectangle((x, y, x + w - 1, y + h - 1), radius=int(self.r * scale),
                                                     outline=mix(self.look["bg"], self.look["edge"], alpha), width=2)


class VideoFeed:
    """Frames of a video clip, scaled and cropped to w x h, one per output frame."""

    def __init__(self, path, start, length, w, h, fps, bg):
        self.w, self.h = w, h
        self.last = Image.new("RGB", (w, h), bg)
        self.p = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", path,
             "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps}",
             "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)

    def next(self):
        buf = self.p.stdout.read(self.w * self.h * 3)
        if len(buf) == self.w * self.h * 3:
            self.last = Image.frombuffer("RGB", (self.w, self.h), buf)
        return self.last   # holds the last frame when the clip is shorter

    def close(self):
        self.p.kill()


def wrap_words(words, font, max_w, draw):
    """Words in lines no wider than max_w, as even as possible (no lone word at the end)."""
    width = lambda ws: draw.textlength(" ".join(ws), font=font)
    for n in range(1, len(words) + 1):
        lines, cur = [], []
        target = width(words) / n
        for w in words:
            if cur and width(cur + [w]) > target * 1.08 and len(lines) < n - 1:
                lines.append(cur)
                cur = []
            cur.append(w)
        lines.append(cur)
        if all(width(l) <= max_w for l in lines):
            return lines
    return [[w] for w in words]


def encoder(W, H, fps, out, enc):
    return subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                             "-s", f"{W}x{H}", "-r", f"{fps:g}", "-i", "-", *enc, "-pix_fmt", "yuv420p",
                             "-video_track_timescale", "60000", "-movflags", "+faststart", out],
                            stdin=subprocess.PIPE)


def render_intro(spec, W, H, fps, out, enc):
    """spec: hook [{path, start}], photo, question, title, sub, dur, look, next_frame (an image
    of the timelapse's first frame, faded into at the end). enc: ffmpeg encoder arguments."""
    L = LOOKS.get(spec.get("look") or "dark", LOOKS["dark"])
    bg, ink, muted, accent = L["bg"], L["ink"], L["muted"], L["accent"]
    k = H / 1080
    D = float(spec.get("dur", 10))
    n = int(round(D * fps))
    hook = [h for h in spec.get("hook") or [] if h.get("path")][:2]
    t1 = 0.40 * D                  # scene 1 -> 2
    t2 = 0.72 * D                  # scene 2 -> 3
    tf = D - 0.6                   # fade into the timelapse

    # scene 1: the hook videos as portrait cards, side by side
    ch = int(0.82 * H)
    cw = int(ch * 9 / 16)
    gap = int(44 * k)
    feeds, cards, cxs = [], [], []
    total = len(hook) * cw + (len(hook) - 1) * gap
    for i, h in enumerate(hook):
        feeds.append(VideoFeed(h["path"], float(h.get("start", 0)), t1 + 0.6, cw, ch, fps, bg))
        cards.append(Card(cw, ch, k, L))
        cxs.append((W - total) / 2 + i * (cw + gap) + cw / 2)

    # scene 2: the photo card on the left, the question on the right
    photo = None
    if spec.get("photo"):
        im = ImageOps.exif_transpose(Image.open(spec["photo"])).convert("RGB")
        ph = int(0.82 * H)
        pw = min(int(ph * im.width / im.height), int(0.42 * W))
        photo = ImageOps.fit(im, (pw, ph), Image.LANCZOS)
        pcard = Card(pw, ph, k, L)
    pcx = int(0.08 * W) + (photo.width if photo else 0) / 2
    tx = int(0.08 * W) + (photo.width if photo else 0) + int(90 * k)
    qfont = serif(92 * k, 600)
    words = (spec.get("question") or "").split()
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    qlines = wrap_words(words, qfont, W - tx - int(0.06 * W), probe)
    accent_from = max(0, len(words) - 2)   # the last words in the accent colour

    # scene 3: title and subtitle, centred
    tfont, sfont = serif(116 * k, 700), serif(44 * k, 450)
    title, sub = spec.get("title") or "", spec.get("sub") or ""

    nxt = Image.open(spec["next_frame"]).convert("RGB").resize((W, H)) if spec.get("next_frame") else None
    enc_p = encoder(W, H, fps, out, enc)
    try:
        for f in range(n):
            t = f / fps
            cv = Image.new("RGB", (W, H), bg)
            d = ImageDraw.Draw(cv)
            # scene 1: cards pop in, one after the other; slide out left
            out1 = ramp(t, t1 - 0.25, t1 + 0.05, snap)
            frames = [fd.next() for fd in feeds] if t < t1 + 0.1 else []
            for i, img in enumerate(frames):
                p = ramp(t, 0.1 * i, 0.45 + 0.1 * i)
                cards[i].paste(cv, img, cxs[i] - out1 * 140 * k, H / 2, min(1, p * 1.6) * (1 - out1),
                               0.86 + 0.14 * pop((t - 0.1 * i) / 0.45))
            # scene 2: the photo slides in, the words land one by one
            a2 = ramp(t, t1 - 0.05, t1 + 0.2) * (1 - ramp(t, t2 - 0.2, t2 + 0.05))
            if a2 > 0.01:
                slide = (1 - ramp(t, t1 - 0.05, t1 + 0.4, snap)) * -160 * k
                if photo:
                    pcard.paste(cv, photo, pcx + slide, H / 2, a2)
                lh = qfont.size * 1.18
                y = H / 2 - lh * len(qlines) / 2 + qfont.size * 0.1
                wi = 0
                for line in qlines:
                    x = tx
                    for w in line:
                        wa = a2 * ramp(t, t1 + 0.3 + 0.08 * wi, t1 + 0.48 + 0.08 * wi, snap)
                        if wa > 0.01:
                            col = accent if wi >= accent_from else ink
                            d.text((x, y + (1 - wa) * 26 * k), w, font=qfont, fill=mix(bg, col, wa))
                        x += d.textlength(w + " ", font=qfont)
                        wi += 1
                    y += lh
            # scene 3: the title lands, the accent bar sweeps, the subtitle follows
            a3 = ramp(t, t2, t2 + 0.3, snap) * (1 - ramp(t, tf - 0.35, tf + 0.05))
            if a3 > 0.01:
                cy = H / 2 - 20 * k
                d.text((W / 2, cy + (1 - ramp(t, t2, t2 + 0.35, snap)) * 30 * k), title, font=tfont,
                       fill=mix(bg, ink, a3), anchor="ms")
                bar = ramp(t, t2 + 0.15, t2 + 0.55, snap) * 200 * k * (1 - ramp(t, tf - 0.35, tf + 0.05))
                d.rectangle((W / 2 - bar / 2, cy + 38 * k, W / 2 + bar / 2, cy + 43 * k), fill=accent)
                a4 = ramp(t, t2 + 0.35, t2 + 0.7) * (1 - ramp(t, tf - 0.35, tf + 0.05))
                d.text((W / 2, cy + 116 * k), sub, font=sfont, fill=mix(bg, muted, a4), anchor="ms")
            # into the timelapse
            if nxt is not None and t > tf:
                cv = Image.blend(cv, nxt, ramp(t, tf, D - 1 / fps))
            enc_p.stdin.write(cv.tobytes())
    finally:
        enc_p.stdin.close()
        enc_p.wait()
        for fd in feeds:
            fd.close()
    if enc_p.returncode != 0:
        raise RuntimeError("intro: ffmpeg failed")


def render_outro(spec, W, H, fps, out, enc):
    """The closing card: the last frame of the timelapse fades into the background, then a big
    line (e.g. "Thanks for watching"), logos side by side, and a few small lines under them.
    spec: lines (text, the first one big), logos [paths], logoSizes [%], dur, look, prev_frame."""
    L = LOOKS.get(spec.get("look") or "dark", LOOKS["dark"])
    bg, ink, muted, accent = L["bg"], L["ink"], L["muted"], L["accent"]
    k = H / 1080
    D = float(spec.get("dur", 6))
    n = int(round(D * fps))
    lines = (spec.get("lines") or "").split("\n")
    head, rest = (lines[0] if lines else ""), [l for l in lines[1:] if l.strip()]
    hfont, sfont = serif(100 * k, 700), serif(36 * k, 450)
    logos = []
    lh = int(110 * k)
    sizes = spec.get("logoSizes") or []
    for i, p in enumerate(spec.get("logos") or []):
        try:
            im = ImageOps.exif_transpose(Image.open(p)).convert("RGBA")
        except OSError:
            continue
        im = im.crop(im.getchannel("A").getbbox() or (0, 0, im.width, im.height))   # no empty margins
        box = lh * (float(sizes[i]) / 100 if i < len(sizes) and sizes[i] else 1)   # logos differ: a size each
        w = int(im.width * box / im.height)
        if w > 3.2 * box:   # a wide wordmark: limit its width instead
            w, h = int(3.2 * box), int(im.height * 3.2 * box / im.width)
        else:
            h = int(box)
        im = im.resize((max(1, w), max(1, h)), Image.LANCZOS)
        if L is LOOKS["dark"]:   # dark marks (e.g. a black wordmark) would vanish: lighten them
            px = im.load()
            for yy in range(im.height):
                for xx in range(im.width):
                    r, g, b, a = px[xx, yy]
                    if a and r + g + b < 160:
                        px[xx, yy] = (*ink, a)
        logos.append(im)
    gap = int(90 * k)
    total = sum(l.width for l in logos) + gap * max(0, len(logos) - 1)
    prev = Image.open(spec["prev_frame"]).convert("RGB").resize((W, H)) if spec.get("prev_frame") else None
    enc_p = encoder(W, H, fps, out, enc)
    try:
        for f in range(n):
            t = f / fps
            cv = Image.new("RGB", (W, H), bg)
            d = ImageDraw.Draw(cv)
            top = H / 2 - (60 if logos else 0) * k - (len(rest) * 24 * k)
            a = ramp(t, 0.5, 0.8, snap)
            d.text((W / 2, top + (1 - a) * 30 * k), head, font=hfont, fill=mix(bg, ink, a), anchor="ms")
            bar = ramp(t, 0.65, 1.05, snap) * 200 * k
            d.rectangle((W / 2 - bar / 2, top + 40 * k, W / 2 + bar / 2, top + 45 * k), fill=accent)
            y = top + 100 * k
            if logos:
                x = (W - total) / 2
                for i, im in enumerate(logos):
                    la = ramp(t, 0.95 + 0.15 * i, 1.3 + 0.15 * i, snap)
                    if la > 0.01:
                        al = im.getchannel("A").point(lambda v: int(v * la))
                        cv.paste(im.convert("RGB"), (int(x), int(y + (lh - im.height) / 2 + (1 - la) * 20 * k)), al)
                    x += im.width + gap
                y += lh + 70 * k
            for i, l in enumerate(rest):
                sa = ramp(t, 1.4 + 0.12 * i, 1.8 + 0.12 * i)
                d.text((W / 2, y + i * 50 * k), l, font=sfont, fill=mix(bg, muted, sa), anchor="ms")
            if prev is not None and t < 0.5:
                cv = Image.blend(prev, cv, ramp(t, 0, 0.5))
            enc_p.stdin.write(cv.tobytes())
    finally:
        enc_p.stdin.close()
        enc_p.wait()
    if enc_p.returncode != 0:
        raise RuntimeError("outro: ffmpeg failed")
