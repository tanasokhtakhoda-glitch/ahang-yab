import asyncio
import html
import os
import traceback
from pathlib import Path

import httpx

import db

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
try:
    OWNER_ID = int(os.getenv("OWNER_ID", "").strip())
except ValueError:
    OWNER_ID = 0

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
TG_MAX = 20 * 1024 * 1024  # محدودیت تلگرام برای دانلود فایل توسط ربات
TOTAL_TIMEOUT = 75  # حداکثر زمان یک شناسایی

_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def log(*args):
    print("[bot]", *args, flush=True)


def fa(n) -> str:
    """عدد را با ارقام فارسی و جداکننده‌ی هزارگان برمی‌گرداند."""
    return f"{int(n):,}".replace(",", "٬").translate(_FA)


# ---------- ارتباط با تلگرام ----------

async def tg(method: str, payload: dict = None, timeout: int = 30) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{TG_API}/{method}", json=payload or {})
        data = r.json()
        if not data.get("ok"):
            log(method, "failed:", str(data)[:300])
        return data
    except Exception as e:
        log(method, "error:", repr(e))
        return {"ok": False}


async def send_message(chat_id, text, reply_to=None, markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    if markup:
        payload["reply_markup"] = markup
    return await tg("sendMessage", payload)


async def edit_message(chat_id, message_id, text, markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if markup:
        payload["reply_markup"] = markup
    return await tg("editMessageText", payload)


async def setup_webhook():
    base = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    log(f"database={'on' if db.ENABLED else 'off'}, owner={'set' if OWNER_ID else 'MISSING'}")
    if not (BOT_TOKEN and WEBHOOK_SECRET and base):
        log("telegram bot disabled (BOT_TOKEN / WEBHOOK_SECRET / RENDER_EXTERNAL_URL missing)")
        return
    data = await tg(
        "setWebhook",
        {
            "url": f"{base}/telegram/webhook",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message", "callback_query", "my_chat_member"],
        },
        timeout=20,
    )
    log("setWebhook:", data)
    if db.ENABLED:
        await db.cleanup_updates()


# ---------- مدیران ----------

async def is_admin(user_id) -> bool:
    if not user_id:
        return False
    if OWNER_ID and user_id == OWNER_ID:
        return True
    if not db.ENABLED:
        return False
    return user_id in await db.admin_ids()


MENU_TEXT = "⚙️ <b>تنظیمات مدیریت</b>\n\nیک گزینه را انتخاب کنید:"
DB_OFF_TEXT = (
    "⚠️ اتصال به پایگاه داده برقرار نیست.\n"
    "متغیرهای SUPABASE_URL و SUPABASE_SECRET_KEY را در رندر بررسی کنید."
)


def menu_markup() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "📊 آمار کاربران", "callback_data": "adm:users"}],
            [{"text": "🎵 آمار شناسایی آهنگ", "callback_data": "adm:songs"}],
        ]
    }


BACK_MARKUP = {"inline_keyboard": [[{"text": "🔙 بازگشت", "callback_data": "adm:menu"}]]}
SETTINGS_BUTTON = {"inline_keyboard": [[{"text": "⚙️ تنظیمات", "callback_data": "adm:menu"}]]}


async def users_stats_text() -> str:
    if not db.ENABLED:
        return DB_OFF_TEXT
    try:
        s = await db.user_stats()
    except Exception as e:
        log("user_stats error:", repr(e))
        return "⚠️ خواندن آمار از پایگاه داده شکست خورد. لاگ رندر را ببینید."
    return (
        "📊 <b>آمار کاربران</b>\n\n"
        f"👥 کل کاربران: <b>{fa(s['total'])}</b>\n"
        f"✅ فعال (ربات را مسدود نکرده‌اند): <b>{fa(s['active'])}</b>\n"
        f"🚫 مسدودکننده‌ی ربات: <b>{fa(s['blocked'])}</b>\n\n"
        f"🆕 جدید امروز: <b>{fa(s['new_today'])}</b>\n"
        f"🆕 جدید در ۷ روز اخیر: <b>{fa(s['new_week'])}</b>\n"
        f"🔥 فعال در ۲۴ ساعت اخیر: <b>{fa(s['active_day'])}</b>"
    )


async def songs_stats_text() -> str:
    if not db.ENABLED:
        return DB_OFF_TEXT
    try:
        s = await db.song_stats()
    except Exception as e:
        log("song_stats error:", repr(e))
        return "⚠️ خواندن آمار از پایگاه داده شکست خورد. لاگ رندر را ببینید."
    return (
        "🎵 <b>آمار شناسایی آهنگ</b>\n\n"
        f"🔢 کل شناسایی‌های موفق: <b>{fa(s['total'])}</b>\n"
        f"📅 امروز: <b>{fa(s['today'])}</b>\n"
        f"🗓 ۷ روز اخیر: <b>{fa(s['week'])}</b>"
    )


async def handle_callback(cb: dict):
    uid = (cb.get("from") or {}).get("id")
    cb_id = cb.get("id")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    data = cb.get("data") or ""

    if not data.startswith("adm:"):
        await tg("answerCallbackQuery", {"callback_query_id": cb_id})
        return
    if not await is_admin(uid):
        await tg(
            "answerCallbackQuery",
            {"callback_query_id": cb_id, "text": "شما دسترسی ندارید.", "show_alert": True},
        )
        return
    await tg("answerCallbackQuery", {"callback_query_id": cb_id})
    if not chat_id or not message_id:
        return

    action = data[4:]
    if action == "menu":
        text, markup = MENU_TEXT, menu_markup()
    elif action == "users":
        text, markup = await users_stats_text(), BACK_MARKUP
    elif action == "songs":
        text, markup = await songs_stats_text(), BACK_MARKUP
    else:
        return
    await edit_message(chat_id, message_id, text, markup)


