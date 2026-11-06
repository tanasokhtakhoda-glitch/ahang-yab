import asyncio
import html
import os
import re
import time
import traceback
from pathlib import Path
from typing import Optional

import httpx

import db

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
try:
    OWNER_ID = int(os.getenv("OWNER_ID", "").strip())
except ValueError:
    OWNER_ID = 0

try:
    BOT_ID = int(BOT_TOKEN.split(":")[0])
except ValueError:
    BOT_ID = 0

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
TG_MAX = 20 * 1024 * 1024  # محدودیت تلگرام برای دانلود فایل توسط ربات
TOTAL_TIMEOUT = 75  # حداکثر زمان یک شناسایی
BROADCAST_DELAY = 0.05  # فاصله بین پیام‌های همگانی (حدود ۲۰ پیام در ثانیه)

_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_bc_running = False
_joined_cache = {}  # (کاربر، کانال) -> زمان انقضای نتیجه‌ی «عضو است»
_alerted = {}  # کانال -> آخرین زمان هشدار به مالک
JOIN_CACHE_SECONDS = 60
ALERT_EVERY_SECONDS = 6 * 3600


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


# ---------- ابزارهای کمکی ----------

def parse_uid(text) -> Optional[int]:
    t = (text or "").strip().translate(_EN)
    if t.isdigit() and len(t) <= 15:
        return int(t)
    return None


def parse_int(text) -> Optional[int]:
    """عدد صحیح (حتی منفی، مثل شناسه‌ی کانال) را برمی‌گرداند."""
    t = (text or "").strip().translate(_EN)
    if re.fullmatch(r"-?\d{1,20}", t):
        return int(t)
    return None


def full_name(row) -> str:
    row = row or {}
    return " ".join(x for x in [row.get("first_name"), row.get("last_name")] if x).strip()


def display_name(row) -> str:
    row = row or {}
    parts = []
    name = full_name(row)
    if name:
        parts.append(html.escape(name))
    if row.get("username"):
        parts.append("@" + html.escape(row["username"]))
    return " ".join(parts) or "بدون نام"


async def is_admin(user_id) -> bool:
    if not user_id:
        return False
    if OWNER_ID and user_id == OWNER_ID:
        return True
    if not db.ENABLED:
        return False
    return user_id in await db.admin_ids()


# ---------- منوها ----------

MENU_TEXT = "⚙️ <b>تنظیمات مدیریت</b>\n\nیک گزینه را انتخاب کنید:"
DB_OFF_TEXT = (
    "⚠️ اتصال به پایگاه داده برقرار نیست.\n"
    "متغیرهای SUPABASE_URL و SUPABASE_SECRET_KEY را در رندر بررسی کنید."
)
DB_FAIL_TEXT = "⚠️ ارتباط با پایگاه داده شکست خورد. لاگ رندر را ببینید."
NOT_STARTED_TEXT = "⚠️ این شناسه ربات را استارت نکرده است."
SETTINGS_BUTTON = {"inline_keyboard": [[{"text": "⚙️ تنظیمات", "callback_data": "adm:menu"}]]}


def btn(text, data) -> dict:
    return {"text": text, "callback_data": data}


def back_markup(target="adm:menu", label="🔙 بازگشت") -> dict:
    return {"inline_keyboard": [[btn(label, target)]]}


def menu_markup(is_owner: bool) -> dict:
    rows = [
        [btn("📊 آمار کاربران", "adm:users"), btn("🎵 آمار شناسایی", "adm:songs")],
        [btn("📢 پیام همگانی", "adm:bc")],
        [btn("🚫 مسدودسازی کاربران", "adm:bans")],
        [btn("🔒 جوین اجباری", "adm:jc")],
    ]
    if is_owner:
        rows.append([btn("👮 مدیریت مدیران", "adm:admins")])
    return {"inline_keyboard": rows}


BANS_TEXT = (
    "🚫 <b>مسدودسازی کاربران</b>\n\n"
    "کاربر مسدودشده نمی‌تواند از ربات استفاده کند.\n"
    "برای مسدود یا آزاد کردن، شناسه‌ی عددی تلگرام او را می‌فرستید."
)


