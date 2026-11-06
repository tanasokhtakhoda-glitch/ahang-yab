import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
if URL.endswith("/rest/v1"):  # اگر آدرس با /rest/v1 وارد شده بود، آن را حذف می‌کنیم
    URL = URL[: -len("/rest/v1")].rstrip("/")
KEY = os.getenv("SUPABASE_SECRET_KEY", "").strip()
ENABLED = bool(URL and KEY)

TEHRAN = timezone(timedelta(hours=3, minutes=30))  # ساعت ایران (بدون ساعت تابستانی)
STATE_TTL = timedelta(minutes=30)  # اگر مدیر بیش از این مدت جواب نداد، مرحله لغو می‌شود

_client: Optional[httpx.AsyncClient] = None
_admin_cache = (0.0, set())


def log(*args):
    print("[db]", *args, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_start() -> datetime:
    n = datetime.now(TEHRAN)
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


async def _req(method, table, *, params=None, json=None, prefer=None):
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15)
    headers = {"apikey": KEY, "Content-Type": "application/json"}
    if KEY.startswith("eyJ"):  # کلیدهای قدیمی (JWT) به هدر Authorization هم نیاز دارند
        headers["Authorization"] = f"Bearer {KEY}"
    if prefer:
        headers["Prefer"] = prefer
    r = await _client.request(
        method, f"{URL}/rest/v1/{table}", params=params, json=json, headers=headers
    )
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {table} -> {r.status_code}: {r.text[:300]}")
    return r


async def count(table: str, **filters) -> int:
    params = {"select": "*", "limit": "1"}
    params.update(filters)
    r = await _req("GET", table, params=params, prefer="count=exact")
    cr = r.headers.get("content-range", "")
    try:
        return int(cr.split("/")[-1])
    except ValueError:
        return 0


# ---------- کاربران ----------

async def upsert_user(u: dict) -> Optional[dict]:
    """کاربر را ثبت یا به‌روز می‌کند و ردیف او (شامل is_banned) را برمی‌گرداند."""
    payload = {
        "telegram_id": u["id"],
        "username": u.get("username"),
        "first_name": u.get("first_name"),
        "last_name": u.get("last_name"),
        "last_seen_at": iso(now_utc()),
        "is_blocked": False,
    }
    r = await _req(
        "POST", "users",
        params={"on_conflict": "telegram_id"},
        json=payload,
        prefer="resolution=merge-duplicates,return=representation",
    )
    rows = r.json()
    return rows[0] if rows else None


async def get_user(user_id: int) -> Optional[dict]:
    r = await _req(
        "GET", "users",
        params={
            "telegram_id": f"eq.{user_id}",
            "select": "telegram_id,first_name,last_name,username,is_banned",
            "limit": "1",
        },
    )
    rows = r.json()
    return rows[0] if rows else None


async def set_blocked(user_id: int, blocked: bool):
    await _req(
        "PATCH", "users",
        params={"telegram_id": f"eq.{user_id}"},
        json={"is_blocked": blocked, "blocked_at": iso(now_utc()) if blocked else None},
        prefer="return=minimal",
    )


async def set_banned(user_id: int, banned: bool) -> bool:
    """کاربر را مسدود یا آزاد می‌کند. اگر چنین کاربری ثبت نشده باشد False برمی‌گرداند."""
    r = await _req(
        "PATCH", "users",
        params={"telegram_id": f"eq.{user_id}"},
        json={"is_banned": banned},
        prefer="return=representation",
    )
    return bool(r.json())


async def list_banned(limit: int = 30) -> list:
    r = await _req(
        "GET", "users",
        params={
            "is_banned": "eq.true",
            "select": "telegram_id,first_name,last_name,username",
            "order": "telegram_id",
            "limit": str(limit),
        },
    )
    return r.json()


async def recipient_ids() -> list:
    """شناسه‌ی همه‌ی کاربرانی که ربات را مسدود نکرده‌اند و خودشان مسدود نیستند."""
    ids, offset = [], 0
    while True:
        r = await _req(
            "GET", "users",
            params={
                "select": "telegram_id",
                "is_blocked": "eq.false",
                "is_banned": "eq.false",
                "order": "telegram_id",
                "limit": "1000",
                "offset": str(offset),
            },
        )
        rows = r.json()
        ids += [int(x["telegram_id"]) for x in rows]
        if len(rows) < 1000:
            break
        offset += 1000
    return ids


# ---------- مدیران ----------

def invalidate_admin_cache():
    global _admin_cache
    _admin_cache = (0.0, set())


async def admin_ids() -> set:
    """فهرست مدیران (به‌جز مالک). برای کم کردن درخواست‌ها ۶۰ ثانیه ذخیره می‌شود."""
    global _admin_cache
    ts, ids = _admin_cache
    if time.time() - ts < 60:
        return ids
    try:
        r = await _req("GET", "admins", params={"select": "telegram_id"})
        ids = {int(x["telegram_id"]) for x in r.json()}
        _admin_cache = (time.time(), ids)
    except Exception as e:
        log("admin_ids error:", repr(e))
    return ids


async def list_admins() -> list:
    r = await _req(
        "GET", "admins",
        params={
            "select": "telegram_id,created_at,users(first_name,last_name,username)",
            "order": "created_at",
        },
    )
    return r.json()


