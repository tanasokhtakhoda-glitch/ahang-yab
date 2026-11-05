import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
KEY = os.getenv("SUPABASE_SECRET_KEY", "").strip()
ENABLED = bool(URL and KEY)

TEHRAN = timezone(timedelta(hours=3, minutes=30))  # ساعت ایران (بدون ساعت تابستانی)

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


async def set_blocked(user_id: int, blocked: bool):
    await _req(
        "PATCH", "users",
        params={"telegram_id": f"eq.{user_id}"},
        json={"is_blocked": blocked, "blocked_at": iso(now_utc()) if blocked else None},
        prefer="return=minimal",
    )


# ---------- مدیران ----------

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