def bans_markup() -> dict:
    return {
        "inline_keyboard": [
            [btn("🚫 مسدود کردن", "adm:ban"), btn("✅ آزاد کردن", "adm:unban")],
            [btn("📋 فهرست مسدودها", "adm:banlist")],
            [btn("🔙 بازگشت", "adm:menu")],
        ]
    }


# ---------- آمار ----------

async def users_stats_text() -> str:
    if not db.ENABLED:
        return DB_OFF_TEXT
    try:
        s = await db.user_stats()
    except Exception as e:
        log("user_stats error:", repr(e))
        return DB_FAIL_TEXT
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
        return DB_FAIL_TEXT
    return (
        "🎵 <b>آمار شناسایی آهنگ</b>\n\n"
        f"🔢 کل شناسایی‌های موفق: <b>{fa(s['total'])}</b>\n"
        f"📅 امروز: <b>{fa(s['today'])}</b>\n"
        f"🗓 ۷ روز اخیر: <b>{fa(s['week'])}</b>"
    )


# ---------- نمایش فهرست مدیران و مسدودها ----------

async def admins_view():
    rows = await db.list_admins()
    lines = ["👮 <b>مدیران ربات</b>", "", f"👑 مالک: <code>{OWNER_ID}</code>", ""]
    buttons = []
    if not rows:
        lines.append("هنوز مدیر دیگری اضافه نشده است.")
    for i, row in enumerate(rows, 1):
        u = row.get("users") or {}
        lines.append(f"{fa(i)}. {display_name(u)} — <code>{row['telegram_id']}</code>")
        short = (full_name(u) or str(row["telegram_id"]))[:18]
        buttons.append([btn(f"🗑 حذف {short}", f"adm:rm:{row['telegram_id']}")])
    buttons.append([btn("➕ افزودن مدیر", "adm:addadm")])
    buttons.append([btn("🔙 بازگشت", "adm:menu")])
    return "\n".join(lines), {"inline_keyboard": buttons}


async def banlist_view():
    rows = await db.list_banned()
    if not rows:
        return "📋 هیچ کاربری مسدود نیست.", back_markup("adm:bans")
    lines = ["📋 <b>کاربران مسدود</b>", ""]
    for i, row in enumerate(rows, 1):
        lines.append(f"{fa(i)}. {display_name(row)} — <code>{row['telegram_id']}</code>")
    return "\n".join(lines), back_markup("adm:bans")


# ---------- پرسیدن ورودی از مدیر ----------

PROMPTS = {
    "addadm": (
        "➕ <b>افزودن مدیر</b>\n\n"
        "شناسه‌ی عددی تلگرام شخص را بفرستید. او باید قبلاً ربات را استارت کرده باشد.\n"
        "برای لغو /cancel را بزنید.",
        "adm:admins",
    ),
    "ban": (
        "🚫 <b>مسدود کردن کاربر</b>\n\n"
        "شناسه‌ی عددی تلگرام کاربر را بفرستید.\nبرای لغو /cancel را بزنید.",
        "adm:bans",
    ),
    "unban": (
        "✅ <b>آزاد کردن کاربر</b>\n\n"
        "شناسه‌ی عددی تلگرام کاربر را بفرستید.\nبرای لغو /cancel را بزنید.",
        "adm:bans",
    ),
    "jcadd": (
        "🔒 <b>افزودن کانال اجباری</b>\n\n"
        "ابتدا ربات را در کانال «مدیر» کنید (برای کانال خصوصی، اجازه‌ی دعوت از طریق لینک هم بدهید).\n\n"
        "بعد یکی از این دو کار را بکنید:\n"
        "۱. یک پیام از کانال را برای ربات فوروارد کنید.\n"
        "۲. اگر فوروارد ممکن نیست، نام کاربری کانال (مثل @mychannel) "
        "یا شناسه‌ی عددی آن (مثل -1001234567890) را بفرستید.\n\n"
        "برای لغو /cancel را بزنید.",
        "adm:jc",
    ),
    "bc": (
        "📢 <b>پیام همگانی</b>\n\n"
        "پیامی را که می‌خواهید برای همه‌ی کاربران ارسال شود بفرستید.\n"
        "متن، عکس، ویدیو، فایل یا پیام فورواردشده، همه پذیرفته می‌شود.\n"
        "قبل از ارسال، پیش‌نمایش و دکمه‌ی تأیید نشان داده می‌شود.\n"
        "برای لغو /cancel را بزنید.",
        "adm:menu",
    ),
}


