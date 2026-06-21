import logging
import os
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]


def get_connection():
    return psycopg2.connect(DATABASE_URL)


@contextmanager
def get_cursor():
    conn = get_connection()
    cur = None
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if cur is not None:
            cur.close()
        conn.close()


def init_db():
    with get_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id        BIGINT PRIMARY KEY,
                username       TEXT,
                first_name     TEXT,
                last_name      TEXT,
                is_premium     BOOLEAN DEFAULT FALSE,
                premium_expiry TIMESTAMP,
                premium_plan   TEXT,
                downloads_used INTEGER DEFAULT 0,
                free_limit     INTEGER DEFAULT 10,
                joined_at      TIMESTAMP DEFAULT NOW(),
                last_active    TIMESTAMP DEFAULT NOW(),
                is_banned      BOOLEAN DEFAULT FALSE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id            SERIAL PRIMARY KEY,
                user_id       BIGINT REFERENCES users(user_id),
                url           TEXT NOT NULL,
                platform      TEXT,
                file_size     BIGINT,
                status        TEXT DEFAULT 'success',
                downloaded_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS premium_keys (
                key        TEXT PRIMARY KEY,
                plan       TEXT NOT NULL,
                days       INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                used_by    BIGINT,
                used_at    TIMESTAMP,
                is_active  BOOLEAN DEFAULT TRUE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS payment_requests (
                id             SERIAL PRIMARY KEY,
                user_id        BIGINT REFERENCES users(user_id),
                plan           TEXT NOT NULL,
                days           INTEGER NOT NULL,
                amount         NUMERIC(10,2),
                payment_method TEXT,
                status         TEXT DEFAULT 'pending',
                created_at     TIMESTAMP DEFAULT NOW(),
                processed_at   TIMESTAMP
            )
        """)
    logger.info("Database initialised.")


def get_or_create_user(user_id: int, username: str = None, first_name: str = None, last_name: str = None):
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO users (user_id, username, first_name, last_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                username   = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name  = EXCLUDED.last_name,
                last_active = NOW()
            RETURNING *
        """, (user_id, username, first_name, last_name))
        return dict(cur.fetchone())


def get_user(user_id: int) -> dict | None:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def is_premium(user_id: int) -> bool:
    user = get_user(user_id)
    if not user or not user["is_premium"] or not user["premium_expiry"]:
        return False
    if user["premium_expiry"] > datetime.now():
        return True
    revoke_premium(user_id)
    return False


def try_consume_download(user_id: int) -> tuple[bool, str]:
    """
    Atomically check quota and consume one download slot.
    For free users: uses a compare-and-increment to prevent race conditions.
    Returns (allowed, reason).
    """
    with get_cursor() as cur:
        cur.execute(
            "SELECT is_banned, is_premium, premium_expiry, downloads_used, free_limit "
            "FROM users WHERE user_id = %s",
            (user_id,),
        )
        user = cur.fetchone()
        if not user:
            return False, "User not found."
        if user["is_banned"]:
            return False, "banned"

        # Premium check
        if user["is_premium"] and user["premium_expiry"] and user["premium_expiry"] > datetime.now():
            cur.execute("UPDATE users SET last_active=NOW() WHERE user_id=%s", (user_id,))
            return True, "premium"

        # Free: atomically increment only if still under limit
        cur.execute("""
            UPDATE users
            SET downloads_used = downloads_used + 1, last_active = NOW()
            WHERE user_id = %s AND downloads_used < free_limit
            RETURNING downloads_used, free_limit
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            return False, f"free_limit_reached:{user['downloads_used']}:{user['free_limit']}"
        return True, "free"


def record_download(user_id: int, url: str, platform: str, file_size: int = None, status: str = "success"):
    """Log the download to the downloads table. Counter is already incremented by try_consume_download."""
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO downloads (user_id, url, platform, file_size, status) VALUES (%s, %s, %s, %s, %s)",
            (user_id, url, platform, file_size, status),
        )
        cur.execute("UPDATE users SET last_active = NOW() WHERE user_id = %s", (user_id,))


def add_premium(user_id: int, days: int, plan: str) -> datetime:
    expiry = datetime.now() + timedelta(days=days)
    with get_cursor() as cur:
        cur.execute(
            "UPDATE users SET is_premium=TRUE, premium_expiry=%s, premium_plan=%s WHERE user_id=%s",
            (expiry, plan, user_id),
        )
    return expiry


def revoke_premium(user_id: int):
    with get_cursor() as cur:
        cur.execute(
            "UPDATE users SET is_premium=FALSE, premium_expiry=NULL, premium_plan=NULL WHERE user_id=%s",
            (user_id,),
        )


def ban_user(user_id: int):
    with get_cursor() as cur:
        cur.execute("UPDATE users SET is_banned=TRUE WHERE user_id=%s", (user_id,))


def unban_user(user_id: int):
    with get_cursor() as cur:
        cur.execute("UPDATE users SET is_banned=FALSE WHERE user_id=%s", (user_id,))


def get_all_users() -> list[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users ORDER BY joined_at DESC")
        return [dict(r) for r in cur.fetchall()]


def get_stats() -> dict:
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) as n FROM users")
        total = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) as n FROM users WHERE is_premium=TRUE AND premium_expiry > NOW()")
        premium = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) as n FROM downloads")
        total_dl = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) as n FROM downloads WHERE downloaded_at > NOW() - INTERVAL '24 hours'")
        today = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) as n FROM users WHERE is_banned=TRUE")
        banned = cur.fetchone()["n"]
    return {
        "total_users":     total,
        "premium_users":   premium,
        "free_users":      total - premium,
        "total_downloads": total_dl,
        "downloads_today": today,
        "banned_users":    banned,
    }


def add_payment_request(user_id: int, plan: str, days: int, amount: float, method: str) -> int:
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO payment_requests (user_id, plan, days, amount, payment_method) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (user_id, plan, days, amount, method),
        )
        return cur.fetchone()["id"]


def get_all_downloads() -> list[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM downloads ORDER BY downloaded_at DESC")
        return [dict(r) for r in cur.fetchall()]
