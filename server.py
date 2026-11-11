#!/usr/bin/env python3
"""Local web UI for Disc Image Studio's Linux CLI (DVD pictures).

Standard library + Pillow only. Binds to 127.0.0.1 only.
Run:  python3 server.py   then open http://127.0.0.1:8765
"""
import colorsys, io, json, math, os, re, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw, ImageFilter, ImageChops

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = Path(os.environ.get("DIS_CLI", ROOT / "cli" / "disc-image-studio"))
BUILTIN = Path(os.environ.get(
    "DIS_PRESETS", ROOT / "repo/src/DiscImageStudio.Core/Presets/built-in-disc-presets.json"))
TARGET = ROOT / "repo/src/DiscImageStudio.Core/Calibration/Patterns/target_slim.png"
DATA = Path(os.environ.get("DATA_DIR", HERE))
USER_PRESETS = DATA / "presets.user.json"
UPLOADS = DATA / "work" / "uploads"
OUT = DATA / "output"
KEEP_ISOS = 3
PORT = int(os.environ.get("PORT", 8765))
for d in (UPLOADS, OUT):
    d.mkdir(parents=True, exist_ok=True)

JOBS = {}
LOCK = threading.Lock()

# ---------------------------------------------------------------- presets
NAMES = {
    "verbatim-dvd-r-azo-43533": "Verbatim DVD-R AZO 43533",
    "verbatim-dvd-plus-r-azo-43498": "Verbatim DVD+R AZO 43498",
    "philips-dvd-plus-r": "Philips DVD+R",
    "ritek-dvd-plus-r-e-series": "Ritek DVD+R E-series",
    "ritek-dvd-r-e-series": "Ritek DVD-R E-series",
}
KEYS = ("totalSectors", "innerRadiusMm", "outerRadiusMm", "channelBitLengthNm",
        "pitchLinear", "pitchQuadratic", "pitchCubic")


def load_presets():
    out = []
    try:
        data = json.loads(BUILTIN.read_text(encoding="utf-8"))
        for p in data.get("dvdPresets", []):
            if "innerRadiusMm" not in p:
                continue
            out.append({"id": p["id"], "name": NAMES.get(p["id"], p["id"]),
                        "source": "built-in", "params": {k: p.get(k, 0) for k in KEYS}})
    except Exception as e:  # keep UI usable without the repo
        print("built-in presets unavailable:", e, file=sys.stderr)
    user = []
    if USER_PRESETS.exists():
        try:
            user = json.loads(USER_PRESETS.read_text(encoding="utf-8"))
        except Exception:
            user = []
    ids = {p["id"]: i for i, p in enumerate(out)}
    for p in user:
        p["source"] = "user"
        if p["id"] in ids:
            out[ids[p["id"]]] = p
        else:
            out.append(p)
    return out


def save_user_preset(p):
    user = []
    if USER_PRESETS.exists():
        user = json.loads(USER_PRESETS.read_text(encoding="utf-8"))
    user = [u for u in user if u["id"] != p["id"]] + [p]
    USER_PRESETS.write_text(json.dumps(user, indent=2), encoding="utf-8")


def delete_user_preset(pid):
    if not USER_PRESETS.exists():
        return
    user = [u for u in json.loads(USER_PRESETS.read_text(encoding="utf-8")) if u["id"] != pid]
    USER_PRESETS.write_text(json.dumps(user, indent=2), encoding="utf-8")


def clean_params(src):
    p = {k: float(src.get(k, 0)) for k in KEYS}
    p["totalSectors"] = int(p["totalSectors"])
    if p["totalSectors"] <= 0 or p["totalSectors"] % 16:
        raise ValueError("totalSectors must be > 0 and a multiple of 16")
    if not (10 < p["innerRadiusMm"] < p["outerRadiusMm"] < 61):
        raise ValueError("radii out of range (need 10 < inner < outer < 61 mm)")
    if p["channelBitLengthNm"] <= 0:
        p["channelBitLengthNm"] = 133.3
    return p


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "preset"