async def ask(uid, chat_id, message_id, state):
    text, cancel_target = PROMPTS[state]
    await db.set_state(uid, state)
    await edit_message(chat_id, message_id, text, back_markup(cancel_target, "❌ انصراف"))


# ---------- پیام همگانی ----------

CONFIRM_MARKUP = {
    "inline_keyboard": [
        [btn("✅ تأیید ارسال", "adm:bcyes")],
        [btn("❌ انصراف", "adm:menu")],
    ]
}


async def prepare_broadcast(msg: dict, uid, chat_id):
    mid = msg["message_id"]
    try:
        n = await db.count("users", is_blocked="eq.false", is_banned="eq.false")
    except Exception as e:
        log("count recipients error:", repr(e))
        await send_message(chat_id, DB_FAIL_TEXT)
        return
    if n == 0:
        await db.clear_state(uid)
        await send_message(chat_id, "کاربری برای ارسال وجود ندارد.", markup=SETTINGS_BUTTON)
        return
    # پیش‌نمایش: همان پیام برای خود مدیر کپی می‌شود
    await tg("copyMessage", {"chat_id": chat_id, "from_chat_id": chat_id, "message_id": mid})
    await db.set_state(uid, "bc_confirm", {"chat_id": chat_id, "message_id": mid, "count": n})
    await send_message(
        chat_id,
        f"👆 پیش‌نمایش بالا برای <b>{fa(n)}</b> کاربر ارسال می‌شود.\nتأیید می‌کنید؟",
        markup=CONFIRM_MARKUP,
    )


async def copy_one(client, to_chat, from_chat, message_id) -> str:
    """نتیجه: ok / blocked / error"""
    for _ in range(3):
        try:
            r = await client.post(
                f"{TG_API}/copyMessage",
                json={"chat_id": to_chat, "from_chat_id": from_chat, "message_id": message_id},
            )
            data = r.json()
        except Exception as e:
            log("copyMessage error:", repr(e))
            return "error"
        if data.get("ok"):
            return "ok"
        code = data.get("error_code")
        if code == 429:  # تلگرام سرعت را محدود کرده؛ کمی صبر و دوباره تلاش
            wait = (data.get("parameters") or {}).get("retry_after", 3)
            await asyncio.sleep(min(wait + 1, 30))
            continue
        if code == 403:  # کاربر ربات را مسدود کرده
            return "blocked"
        return "error"
    return "error"


async def run_broadcast(admin_chat, from_chat, message_id):
    global _bc_running
    _bc_running = True
    sent = blocked = failed = 0
    total = 0
    try:
        ids = await db.recipient_ids()
        total = len(ids)
        async with httpx.AsyncClient(timeout=30) as client:
            for uid in ids:
                res = await copy_one(client, uid, from_chat, message_id)
                if res == "ok":
                    sent += 1
                elif res == "blocked":
                    blocked += 1
                    try:
                        await db.set_blocked(uid, True)
                    except Exception as e:
                        log("set_blocked error:", repr(e))
                else:
                    failed += 1
                await asyncio.sleep(BROADCAST_DELAY)
    except Exception as e:
        log("broadcast error:", repr(e))
        traceback.print_exc()
    finally:
        _bc_running = False
    await send_message(
        admin_chat,
        "📢 <b>گزارش پیام همگانی</b>\n\n"
        f"👥 کل گیرندگان: <b>{fa(total)}</b>\n"
        f"✅ ارسال موفق: <b>{fa(sent)}</b>\n"
        f"🚫 مسدودکننده‌ی ربات: <b>{fa(blocked)}</b>\n"
        f"⚠️ خطا: <b>{fa(failed)}</b>",
        markup=SETTINGS_BUTTON,
    )