# ---------- وضعیت مسدود کردن ربات توسط کاربر ----------

async def handle_my_chat_member(ev: dict):
    chat = ev.get("chat") or {}
    if chat.get("type") != "private" or not db.ENABLED:
        return
    status = (ev.get("new_chat_member") or {}).get("status")
    uid = chat.get("id")
    if not uid:
        return
    try:
        if status == "kicked":
            await db.set_blocked(uid, True)
        elif status == "member":
            await db.set_blocked(uid, False)
    except Exception as e:
        log("set_blocked error:", repr(e))


# ---------- پیام‌های کاربران ----------

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


async def send_result(chat_id, info: dict, reply_to=None):
    caption = result_text(info)
    cover = info.get("cover")
    base = {"chat_id": chat_id, "parse_mode": "HTML"}
    if reply_to:
        base["reply_to_message_id"] = reply_to
        base["allow_sending_without_reply"] = True

    for markup in result_markups(info):
        if cover:  # اول با عکس کاور
            data = await tg("sendPhoto", {**base, "photo": cover, "caption": caption, "reply_markup": markup})
            if data.get("ok"):
                return
        data = await tg(
            "sendMessage",
            {**base, "text": caption, "reply_markup": markup, "disable_web_page_preview": True},
        )
        if data.get("ok"):
            return


async def handle_message(msg: dict, process, add_links):
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    user = msg.get("from") or {}
    uid = user.get("id")
    chat_id = chat["id"]
    mid = msg.get("message_id")

    # ثبت کاربر در پایگاه داده (و خواندن وضعیت مسدودیت)
    row = None
    if db.ENABLED and uid:
        try:
            row = await db.upsert_user(user)
        except Exception as e:
            log("upsert_user error:", repr(e))
    admin = await is_admin(uid)

    if row and row.get("is_banned") and not admin:
        await send_message(chat_id, "⛔ دسترسی شما به این ربات مسدود شده است.")
        return

    text = (msg.get("text") or "").strip()
    if text.startswith("/start") or text.startswith("/help"):
        await send_message(chat_id, WELCOME, markup=SETTINGS_BUTTON if admin else None)
        return
    if admin and (text.startswith("/settings") or text == "تنظیمات"):
        await send_message(chat_id, MENU_TEXT, markup=menu_markup())
        return

    picked = pick_file(msg)
    if not picked:
        await send_message(chat_id, "لطفاً یک پیام صوتی، فایل آهنگ یا ویدیو بفرستید. راهنما: /help", mid)
        return

    file_id, size, suffix = picked
    if size and size > TG_MAX:
        await send_message(chat_id, "این فایل بزرگ‌تر از ۲۰ مگابایت است. فایل کوچک‌تری بفرستید.", mid)
        return

    start = parse_start(msg.get("caption"))
    await send_message(chat_id, "در حال شناسایی… (اگر سرور خواب بوده، تا یک دقیقه طول می‌کشد)", mid)

    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(f"{TG_API}/getFile", params={"file_id": file_id})
            data = r.json()
            if not data.get("ok"):
                log("getFile failed:", r.text[:300])
                await send_message(chat_id, "فایل دریافت نشد. شاید بیش از ۲۰ مگابایت است.", mid)
                return
            fr = await c.get(f"{TG_FILE}/{data['result']['file_path']}")
            raw = fr.content
    except Exception as e:
        log("download error:", repr(e))
        await send_message(chat_id, "دریافت فایل از تلگرام شکست خورد. دوباره امتحان کنید.", mid)
        return

    log(f"file {len(raw)} bytes, suffix={suffix}, start={start}")
    try:
        info = await asyncio.wait_for(process(raw, suffix, start), timeout=TOTAL_TIMEOUT)
    except ValueError:
        await send_message(chat_id, "این فایل صدا ندارد یا خوانده نشد. فایل دیگری امتحان کنید.", mid)
        return
    except asyncio.TimeoutError:
        await send_message(chat_id, "شناسایی بیش از حد طول کشید. کمی بعد دوباره امتحان کنید.", mid)
        return

    if not info:
        await send_message(
            chat_id,
            "آهنگ پیدا نشد. از بخش دیگری از آهنگ (با نوشتن ثانیه‌ی شروع در توضیح فایل) امتحان کنید.",
            mid,
        )
        return

    await send_result(chat_id, add_links(info), mid)
    if db.ENABLED and uid:
        try:
            await db.log_recognition(uid, info.get("title"), info.get("artist"), info.get("source"))
        except Exception as e:
            log("log_recognition error:", repr(e))


# ---------- نقطه‌ی ورود ----------

async def handle_update(update: dict, process, add_links):
    update_id = update.get("update_id")
    if db.ENABLED and update_id is not None and not await db.mark_update(update_id):
        return  # قبلاً پردازش شده
    try:
        if "my_chat_member" in update:
            await handle_my_chat_member(update["my_chat_member"])
        elif "callback_query" in update:
            await handle_callback(update["callback_query"])
        elif "message" in update:
            await handle_message(update["message"], process, add_links)
    except Exception as e:
        log("handle_update error:", repr(e))
        traceback.print_exc()