# ---------------------------------------------------------------- imaging
def to_square(img, s, fit, zoom, dx, dy):
    """Return an L-mode s x s image. fit: 'contain' (letterbox white) or 'cover'."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = ImageOps.exif_transpose(img).convert("L")
    w, h = img.size
    base = (s / max(w, h)) if fit == "contain" else (s / min(w, h))
    sc = base * zoom
    nw, nh = max(1, round(w * sc)), max(1, round(h * sc))
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("L", (s, s), 255)
    canvas.paste(img, (round((s - nw) / 2 + dx * s), round((s - nh) / 2 + dy * s)))
    return canvas


def tone(img, q):
    gamma = float(q.get("gamma", 1.0))
    bright = float(q.get("brightness", 0))      # -100..100
    contrast = float(q.get("contrast", 1.0))
    invert = bool(q.get("invert", False))
    lut = []
    for v in range(256):
        x = (v / 255) ** (1 / max(gamma, 0.05))
        x = (x - 0.5) * contrast + 0.5 + bright / 255
        x = min(1.0, max(0.0, x))
        lut.append(round((1 - x if invert else x) * 255))
    return img.point(lut)


def auto_tone(img, q):
    """Pick gamma / brightness / contrast / threshold / dither for a good 1-bit result."""
    sq = to_square(img, 256, q.get("fit", "contain"), float(q.get("zoom", 1)),
                   float(q.get("dx", 0)), float(q.get("dy", 0)))
    mask = ring_mask(256, 0.35, 1.0)                 # only the printed ring counts
    hist = sq.histogram(mask)
    n = sum(hist) or 1

    def pct(h, p):
        acc = 0
        for v, c in enumerate(h):
            acc += c
            if acc >= n * p:
                return v
        return 255

    lo, hi = pct(hist, 0.02), pct(hist, 0.98)
    if hi - lo < 20:
        lo, hi = 0, 255
    contrast = min(2.2, max(0.6, 255.0 / (hi - lo)))
    bright = max(-100, min(100, -((lo + hi) / 2 / 255 - 0.5) * contrast * 255))
    t = {"gamma": 1.0, "brightness": bright, "contrast": contrast}
    stretched = tone(sq, t)
    h2 = stretched.histogram(mask)
    mid = h2[20:236]
    mid_n = sum(mid)
    line_art = mid_n < 0.25 * n                       # mostly pure black/white: leave gamma alone
    gamma = 1.0
    if not line_art:
        acc, med = 0, 128
        for v, c in enumerate(mid):
            acc += c
            if acc >= mid_n * 0.5:
                med = v + 20
                break
        mu = min(0.9, max(0.1, med / 255))
        gamma = max(0.75, min(1.5, math.log(mu) / math.log(0.5)))
    t["gamma"] = gamma
    final = tone(sq, t)
    h3 = final.histogram(mask)
    total = sum(h3) or 1
    sumall = sum(i * c for i, c in enumerate(h3))
    wb = sb = 0
    best, thr = -1, 128
    for i, c in enumerate(h3):                        # Otsu threshold
        wb += c
        if not wb:
            continue
        wf = total - wb
        if not wf:
            break
        sb += i * c
        mb, mf = sb / wb, (sumall - sb) / wf
        between = wb * wf * (mb - mf) ** 2
        if between > best:
            best, thr = between, i
    dither = 0.15 if line_art else 0.65
    return {"gamma": round(gamma, 2), "brightness": round(bright), "contrast": round(contrast, 2),
            "threshold": int(max(60, min(190, thr))), "tone_dither": dither, "tone_brightness": 0}


def ring_mask(s, inner, outer):
    m = Image.new("L", (s * 4, s * 4), 0)
    d = ImageDraw.Draw(m)
    c = s * 2
    ro, ri = c, c * inner / outer
    d.ellipse((c - ro, c - ro, c + ro, c + ro), fill=255)
    d.ellipse((c - ri, c - ri, c + ri, c + ri), fill=0)
    return m.resize((s, s), Image.LANCZOS)


def approx_preview(img, q, params, size=640):
    """Instant, approximate 1-bit look. The accurate render uses the real engine."""
    sq = tone(to_square(img, size, q.get("fit", "contain"), float(q.get("zoom", 1)),
                        float(q.get("dx", 0)), float(q.get("dy", 0))), q)
    s = float(q.get("tone_dither", 0.5))
    thr = int(q.get("threshold", 128))
    hard = sq.point(lambda v: 255 if v >= thr else 0)
    fs = sq.convert("1", dither=Image.FLOYDSTEINBERG).convert("L")
    mixed = Image.blend(hard, fs, s)
    bg = Image.new("L", (size, size), 24)
    mask = ring_mask(size, params["innerRadiusMm"], params["outerRadiusMm"])
    out = Image.composite(mixed, bg, mask)
    return out


def realistic(png_path, inner, outer, light=300.0, rainbow=0.8, size=900):
    """Photo-style mock-up of the burnt disc. Cosmetic only: it shows how the engine
    readback would modulate a real disc's reflection, not an optical simulation."""
    S = size
    c = S / 2
    k = (S * 0.46) / 60.0                        # px per mm, disc radius 60 mm
    G = Image.open(png_path).convert("L")
    w = round(outer * k * 2)
    G = G.resize((w, w), Image.LANCZOS)
    pic = Image.new("L", (S, S), 255)
    pic.paste(G, (round(c - w / 2), round(c - w / 2)))

    def disc_mask(r0, r1):
        m = Image.new("L", (S * 2, S * 2), 0)
        d = ImageDraw.Draw(m)
        d.ellipse((S - r1 * k * 2, S - r1 * k * 2, S + r1 * k * 2, S + r1 * k * 2), fill=255)
        d.ellipse((S - r0 * k * 2, S - r0 * k * 2, S + r0 * k * 2, S + r0 * k * 2), fill=0)
        return m.resize((S, S), Image.LANCZOS)

    # backdrop: soft dark desk with vignette
    bg = Image.new("RGB", (S, S), (38, 40, 46))
    vig = Image.radial_gradient("L").resize((S, S))
    bg = Image.composite(Image.new("RGB", (S, S), (14, 15, 18)), bg, vig.point(lambda v: min(255, int(v * 0.9))))

    # conic rainbow, two lobes opposite each other, hue drifting with angle
    rb = Image.new("RGB", (S, S), (0, 0, 0))
    d = ImageDraw.Draw(rb)
    wide = Image.new("L", (S, S), 0)
    wd = ImageDraw.Draw(wide)
    step = 1.5
    a = 0.0
    while a < 360:
        rel = math.radians(a - light)
        lobe = max(0.0, math.cos(3 * rel)) ** 3.0
        hue = ((a - light) % 120) / 120.0 * 0.85
        r_, g_, b_ = colorsys.hsv_to_rgb(hue, 0.9, 1.0)
        f = lobe * rainbow
        d.pieslice((c - S, c - S, c + S, c + S), a - 90, a - 90 + step + 0.6,
                   fill=(int(r_ * 255 * f), int(g_ * 255 * f), int(b_ * 255 * f)))
        wv = int(255 * (0.5 + 0.5 * math.cos(rel)) ** 1.5)
        wd.pieslice((c - S, c - S, c + S, c + S), a - 90, a - 90 + step + 0.6, fill=wv)
        a += step
    rb = rb.filter(ImageFilter.GaussianBlur(3))
    wide = wide.filter(ImageFilter.GaussianBlur(25))

    # silver/teal base with faint concentric track texture
    base = Image.new("RGB", (S, S), (112, 106, 158))
    tex = Image.new("L", (S, S), 128)
    td = ImageDraw.Draw(tex)
    r = 22.0
    while r < 58:
        td.ellipse((c - r * k, c - r * k, c + r * k, c + r * k), outline=128 + int(10 * math.sin(r * 5)))
        r += 0.35
    tex = tex.filter(ImageFilter.GaussianBlur(0.6))
    base = Image.blend(base, Image.merge("RGB", (tex, tex, tex)), 0.18)

    # the picture: bright engine pixels reflect, dark pixels absorb
    # on a real disc the picture is only a faint difference in reflectivity, so keep it soft and low contrast
    gain = pic.point(lambda v: int(176 + 79 * (v / 255)))
    # the picture washes out where the light hits strongly (inside the rainbow wedges and on the lit side)
    # and shows best away from them, so moving the light or the rainbow reveals different parts
    wash = ImageChops.lighter(rb.convert("L").point(lambda v: min(255, int(v * 1.6))), wide.point(lambda v: int(v * 0.75)))
    gain = Image.composite(Image.new("L", (S, S), 245), gain, wash)
    lit = Image.merge("RGB", [gain] * 3)
    surface = ImageChops.multiply(ImageChops.add(base, rb), lit.point(lambda v: v))
    surface = surface.point(lambda v: min(255, int(v * 1.3)))

    # specular streak along the light axis
    spec = Image.new("L", (S, S), 0)
    sd = ImageDraw.Draw(spec)
    sd.pieslice((c - S, c - S, c + S, c + S), light - 90 - 5, light - 90 + 5, fill=150)
    sd.pieslice((c - S, c - S, c + S, c + S), light + 90 - 4, light + 90 + 4, fill=90)
    spec = spec.filter(ImageFilter.GaussianBlur(18))
    surface = Image.composite(Image.new("RGB", (S, S), (255, 255, 255)), surface, spec.point(lambda v: v // 2))

    out = bg.copy()
    data = disc_mask(22.0, 59.0)
    out = Image.composite(surface, out, data)
    # outer rim and clamp ring
    out = Image.composite(Image.new("RGB", (S, S), (200, 208, 214)), out, disc_mask(58.2, 59.6).point(lambda v: v // 2))
    hub = disc_mask(15.0, 22.0)
    clear = Image.blend(bg, Image.new("RGB", (S, S), (170, 178, 186)), 0.22)
    out = Image.composite(clear, out, hub)
    out = Image.composite(Image.new("RGB", (S, S), (225, 230, 235)), out, disc_mask(21.4, 23.2).point(lambda v: v // 3))
    hole = disc_mask(0, 7.5)
    out = Image.composite(Image.new("RGB", (S, S), (22, 23, 27)), out, hole)
    out = Image.composite(Image.new("RGB", (S, S), (210, 214, 220)), out, disc_mask(7.5, 8.3).point(lambda v: v // 2))
    return out


# ---------------------------------------------------------------- jobs
def run_job(job, kind, img_path, q, params):
    def log(msg):
        job["log"] = (job["log"] + [msg])[-40:]

    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tag = f"{kind}-{stamp}"
        wdir = HERE / "work" / job["id"]
        wdir.mkdir(parents=True, exist_ok=True)
        iso = OUT / f"{tag}.iso"
        png = OUT / f"{tag}.png"

        job["stage"] = "preparing image"
        if kind == "calibration":
            src = Image.open(TARGET)
            sq = to_square(src, 2048, "contain", 1, 0, 0)
            sq = sq.point(lambda v: 255 if v >= 128 else 0)
        else:
            src = Image.open(img_path)
            sq = tone(to_square(src, 2048, q.get("fit", "contain"), float(q.get("zoom", 1)),
                                float(q.get("dx", 0)), float(q.get("dy", 0))), q)
        inp = wdir / "input.png"
        sq.save(inp)

        total = params["totalSectors"]
        geo = ["--inner-radius-mm", str(params["innerRadiusMm"]),
               "--outer-radius-mm", str(params["outerRadiusMm"]),
               "--channel-bit-nm", str(params["channelBitLengthNm"]),
               "--pitch-linear", str(params["pitchLinear"]),
               "--pitch-quadratic", str(params["pitchQuadratic"]),
               "--pitch-cubic", str(params["pitchCubic"])]
        cmd = [str(CLI), "solve", "--image", str(inp), "--total-sectors", str(total),
               "--iso-output", str(iso), "--fill-sectors", str(total), "--lba", "0",
               "--algorithm", "dispersion", "--fast-output", "true", "--fast-parallelism", "0",
               "--initial", "zero",
               "--tone-dither", str(float(q.get("tone_dither", 0.5))),
               "--tone-brightness", str(int(q.get("tone_brightness", 0))),
               "--image-threshold", str(int(q.get("threshold", 128)))] + geo
        job["stage"] = "generating disc image"
        log(" ".join(cmd[1:4]) + " ...")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            m = re.match(r"block (\d+)/(\d+)", line)
            if m:
                job["progress"] = round(0.85 * int(m.group(1)) / int(m.group(2)), 3)
            else:
                log(line.strip())
        if p.wait() != 0:
            raise RuntimeError("solve failed: " + " | ".join(job["log"][-3:]))

        job["stage"] = "rendering accurate preview"
        job["progress"] = 0.88
        cmd = [str(CLI), "simulate", "--iso", str(iso), "--output", str(png)] + geo
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("simulate failed: " + (r.stdout + r.stderr)[-300:])
        job.update(state="done", stage="done", progress=1.0, iso=str(iso), iso_show=(os.environ["ISO_DISPLAY_DIR"].rstrip("/") + "/" + iso.name) if os.environ.get("ISO_DISPLAY_DIR") else str(iso), png=png.name,
                   iso_size=iso.stat().st_size)
        prune()
    except Exception as e:
        job.update(state="error", stage="error", error=str(e))


def prune():
    isos = sorted(OUT.glob("*.iso"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in isos[KEEP_ISOS:]:
        for f in (old, old.with_suffix(".png"), Path(str(old) + ".json")):
            f.unlink(missing_ok=True)


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit=40 * 1024 * 1024):
        n = int(self.headers.get("Content-Length", 0))
        if n > limit:
            raise ValueError("body too large")
        return self.rfile.read(n)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        m = re.fullmatch(r"/static/((?:fonts/)?[A-Za-z0-9_.-]+\.(?:css|woff2|svg))", path)
        if m and (HERE / "static" / m.group(1)).exists():
            ct = {"css": "text/css", "woff2": "font/woff2", "svg": "image/svg+xml"}[m.group(1).rsplit(".", 1)[1]]
            return self.send(200, (HERE / "static" / m.group(1)).read_bytes(), ct)
        if path == "/api/presets":
            return self.send(200, load_presets())
        if path == "/api/status":
            return self.send(200, {"cli": CLI.exists(), "presets": BUILTIN.exists(),
                                   "target": TARGET.exists()})
        m = re.fullmatch(r"/api/job/([0-9a-f]{8})", path)
        if m:
            j = JOBS.get(m.group(1))
            return self.send(200 if j else 404, j or {"error": "unknown job"})
        if path == "/api/realistic":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            name = re.sub(r"[^A-Za-z0-9_.-]", "", qs.get("png", [""])[0])
            f = OUT / name
            if not f.exists():
                return self.send(404, {"error": "no render yet"})
            img = realistic(f, float(qs.get("inner", ["24"])[0]), float(qs.get("outer", ["58"])[0]),
                            float(qs.get("light", ["300"])[0]), float(qs.get("rainbow", ["0.8"])[0]))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return self.send(200, buf.getvalue(), "image/png")
        m = re.fullmatch(r"/output/([A-Za-z0-9_.-]+\.png)", path)
        if m and (OUT / m.group(1)).exists():
            return self.send(200, (OUT / m.group(1)).read_bytes(), "image/png")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        try:
            path = self.path.split("?")[0]
            if path == "/api/upload":
                data = self.body()
                img = Image.open(io.BytesIO(data))
                img.load()
                iid = uuid.uuid4().hex[:8]
                img.save(UPLOADS / f"{iid}.png")
                return self.send(200, {"image_id": iid, "w": img.width, "h": img.height})
            q = json.loads(self.body() or b"{}")
            if path == "/api/auto":
                f = UPLOADS / f"{re.sub('[^0-9a-f]', '', q.get('image_id', ''))}.png"
                return self.send(200, auto_tone(Image.open(f), q))
            if path == "/api/preview":
                f = UPLOADS / f"{re.sub('[^0-9a-f]', '', q.get('image_id', ''))}.png"
                params = clean_params(q["params"])
                out = approx_preview(Image.open(f), q, params)
                buf = io.BytesIO()
                out.save(buf, "PNG")
                return self.send(200, buf.getvalue(), "image/png")
            if path in ("/api/render", "/api/calibration-target"):
                if any(j["state"] == "running" for j in JOBS.values()):
                    return self.send(409, {"error": "a job is already running"})
                params = clean_params(q["params"])
                kind = "calibration" if path.endswith("target") else "render"
                f = None
                if kind == "render":
                    f = UPLOADS / f"{re.sub('[^0-9a-f]', '', q.get('image_id', ''))}.png"
                    if not f.exists():
                        raise ValueError("upload an image first")
                jid = uuid.uuid4().hex[:8]
                job = {"id": jid, "state": "running", "stage": "queued", "progress": 0.0, "log": []}
                JOBS[jid] = job
                threading.Thread(target=run_job, args=(job, kind, f, q, params), daemon=True).start()
                return self.send(200, {"job": jid})
            if path == "/api/presets":
                pid = slug(q.get("name", "custom"))
                p = {"id": "user-" + pid, "name": q.get("name", "Custom")[:60],
                     "params": clean_params(q["params"])}
                save_user_preset(p)
                return self.send(200, p)
            if path == "/api/presets/import":
                # accepts a GUI *.calibration.json (uses its "parameters" block)
                par = q.get("parameters") or (q.get("target") or {}).get("parameters")
                if not par:
                    raise ValueError("no parameters block found")
                params = clean_params({
                    "totalSectors": par["sectors"], "innerRadiusMm": par["innerRadiusMm"],
                    "outerRadiusMm": par["outerRadiusMm"],
                    "channelBitLengthNm": par.get("linearDensity", 133.3),
                    "pitchLinear": par.get("pitchLinear", 0),
                    "pitchQuadratic": par.get("pitchQuadratic", 0),
                    "pitchCubic": par.get("pitchCubic", 0)})
                name = q.get("name", "Imported calibration")
                p = {"id": "user-" + slug(name), "name": name, "params": params}
                save_user_preset(p)
                return self.send(200, p)
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(400, {"error": str(e)})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/presets/(user-[a-z0-9-]+)", self.path)
        if m:
            delete_user_preset(m.group(1))
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"CLI: {CLI} ({'ok' if CLI.exists() else 'MISSING'})")
    HOST = os.environ.get("HOST", "127.0.0.1")
    print(f"Open http://127.0.0.1:{PORT}")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