async def confirm_broadcast(uid, chat_id, message_id):
    st = None
    try:
        st = await db.get_state(uid)
    except Exception as e:
        log("get_state error:", repr(e))
    if not st or st.get("state") != "bc_confirm":
        await edit_message(
            chat_id, message_id,
            "⌛ این درخواست منقضی شده است. دوباره از بخش «پیام همگانی» شروع کنید.",
            back_markup(),
        )
        return
    if _bc_running:
        await edit_message(
            chat_id, message_id,
            "⏳ یک ارسال همگانی در حال انجام است. تا پایان آن صبر کنید.",
            back_markup(),
        )
        return
    data = st.get("data") or {}
    try:
        await db.clear_state(uid)
    except Exception as e:
        log("clear_state error:", repr(e))
    await edit_message(
        chat_id, message_id,
        "📤 ارسال شروع شد. گزارش پایان کار برایتان فرستاده می‌شود.",
    )
    await run_broadcast(chat_id, data.get("chat_id"), data.get("message_id"))


# ---------- ورودی‌های مدیر (پاسخ به سؤال‌های ربات) ----------

async def handle_admin_input(msg: dict, uid, chat_id, st: dict) -> bool:
    """اگر پیام پاسخ یک مرحله بود پردازش می‌کند و True برمی‌گرداند."""
    state = st.get("state")
    text = (msg.get("text") or "").strip()

    if text in ("/cancel", "انصراف"):
        await db.clear_state(uid)
        await send_message(chat_id, "لغو شد.", markup=SETTINGS_BUTTON)
        return True

    if state == "bc":
        await prepare_broadcast(msg, uid, chat_id)
        return True

    if state == "jcadd":
        await handle_channel_input(msg, uid, chat_id)
        return True

    if state in ("addadm", "ban", "unban"):
        target = parse_uid(text)
        if target is None:
            await send_message(chat_id, "⚠️ شناسه نامعتبر است. فقط عدد بفرستید یا /cancel را بزنید.")
            return True
        await db.clear_state(uid)

        row = await db.get_user(target)
        if state == "addadm":
            if OWNER_ID and target == OWNER_ID:
                await send_message(chat_id, "مالک ربات همیشه مدیر است.", markup=back_markup("adm:admins"))
            elif not row:
                await send_message(chat_id, NOT_STARTED_TEXT, markup=back_markup("adm:admins"))
            elif target in await db.admin_ids():
                await send_message(chat_id, "این کاربر از قبل مدیر است.", markup=back_markup("adm:admins"))
            else:
                await db.add_admin(target, uid)
                await send_message(
                    chat_id,
                    f"✅ {display_name(row)} (<code>{target}</code>) به مدیران اضافه شد.",
                    markup=back_markup("adm:admins"),
                )
                await send_message(
                    target,
                    "👮 شما به‌عنوان مدیر ربات اضافه شدید.\nبرای دیدن تنظیمات /settings را بزنید.",
                    markup=SETTINGS_BUTTON,
                )
            return True

        # مسدود / آزاد کردن
        if state == "ban" and (target == OWNER_ID or target in await db.admin_ids()):
            await send_message(chat_id, "نمی‌توان مالک یا مدیر را مسدود کرد.", markup=back_markup("adm:bans"))
        elif not row:
            await send_message(chat_id, NOT_STARTED_TEXT, markup=back_markup("adm:bans"))
        else:
            await db.set_banned(target, state == "ban")
            verb = "مسدود شد" if state == "ban" else "آزاد شد"
            icon = "🚫" if state == "ban" else "✅"
            await send_message(
                chat_id,
                f"{icon} {display_name(row)} (<code>{target}</code>) {verb}.",
                markup=back_markup("adm:bans"),
            )
        return True

    return False


# ---------- جوین اجباری ----------

JOIN_OK = ("creator", "administrator", "member")


async def bot_is_admin(chat_id) -> bool:
    data = await tg("getChatMember", {"chat_id": chat_id, "user_id": BOT_ID})
    return bool(data.get("ok")) and (data.get("result") or {}).get("status") in ("creator", "administrator")


async def alert_owner(chan, reason: str):
    """به مالک خبر می‌دهد بررسی عضویت یک کانال کار نمی‌کند (حداکثر هر ۶ ساعت یک بار)."""
    key = chan["chat_id"]
    if not OWNER_ID or time.time() - _alerted.get(key, 0) < ALERT_EVERY_SECONDS:
        return
    _alerted[key] = time.time()
    await send_message(
        OWNER_ID,
        f"⚠️ بررسی عضویت در کانال «{html.escape(chan.get('title') or '')}» کار نمی‌کند.\n"
        "احتمالاً ربات از کانال حذف شده یا دیگر مدیر نیست.\n"
        "تا درست شدن آن، این کانال برای کاربران نادیده گرفته می‌شود.\n\n"
        f"جزئیات: {html.escape(reason[:150])}",
    )


