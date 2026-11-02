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

app = FastAPI()
shazam = Shazam()


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
    cmd = [
        "ffmpeg", "-y", "-ss", str(max(start, 0)), "-t", str(CLIP_SECONDS),
        "-i", src, "-vn", "-ac", "1", "-ar", "44100", "-f", "wav", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0 or len(out) < 2000:
        raise ValueError("bad_audio")
    return out


async def recognize_shazam(wav_path: str):
    result = await asyncio.wait_for(shazam.recognize(wav_path), timeout=40)
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
    async with httpx.AsyncClient(timeout=40) as client:
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


@app.post("/api/recognize")
async def recognize(file: UploadFile = File(...), start: float = Form(0)):
    raw = await file.read()
    if len(raw) > MAX_BYTES:
        return JSONResponse({"error": "too_big"}, status_code=413)

    suffix = Path(file.filename or "").suffix or ".bin"
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in" + suffix)
        with open(src, "wb") as f:
            f.write(raw)
        try:
            wav = await to_wav(src, start)
        except ValueError:
            return JSONResponse({"error": "bad_audio"}, status_code=422)

        wav_path = os.path.join(tmp, "clip.wav")
        with open(wav_path, "wb") as f:
            f.write(wav)

        info = None
        try:
            info = await recognize_shazam(wav_path)
        except Exception as e:  # Shazam غیررسمی است؛ خطا را نادیده می‌گیریم و پشتیبان را امتحان می‌کنیم
            print("shazam error:", repr(e))

        if not info:
            try:
                info = await recognize_acr(wav)
            except Exception as e:
                print("acrcloud error:", repr(e))

    if not info:
        return {"found": False}
    return {"found": True, **add_links(info)}
