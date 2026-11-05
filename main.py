import asyncio
import base64
import hashlib
import hmac
import html
import io
import json
import os
import tempfile
import time
import urllib.parse
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageDraw
from shazamio import Shazam

BASE = Path(__file__).parent
MAX_BYTES = 30 * 1024 * 1024  # حداکثر حجم فایل ورودی (برنامه‌ی وب)
CLIP_SECONDS = 15  # مقدار صدایی که برای تشخیص فرستاده می‌شود
FFMPEG_TIMEOUT = 40  # حداکثر زمان تبدیل صدا
TOTAL_TIMEOUT = 75  # حداکثر زمان کل یک شناسایی

# ---------- تنظیمات ربات تلگرام (از Environment در رندر خوانده می‌شود) ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
ALLOWED = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()
}
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
TG_MAX = 20 * 1024 * 1024  # محدودیت تلگرام برای دانلود فایل توسط ربات
seen_updates = deque(maxlen=300)

shazam = Shazam()


def log(*args):
    print("[recognize]", *args, flush=True)


async def setup_webhook():
    base = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not (BOT_TOKEN and WEBHOOK_SECRET and base):
        log("telegram bot disabled (BOT_TOKEN / WEBHOOK_SECRET / RENDER_EXTERNAL_URL missing)")
        return
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{TG_API}/setWebhook",
                json={
                    "url": f"{base}/telegram/webhook",
                    "secret_token": WEBHOOK_SECRET,
                    "allowed_updates": ["message"],
                },
            )
        log("setWebhook:", r.text)
    except Exception as e:
        log("setWebhook error:", repr(e))


@asynccontextmanager
async def lifespan(app_: FastAPI):
    asyncio.create_task(setup_webhook())
    yield


app = FastAPI(lifespan=lifespan)


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


# ---------- ربات تلگرام ----------

WELCOME = (
    "سلام! 👋\n"
    "یک پیام صوتی، فایل آهنگ (mp3) یا ویدیو برایم بفرستید تا نام آهنگ را پیدا کنم.\n\n"
    "نکته: اگر آهنگ از اول فایل شروع نمی‌شود، موقع فرستادن فایل در بخش توضیح (caption) "
    "ثانیه‌ی شروع را بنویسید. مثلاً 30 یا 1:20\n\n"
    "محدودیت: فایل‌ها تا ۲۰ مگابایت."
)