async def is_member(uid, chan) -> bool:
    key = (uid, chan["chat_id"])
    if _joined_cache.get(key, 0) > time.time():
        return True
    data = await tg("getChatMember", {"chat_id": chan["chat_id"], "user_id": uid})
    if not data.get("ok"):
        await alert_owner(chan, str(data.get("description") or data))
        return True  # اگر بررسی ممکن نبود، کاربر را معطل نمی‌کنیم
    m = data.get("result") or {}
    st = m.get("status")
    ok = st in JOIN_OK or (st == "restricted" and bool(m.get("is_member")))
    if ok:
        if len(_joined_cache) > 5000:
            _joined_cache.clear()
        _joined_cache[key] = time.time() + JOIN_CACHE_SECONDS
    return ok


async def join_status(uid) -> list:
    """[(کانال، عضو است؟)] برای همه‌ی کانال‌های اجباری."""
    chans = await db.list_channels()
    if not chans:
        return []
    flags = await asyncio.gather(*(is_member(uid, c) for c in chans))
    return list(zip(chans, flags))


def join_text(status) -> str:
    lines = ["🔒 <b>برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شوید:</b>", ""]
    for chan, joined in status:
        lines.append(f"{'✅' if joined else '❌'} {html.escape(chan.get('title') or '')}")
    lines += ["", "بعد از عضویت، دکمه‌ی «عضو شدم» را بزنید."]
    return "\n".join(lines)


def join_markup(status) -> dict:
    rows = []
    for chan, joined in status:
        if joined:
            continue
        link = chan.get("invite_link") or (
            f"https://t.me/{chan['username']}" if chan.get("username") else None
        )
        if link:
            rows.append([{"text": f"📢 عضویت در {chan.get('title') or ''}"[:60], "url": link}])
    rows.append([btn("✅ عضو شدم، بررسی کن", "jc:chk")])
    return {"inline_keyboard": rows}


async def enforce_join(uid, chat_id) -> bool:
    """اگر کاربر در همه‌ی کانال‌ها عضو نبود پیام می‌فرستد و True برمی‌گرداند."""
    try:
        status = await join_status(uid)
    except Exception as e:
        log("join check error:", repr(e))
        return False
    if not status or all(j for _, j in status):
        return False
    await send_message(chat_id, join_text(status), markup=join_markup(status))
    return True


async def handle_join_check(cb: dict):
    uid = (cb.get("from") or {}).get("id")
    cb_id = cb.get("id")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    try:
        status = await join_status(uid) if db.ENABLED else []
    except Exception as e:
        log("join check error:", repr(e))
        status = []
    if status and not all(j for _, j in status):
        await tg(
            "answerCallbackQuery",
            {"callback_query_id": cb_id, "text": "هنوز در همه‌ی کانال‌ها عضو نشده‌اید.", "show_alert": True},
        )
        if chat_id and message_id:
            await edit_message(chat_id, message_id, join_text(status), join_markup(status))
        return
    await tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "✅ عضویت شما تأیید شد."})
    if chat_id and message_id:
        await edit_message(chat_id, message_id, "✅ عضویت شما تأیید شد.\n\n" + WELCOME)


# ---------- مدیریت کانال‌های اجباری (برای مدیران) ----------

async def jc_view():
    chans = await db.list_channels(use_cache=False)
    lines = ["🔒 <b>جوین اجباری</b>", ""]
    buttons = []
    if not chans:
        lines.append("هنوز کانالی اضافه نشده است. فعلاً همه می‌توانند بدون عضویت از ربات استفاده کنند.")
    else:
        flags = await asyncio.gather(*(bot_is_admin(c["chat_id"]) for c in chans))
        for i, (c, ok) in enumerate(zip(chans, flags), 1):
            un = f" @{html.escape(c['username'])}" if c.get("username") else ""
            mark = "✅" if ok else "⚠️ ربات مدیر نیست"
            lines.append(f"{fa(i)}. {html.escape(c.get('title') or '')}{un} — {mark}")
            short = (c.get("title") or str(c["chat_id"]))[:18]
            buttons.append([btn(f"🗑 حذف {short}", f"adm:jcrm:{c['chat_id']}")])
        lines += ["", "کاربران عادی باید در همه‌ی این کانال‌ها عضو باشند؛ مدیران معاف‌اند."]
    buttons.append([btn("➕ افزودن کانال", "adm:jcadd")])
    buttons.append([btn("🔙 بازگشت", "adm:menu")])
    return "\n".join(lines), {"inline_keyboard": buttons}