async def add_admin(user_id: int, added_by: int):
    await _req(
        "POST", "admins",
        params={"on_conflict": "telegram_id"},
        json={"telegram_id": user_id, "added_by": added_by},
        prefer="resolution=ignore-duplicates,return=minimal",
    )
    invalidate_admin_cache()


async def remove_admin(user_id: int):
    await _req(
        "DELETE", "admins",
        params={"telegram_id": f"eq.{user_id}"},
        prefer="return=minimal",
    )
    invalidate_admin_cache()


# ---------- کانال‌های جوین اجباری ----------

_chan_cache = (0.0, [])


def invalidate_channels_cache():
    global _chan_cache
    _chan_cache = (0.0, [])


async def list_channels(use_cache: bool = True) -> list:
    """کانال‌های اجباری. برای کم کردن درخواست‌ها ۳۰ ثانیه ذخیره می‌شود."""
    global _chan_cache
    ts, rows = _chan_cache
    if use_cache and time.time() - ts < 30:
        return rows
    r = await _req(
        "GET", "channels",
        params={
            "is_active": "eq.true",
            "select": "chat_id,title,username,invite_link",
            "order": "id",
        },
    )
    rows = r.json()
    _chan_cache = (time.time(), rows)
    return rows


async def add_channel(chat_id: int, title: str, username, invite_link, added_by: int):
    await _req(
        "POST", "channels",
        params={"on_conflict": "chat_id"},
        json={
            "chat_id": chat_id,
            "title": title or str(chat_id),
            "username": username,
            "invite_link": invite_link,
            "added_by": added_by,
            "is_active": True,
        },
        prefer="resolution=merge-duplicates,return=minimal",
    )
    invalidate_channels_cache()


async def remove_channel(chat_id: int):
    await _req(
        "DELETE", "channels",
        params={"chat_id": f"eq.{chat_id}"},
        prefer="return=minimal",
    )
    invalidate_channels_cache()


# ---------- وضعیت گفتگو با مدیر (مثلاً منتظر دریافت شناسه) ----------

async def set_state(user_id: int, state: str, data: Optional[dict] = None):
    await _req(
        "POST", "admin_states",
        params={"on_conflict": "telegram_id"},
        json={
            "telegram_id": user_id,
            "state": state,
            "data": data or {},
            "updated_at": iso(now_utc()),
        },
        prefer="resolution=merge-duplicates,return=minimal",
    )


async def get_state(user_id: int) -> Optional[dict]:
    r = await _req(
        "GET", "admin_states",
        params={
            "telegram_id": f"eq.{user_id}",
            "select": "state,data,updated_at",
            "limit": "1",
        },
    )
    rows = r.json()
    if not rows:
        return None
    row = rows[0]
    try:
        ts = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
        if now_utc() - ts > STATE_TTL:
            return None
    except Exception:
        pass
    return row


async def clear_state(user_id: int):
    await _req(
        "DELETE", "admin_states",
        params={"telegram_id": f"eq.{user_id}"},
        prefer="return=minimal",
    )


# ---------- به‌روزرسانی‌های تلگرام (جلوگیری از پردازش تکراری) ----------

async def mark_update(update_id: int) -> bool:
    """اگر این به‌روزرسانی قبلاً دیده نشده باشد True برمی‌گرداند."""
    try:
        r = await _req(
            "POST", "processed_updates",
            params={"on_conflict": "update_id"},
            json={"update_id": update_id},
            prefer="resolution=ignore-duplicates,return=representation",
        )
        return bool(r.json())
    except Exception as e:
        log("mark_update error:", repr(e))
        return True  # اگر پایگاه داده مشکل داشت، پیام را رد نمی‌کنیم


async def cleanup_updates():
    try:
        cutoff = now_utc() - timedelta(days=2)
        await _req(
            "DELETE", "processed_updates",
            params={"processed_at": f"lt.{iso(cutoff)}"},
            prefer="return=minimal",
        )
    except Exception as e:
        log("cleanup_updates error:", repr(e))


# ---------- شناسایی‌ها و آمار ----------

async def log_recognition(user_id, title, artist, source):
    await _req(
        "POST", "recognitions",
        json={
            "telegram_user_id": user_id,
            "title": title,
            "artist": artist,
            "source": source,
        },
        prefer="return=minimal",
    )


async def user_stats() -> dict:
    today = today_start()
    week = today - timedelta(days=6)
    day_ago = now_utc() - timedelta(days=1)
    total, blocked, new_today, new_week, active_day = await asyncio.gather(
        count("users"),
        count("users", is_blocked="eq.true"),
        count("users", started_at=f"gte.{iso(today)}"),
        count("users", started_at=f"gte.{iso(week)}"),
        count("users", last_seen_at=f"gte.{iso(day_ago)}"),
    )
    return {
        "total": total,
        "blocked": blocked,
        "active": max(total - blocked, 0),
        "new_today": new_today,
        "new_week": new_week,
        "active_day": active_day,
    }


async def song_stats() -> dict:
    today = today_start()
    week = today - timedelta(days=6)
    total, today_n, week_n = await asyncio.gather(
        count("recognitions"),
        count("recognitions", created_at=f"gte.{iso(today)}"),
        count("recognitions", created_at=f"gte.{iso(week)}"),
    )
    return {"total": total, "today": today_n, "week": week_n}