def parse_start(text) -> float:
    t = (text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٫", "0123456789."))
    try:
        if ":" in t:
            m, s = t.split(":", 1)
            return max(int(m) * 60 + float(s), 0)
        return max(float(t), 0)
    except Exception:
        return 0


def pick_file(msg: dict):
    """(file_id, size, suffix) برمی‌گرداند یا None."""
    if msg.get("voice"):
        v = msg["voice"]
        return v["file_id"], v.get("file_size", 0), ".ogg"
    if msg.get("audio"):
        a = msg["audio"]
        suffix = Path(a.get("file_name") or "").suffix or ".mp3"
        return a["file_id"], a.get("file_size", 0), suffix
    if msg.get("video"):
        v = msg["video"]
        return v["file_id"], v.get("file_size", 0), ".mp4"
    if msg.get("video_note"):
        v = msg["video_note"]
        return v["file_id"], v.get("file_size", 0), ".mp4"
    doc = msg.get("document")
    if doc and (doc.get("mime_type") or "").startswith(("audio/", "video/")):
        suffix = Path(doc.get("file_name") or "").suffix or ".bin"
        return doc["file_id"], doc.get("file_size", 0), suffix
    return None


async def tg_send(chat_id, text, reply_to=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{TG_API}/sendMessage", json=payload)
            if r.status_code != 200:
                log("tg_send failed:", r.text[:300])
    except Exception as e:
        log("tg_send error:", repr(e))


def song_label(info: dict) -> str:
    return f"{info.get('artist') or ''} - {info.get('title') or ''}".strip(" -")


def result_text(info: dict) -> str:
    title = html.escape(info.get("title") or "")
    artist = html.escape(info.get("artist") or "")
    label = html.escape(song_label(info))
    return f"🎵 <b>{title}</b>\n👤 {artist}\n\n<code>{label}</code>"


def result_markups(info: dict) -> list:
    """ابتدا با دکمه‌ی کپی؛ اگر تلگرام نپذیرفت، بدون آن."""
    row = [
        {"text": "▶️ یوتیوب", "url": info["youtube"]},
        {"text": "🟢 اسپاتیفای", "url": info["spotify"]},
    ]
    basic = {"inline_keyboard": [row]}
    label = song_label(info)
    if not label:
        return [basic]
    full = {
        "inline_keyboard": [
            row,
            [{"text": "📋 کپی نام آهنگ", "copy_text": {"text": label[:256]}}],
        ]
    }
    return [full, basic]


async def tg_send_result(chat_id, info: dict, reply_to=None):
    caption = result_text(info)
    cover = info.get("cover")
    base = {"chat_id": chat_id, "parse_mode": "HTML"}
    if reply_to:
        base["reply_to_message_id"] = reply_to
        base["allow_sending_without_reply"] = True

    async with httpx.AsyncClient(timeout=30) as c:
        for markup in result_markups(info):
            # اول با عکس کاور
            if cover:
                try:
                    r = await c.post(
                        f"{TG_API}/sendPhoto",
                        json={**base, "photo": cover, "caption": caption, "reply_markup": markup},
                    )
                    if r.status_code == 200:
                        return
                    log("sendPhoto failed:", r.text[:300])
                except Exception as e:
                    log("sendPhoto error:", repr(e))
            # اگر عکس نشد، فقط متن
            try:
                r = await c.post(
                    f"{TG_API}/sendMessage",
                    json={**base, "text": caption, "reply_markup": markup, "disable_web_page_preview": True},
                )
                if r.status_code == 200:
                    return
                log("sendMessage failed:", r.text[:300])
            except Exception as e:
                log("sendMessage error:", repr(e))


async def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    user_id = (msg.get("from") or {}).get("id")
    mid = msg.get("message_id")
    log(f"telegram message from user={user_id}")

    if ALLOWED and user_id not in ALLOWED:
        await tg_send(chat_id, "شما اجازه‌ی استفاده از این ربات را ندارید.", mid)
        return

    text = msg.get("text") or ""
    if text.startswith("/start") or text.startswith("/help"):
        await tg_send(chat_id, WELCOME)
        return

    picked = pick_file(msg)
    if not picked:
        await tg_send(chat_id, "لطفاً یک پیام صوتی، فایل آهنگ یا ویدیو بفرستید. راهنما: /help", mid)
        return

    file_id, size, suffix = picked
    if size and size > TG_MAX:
        await tg_send(chat_id, "این فایل بزرگ‌تر از ۲۰ مگابایت است. فایل کوچک‌تری بفرستید.", mid)
        return

    start = parse_start(msg.get("caption"))
    await tg_send(chat_id, "در حال شناسایی… (اگر سرور خواب بوده، تا یک دقیقه طول می‌کشد)", mid)

    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(f"{TG_API}/getFile", params={"file_id": file_id})
            data = r.json()
            if not data.get("ok"):
                log("getFile failed:", r.text[:300])
                await tg_send(chat_id, "فایل دریافت نشد. شاید بیش از ۲۰ مگابایت است.", mid)
                return
            fr = await c.get(f"{TG_FILE}/{data['result']['file_path']}")
            raw = fr.content
    except Exception as e:
        log("download error:", repr(e))
        await tg_send(chat_id, "دریافت فایل از تلگرام شکست خورد. دوباره امتحان کنید.", mid)
        return

    log(f"telegram file {len(raw)} bytes, suffix={suffix}, start={start}")
    try:
        info = await asyncio.wait_for(process(raw, suffix, start), timeout=TOTAL_TIMEOUT)
    except ValueError:
        await tg_send(chat_id, "این فایل صدا ندارد یا خوانده نشد. فایل دیگری امتحان کنید.", mid)
        return
    except asyncio.TimeoutError:
        await tg_send(chat_id, "شناسایی بیش از حد طول کشید. کمی بعد دوباره امتحان کنید.", mid)
        return

    if not info:
        await tg_send(
            chat_id,
            "آهنگ پیدا نشد. از بخش دیگری از آهنگ (با نوشتن ثانیه‌ی شروع در توضیح فایل) امتحان کنید.",
            mid,
        )
        return
    await tg_send_result(chat_id, add_links(info), mid)


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, background: BackgroundTasks):
    if not BOT_TOKEN or not WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=404)
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=403)
    update = await request.json()
    uid = update.get("update_id")
    if uid in seen_updates:  # تلگرام گاهی همان پیام را دوباره می‌فرستد
        return {"ok": True}
    seen_updates.append(uid)
    msg = update.get("message")
    if msg:
        background.add_task(handle_message, msg)
    return {"ok": True}
