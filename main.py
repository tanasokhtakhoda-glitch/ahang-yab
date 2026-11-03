import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import tempfile
import time
import urllib.parse
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageDraw
from shazamio import Shazam

BASE = Path(__file__).parent
MAX_BYTES = 30 * 1024 * 1024  # حداکثر حجم فایل ورودی
CLIP_SECONDS = 15  # مقدار صدایی که برای تشخیص فرستاده می‌شود
FFMPEG_TIMEOUT = 40  # حداکثر زمان تبدیل صدا
TOTAL_TIMEOUT = 75  # حداکثر زمان کل یک شناسایی

app = FastAPI()
shazam = Shazam()


def log(*args):
    print("[recognize]", *args, flush=True)


# ---------- صفحه‌ها و فایل‌های PWA ----------

@app.get("/")
async def index():
    return FileResponse(BASE / "index.html", media_type="text/html")


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/manifest.webmanifest")
async def manifest():
    data = {
        "name": "آهنگ‌یاب",
        "short_name": "آهنگ‌یاب",
        "lang": "fa",
        "dir": "rtl",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0b5c63",
        "theme_color": "#0b5c63",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(json.dumps(data, ensure_ascii=False), media_type="application/manifest+json")


SW_JS = """
const CACHE = 'ahang-yab-v1';
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(['/'])));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api')) return;
  e.respondWith(
    fetch(e.request)
      .then((r) => { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return r; })
      .catch(() => caches.match(e.request))
  );
});
"""


@app.get("/sw.js")
async def sw():
    return Response(SW_JS, media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


def make_icon(size: int) -> bytes:
    img = Image.new("RGB", (size, size), "#0b5c63")
    d = ImageDraw.Draw(img)
    c = size / 2
    for r, col in ((0.46, "#13727a"), (0.36, "#1d8a92")):
        d.ellipse([c - size * r, c - size * r, c + size * r, c + size * r], fill=col)
    r = size * 0.24
    d.ellipse([c - r, c - r, c + r, c + r], fill="#f6f1e4")
    r2 = size * 0.07
    d.ellipse([c - r2, c - r2, c + r2, c + r2], fill="#f4b73f")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@app.get("/icon-{size}.png")
async def icon(size: int):
    size = 512 if size >= 512 else 192
    return Response(make_icon(size), media_type="image/png")


# ---------- تشخیص آهنگ ----------

async def to_wav(src: str, start: float) -> bytes:
    # خروجی را در یک فایل واقعی می‌نویسیم تا هدر wav درست و کامل باشد
    out_path = src + ".clip.wav"
    cmd = [
        "ffmpeg", "-y", "-ss", str(max(start, 0)), "-t", str(CLIP_SECONDS),
        "-i", src, "-vn", "-ac", "1", "-ar", "44100", out_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        log("ffmpeg timeout")
        raise ValueError("bad_audio")
    if proc.returncode != 0 or not os.path.exists(out_path):
        log("ffmpeg error:", err.decode(errors="ignore")[-500:])
        raise ValueError("bad_audio")
    with open(out_path, "rb") as f:
        data = f.read()
    if len(data) < 2000:
        log("ffmpeg output too small:", len(data))
        raise ValueError("bad_audio")
    return data


async def recognize_shazam(wav_path: str):
    result = await asyncio.wait_for(shazam.recognize(wav_path), timeout=30)
    track = (result or {}).get("track")
    if not track:
        return None
    return {
        "title": track.get("title"),
        "artist": track.get("subtitle"),
        "cover": (track.get("images") or {}).get("coverart"),
        "source": "Shazam",
    }


async def recognize_acr(wav: bytes):
    host = os.getenv("ACR_HOST")
    key = os.getenv("ACR_KEY")
    secret = os.getenv("ACR_SECRET")
    if not (host and key and secret):
        return None
    ts = str(int(time.time()))
    to_sign = "\n".join(["POST", "/v1/identify", key, "audio", "1", ts])
    sig = base64.b64encode(
        hmac.new(secret.encode(), to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    data = {
        "access_key": key,
        "sample_bytes": str(len(wav)),
        "timestamp": ts,
        "signature": sig,
        "data_type": "audio",
        "signature_version": "1",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://{host}/v1/identify", data=data, files={"sample": ("sample.wav", wav)}
        )
    music = ((r.json() or {}).get("metadata") or {}).get("music") or []
    if not music:
        return None
    m = music[0]
    return {
        "title": m.get("title"),
        "artist": ", ".join(a.get("name", "") for a in m.get("artists", [])),
        "cover": None,
        "source": "ACRCloud",
    }


def add_links(info: dict) -> dict:
    q = urllib.parse.quote(f"{info['artist']} {info['title']}".strip())
    info["youtube"] = f"https://www.youtube.com/results?search_query={q}"
    info["spotify"] = f"https://open.spotify.com/search/{q}"
    return info


async def process(raw: bytes, suffix: str, start: float):
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in" + suffix)
        with open(src, "wb") as f:
            f.write(raw)

        wav = await to_wav(src, start)
        log(f"ffmpeg ok, wav={len(wav)} bytes, {time.time() - t0:.1f}s")

        wav_path = os.path.join(tmp, "clip.wav")
        with open(wav_path, "wb") as f:
            f.write(wav)

        info = None
        t1 = time.time()
        try:
            info = await recognize_shazam(wav_path)
            log(f"shazam done, found={bool(info)}, {time.time() - t1:.1f}s")
        except Exception as e:  # Shazam غیررسمی است؛ خطا را ثبت می‌کنیم و پشتیبان را امتحان می‌کنیم
            log("shazam error:", repr(e), f"after {time.time() - t1:.1f}s")

        if not info:
            t2 = time.time()
            try:
                info = await recognize_acr(wav)
                log(f"acrcloud done, found={bool(info)}, {time.time() - t2:.1f}s")
            except Exception as e:
                log("acrcloud error:", repr(e))
        return info


@app.post("/api/recognize")
async def recognize(file: UploadFile = File(...), start: float = Form(0)):
    raw = await file.read()
    log(f"received {len(raw)} bytes, name={file.filename}, start={start}")
    if len(raw) > MAX_BYTES:
        return JSONResponse({"error": "too_big"}, status_code=413)

    suffix = Path(file.filename or "").suffix or ".bin"
    try:
        info = await asyncio.wait_for(process(raw, suffix, start), timeout=TOTAL_TIMEOUT)
    except ValueError:
        return JSONResponse({"error": "bad_audio"}, status_code=422)
    except asyncio.TimeoutError:
        log("overall timeout")
        return JSONResponse({"error": "timeout"}, status_code=504)

    if not info:
        log("not found")
        return {"found": False}
    log("found:", info.get("title"))
    return {"found": True, **add_links(info)}