def channel_ref_from_message(msg: dict):
    """شناسه یا نام کاربری کانال را از پیام فورواردی یا متن بیرون می‌کشد."""
    fo = msg.get("forward_origin") or {}
    fc = msg.get("forward_from_chat") or (fo.get("chat") if fo.get("type") == "channel" else None)
    if fc and fc.get("id"):
        return fc["id"]
    text = (msg.get("text") or "").strip()
    m = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})", text)
    if m and m.group(1).lower() != "joinchat":
        return "@" + m.group(1)
    if re.fullmatch(r"@[A-Za-z0-9_]{4,}", text):
        return text
    n = parse_int(text)
    if n is not None and n < 0:
        return n
    return None


async def handle_channel_input(msg: dict, uid, chat_id):
    ref = channel_ref_from_message(msg)
    if ref is None:
        await send_message(
            chat_id,
            "⚠️ کانال شناخته نشد. یک پیام از کانال فوروارد کنید، یا نام کاربری (مثل @mychannel) "
            "یا شناسه‌ی عددی منفی آن را بفرستید.\nبرای لغو /cancel را بزنید.",
        )
        return
    data = await tg("getChat", {"chat_id": ref})
    if not data.get("ok"):
        await send_message(
            chat_id,
            "⚠️ این کانال پیدا نشد یا ربات به آن دسترسی ندارد. اول ربات را در کانال مدیر کنید "
            "و دوباره امتحان کنید.\nبرای لغو /cancel را بزنید.",
        )
        return
    info = data["result"]
    if info.get("type") not in ("channel", "supergroup", "group"):
        await send_message(chat_id, "⚠️ این شناسه مربوط به کانال یا گروه نیست.")
        return
    cid = info["id"]
    title = info.get("title") or str(cid)
    if not await bot_is_admin(cid):
        await send_message(
            chat_id,
            f"⚠️ ربات در «{html.escape(title)}» مدیر نیست. اول آن را مدیر کنید و دوباره بفرستید.\n"
            "برای لغو /cancel را بزنید.",
        )
        return
    username = info.get("username")
    link = f"https://t.me/{username}" if username else info.get("invite_link")
    if not link:
        r = await tg("createChatInviteLink", {"chat_id": cid, "name": "ahang-yab-bot"})
        link = (r.get("result") or {}).get("invite_link") if r.get("ok") else None
    if not link:
        await send_message(
            chat_id,
            "⚠️ لینک دعوت ساخته نشد. در کانال خصوصی به ربات اجازه‌ی «دعوت کاربران از طریق لینک» بدهید "
            "و دوباره بفرستید.\nبرای لغو /cancel را بزنید.",
        )
        return
    await db.add_channel(cid, title, username, link, uid)
    await db.clear_state(uid)
    await send_message(
        chat_id,
        f"✅ کانال «{html.escape(title)}» به جوین اجباری اضافه شد.",
        markup=back_markup("adm:jc"),
    )


# ---------- دکمه‌های منوی مدیریت ----------

OWNER_ONLY = ("admins", "addadm")


