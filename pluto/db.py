"""SQLite storage for Pluto. All timestamps are stored as UTC strings."""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv("PLUTO_DB", "pluto.db")
FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def from_str(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,            -- idea | promise | task | note
                title TEXT NOT NULL,
                original_text TEXT NOT NULL,   -- never overwritten
                status TEXT NOT NULL DEFAULT 'open',  -- open | done | archived | deleted
                due_at TEXT,
                created_at TEXT NOT NULL,
                last_nudged_at TEXT,
                nudge_count INTEGER NOT NULL DEFAULT 0,
                snoozed_until TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS nudges(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL
            )"""
        )


def add_item(user_id, kind, title, original_text, due_at=None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO items(user_id, kind, title, original_text, due_at, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (user_id, kind, title, original_text, due_at, to_str(now_utc())),
        )
        return cur.lastrowid


def get_item(user_id, item_id):
    with conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE id=? AND user_id=? AND status!='deleted'",
            (item_id, user_id),
        ).fetchone()


def list_open(user_id, limit=30):
    with conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE user_id=? AND status='open'"
            " ORDER BY COALESCE(due_at,'9999') ASC, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def recall_pool(user_id, limit=300):
    """Everything the user has not deleted, newest first, for recall questions."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE user_id=? AND status!='deleted'"
            " ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def set_status(user_id, item_id, status) -> bool:
    with conn() as c:
        cur = c.execute(
            "UPDATE items SET status=? WHERE id=? AND user_id=? AND status!='deleted'",
            (status, item_id, user_id),
        )
        return cur.rowcount > 0


def snooze(user_id, item_id, hours=24) -> bool:
    until = to_str(now_utc() + timedelta(hours=hours))
    with conn() as c:
        cur = c.execute(
            "UPDATE items SET snoozed_until=? WHERE id=? AND user_id=? AND status='open'",
            (until, item_id, user_id),
        )
        return cur.rowcount > 0


def export_all(user_id):
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM items WHERE user_id=? AND status!='deleted' ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- nudges ----------

DUE_GAP_HOURS = 6        # min gap between nudges for an overdue item
DUE_MAX_NUDGES = 5
IDEA_AFTER_DAYS = 3      # an idea is "stale" after this many days
IDEA_GAP_DAYS = 3
IDEA_MAX_NUDGES = 3
DAILY_CAP = 5            # max nudges per user per day


def nudges_last_24h(user_id) -> int:
    since = to_str(now_utc() - timedelta(hours=24))
    with conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM nudges WHERE user_id=? AND sent_at>=?",
            (user_id, since),
        ).fetchone()[0]


def pick_nudges(now=None):
    """Return (item_row, reason) pairs that should be nudged now.
    reason is 'due' or 'stale'. Daily cap is applied per user."""
    now = now or now_utc()
    now_s = to_str(now)
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM items WHERE status='open'"
            " AND (snoozed_until IS NULL OR snoozed_until<=?)",
            (now_s,),
        ).fetchall()

    picked, per_user = [], {}
    for r in rows:
        last = from_str(r["last_nudged_at"]) if r["last_nudged_at"] else None
        reason = None
        if r["due_at"] and r["due_at"] <= now_s:
            if r["nudge_count"] < DUE_MAX_NUDGES and (
                last is None or now - last >= timedelta(hours=DUE_GAP_HOURS)
            ):
                reason = "due"
        elif r["kind"] == "idea" and not r["due_at"]:
            age = now - from_str(r["created_at"])
            if (
                age >= timedelta(days=IDEA_AFTER_DAYS)
                and r["nudge_count"] < IDEA_MAX_NUDGES
                and (last is None or now - last >= timedelta(days=IDEA_GAP_DAYS))
            ):
                reason = "stale"
        if not reason:
            continue
        uid = r["user_id"]
        if uid not in per_user:
            per_user[uid] = nudges_last_24h(uid)
        if per_user[uid] >= DAILY_CAP:
            continue
        per_user[uid] += 1
        picked.append((r, reason))
    return picked


def mark_nudged(item_id, user_id) -> None:
    now_s = to_str(now_utc())
    with conn() as c:
        c.execute(
            "UPDATE items SET last_nudged_at=?, nudge_count=nudge_count+1 WHERE id=?",
            (now_s, item_id),
        )
        c.execute(
            "INSERT INTO nudges(user_id,item_id,sent_at) VALUES (?,?,?)",
            (user_id, item_id, now_s),
        )