async def handle_callback(cb: dict):
    uid = (cb.get("from") or {}).get("id")
    cb_id = cb.get("id")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    data = cb.get("data") or ""

    async def answer(text=None, alert=False):
        payload = {"callback_query_id": cb_id}
        if text:
            payload.update({"text": text, "show_alert": alert})
        await tg("answerCallbackQuery", payload)

    if data == "jc:chk":
        await handle_join_check(cb)
        return
    if not data.startswith("adm:"):
        await answer()
        return
    if not await is_admin(uid):
        await answer("شما دسترسی ندارید.", True)
        return

    action = data[4:]
    is_owner = bool(OWNER_ID and uid == OWNER_ID)
    if (action in OWNER_ONLY or action.startswith(("rm:", "rmy:"))) and not is_owner:
        await answer("فقط مالک ربات به این بخش دسترسی دارد.", True)
        return

    await answer()
    if not chat_id or not message_id:
        return

    if action != "menu" and not db.ENABLED:
        await edit_message(chat_id, message_id, DB_OFF_TEXT, back_markup())
        return

    try:
        if action == "bcyes":
            await confirm_broadcast(uid, chat_id, message_id)
            return

        if db.ENABLED:
            await db.clear_state(uid)  # هر جابه‌جایی در منو، مرحله‌ی نیمه‌کاره را لغو می‌کند

        if action == "menu":
            text, markup = MENU_TEXT, menu_markup(is_owner)
        elif action == "users":
            text, markup = await users_stats_text(), back_markup()
        elif action == "songs":
            text, markup = await songs_stats_text(), back_markup()
        elif action == "bans":
            text, markup = BANS_TEXT, bans_markup()
        elif action == "banlist":
            text, markup = await banlist_view()
        elif action == "jc":
            text, markup = await jc_view()
        elif action.startswith("jcrm:"):
            cid = parse_int(action[5:])
            chans = await db.list_channels(use_cache=False)
            title = next((c.get("title") for c in chans if c["chat_id"] == cid), None) or str(cid)
            text = f"🗑 کانال «{html.escape(title)}» از جوین اجباری حذف شود؟"
            markup = {
                "inline_keyboard": [
                    [btn("✅ بله، حذف شود", f"adm:jcrmy:{cid}")],
                    [btn("❌ خیر", "adm:jc")],
                ]
            }
        elif action.startswith("jcrmy:"):
            cid = parse_int(action[6:])
            if cid is not None:
                await db.remove_channel(cid)
            text, markup = await jc_view()
            text = "✅ کانال حذف شد.\n\n" + text
        elif action in ("ban", "unban", "bc", "addadm", "jcadd"):
            await ask(uid, chat_id, message_id, action)
            return
        elif action == "admins":
            text, markup = await admins_view()
        elif action.startswith("rm:"):
            target = parse_uid(action[3:])
            row = await db.get_user(target) if target else None
            text = f"🗑 مدیر {display_name(row)} (<code>{target}</code>) حذف شود؟"
            markup = {
                "inline_keyboard": [
                    [btn("✅ بله، حذف شود", f"adm:rmy:{target}")],
                    [btn("❌ خیر", "adm:admins")],
                ]
            }
        elif action.startswith("rmy:"):
            target = parse_uid(action[4:])
            if target:
                await db.remove_admin(target)
            text, markup = await admins_view()
            text = "✅ مدیر حذف شد.\n\n" + text
        else:
            return
    except Exception as e:
        log("callback error:", repr(e))
        traceback.print_exc()
        text, markup = DB_FAIL_TEXT, back_markup()
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

    # جوین اجباری: کاربران عادی باید در کانال‌های تعیین‌شده عضو باشند (مدیران معاف‌اند)
    if not admin and db.ENABLED and uid:
        if await enforce_join(uid, chat_id):
            return

    text = (msg.get("text") or "").strip()

    # دستورهای اصلی (مرحله‌ی نیمه‌کاره‌ی مدیر را هم لغو می‌کنند)
    if text.startswith(("/start", "/help", "/settings")) or (admin and text == "تنظیمات"):
        if admin and db.ENABLED:
            try:
                await db.clear_state(uid)
            except Exception as e:
                log("clear_state error:", repr(e))
        if text.startswith("/settings") or text == "تنظیمات":
            if admin:
                await send_message(
                    chat_id, MENU_TEXT,
                    markup=menu_markup(bool(OWNER_ID and uid == OWNER_ID)),
                )
            return
        await send_message(chat_id, WELCOME, markup=SETTINGS_BUTTON if admin else None)
        return

    # اگر مدیر وسط یک مرحله است (مثلاً منتظر شناسه یا پیام همگانی)
    if admin and db.ENABLED:
        try:
            st = await db.get_state(uid)
            if st and await handle_admin_input(msg, uid, chat_id, st):
                return
        except Exception as e:
            log("admin input error:", repr(e))
            traceback.print_exc()
            await send_message(chat_id, DB_FAIL_TEXT)
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
