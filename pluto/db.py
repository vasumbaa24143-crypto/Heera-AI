"""SQLite storage for Pluto. All timestamps are stored as UTC strings.

Every function that depends on the current time takes an optional `now`
(an aware UTC datetime) so tests and simulate.py can control the clock.
"""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv("PLUTO_DB", "pluto.db")
FMT = "%Y-%m-%dT%H:%M:%SZ"

STATUSES = ("open", "done", "archived")
PRIORITIES = ("high", "normal", "low")
_PRIORITY_ORDER = "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"

# The bot sets this to be told when an item is added, finished or deleted (see sheets.py).
on_change = None


def _changed() -> None:
    if on_change is not None:
        on_change()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def from_str(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


@contextmanager
def conn():
    """One connection per call: commits on success, rolls back on error, always closes."""
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,            -- idea | promise | task | note
                title TEXT NOT NULL,
                original_text TEXT NOT NULL,   -- never overwritten
                status TEXT NOT NULL DEFAULT 'open',  -- open | done | archived
                due_at TEXT,
                created_at TEXT NOT NULL,
                last_nudged_at TEXT,
                nudge_count INTEGER NOT NULL DEFAULT 0,
                snoozed_until TEXT,
                priority TEXT NOT NULL DEFAULT 'normal'  -- high | normal | low
            )"""
        )
        cols = {r["name"] for r in c.execute("PRAGMA table_info(items)")}
        if "priority" not in cols:  # databases made before priorities existed
            c.execute("ALTER TABLE items ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'")
        c.execute(
            """CREATE TABLE IF NOT EXISTS nudges(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,            -- user | pluto
                text TEXT NOT NULL,            -- never overwritten
                created_at TEXT NOT NULL
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS messages_user ON messages(user_id, id)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS settings(
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS items_user ON items(user_id, status)")
        c.execute("CREATE INDEX IF NOT EXISTS nudges_user ON nudges(user_id, sent_at)")
        # Older prototype builds soft-deleted rows; /delete promises "remove forever".
        c.execute("DELETE FROM nudges WHERE item_id IN (SELECT id FROM items WHERE status='deleted')")
        c.execute("DELETE FROM items WHERE status='deleted'")
        _init_search(c)


# ---------- full-text search (SQLite FTS5) ----------
#
# Two external-content FTS5 indexes point at the real rows in `items` and `messages`,
# so the text isn't stored twice and the original rows stay the source of truth.
# Triggers keep them in step on insert/delete (and on the rare item update), which
# also makes /delete and /forget remove things from search. The `unicode61`
# tokenizer is used without Porter stemming, which is English-only and would mangle
# Hinglish words. If this SQLite has no FTS5, search falls back to LIKE.

HAS_FTS = None  # set by init()

_SEARCH_TABLES = {
    # index table: (content table, indexed columns)
    "items_fts": ("items", ("title", "original_text")),
    "messages_fts": ("messages", ("text",)),
}


def _init_search(c) -> None:
    global HAS_FTS
    try:
        for fts, (table, cols) in _SEARCH_TABLES.items():
            existed = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (fts,)
            ).fetchone()
            col_list = ", ".join(cols)
            c.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5({col_list},"
                f" content='{table}', content_rowid='id',"
                " tokenize='unicode61 remove_diacritics 2')"
            )
            new = ", ".join(f"new.{x}" for x in cols)
            old = ", ".join(f"old.{x}" for x in cols)
            c.execute(
                f"CREATE TRIGGER IF NOT EXISTS {fts}_ai AFTER INSERT ON {table} BEGIN"
                f" INSERT INTO {fts}(rowid, {col_list}) VALUES (new.id, {new}); END"
            )
            c.execute(
                f"CREATE TRIGGER IF NOT EXISTS {fts}_ad AFTER DELETE ON {table} BEGIN"
                f" INSERT INTO {fts}({fts}, rowid, {col_list}) VALUES ('delete', old.id, {old}); END"
            )
            c.execute(
                f"CREATE TRIGGER IF NOT EXISTS {fts}_au AFTER UPDATE OF {col_list} ON {table} BEGIN"
                f" INSERT INTO {fts}({fts}, rowid, {col_list}) VALUES ('delete', old.id, {old});"
                f" INSERT INTO {fts}(rowid, {col_list}) VALUES (new.id, {new}); END"
            )
            if not existed:  # first run on an existing database: index what's already there
                c.execute(f"INSERT INTO {fts}({fts}) VALUES ('rebuild')")
        HAS_FTS = True
    except sqlite3.OperationalError as e:
        if "fts5" not in str(e).lower():
            raise
        HAS_FTS = False  # this SQLite build has no FTS5: search uses LIKE instead


_TOKEN = re.compile(r"[\w\u0900-\u097F]+")  # words, including Devanagari
STOPWORDS = frozenset("""
a an and are as at be but by did do does for from had has have i if in into is it its me my of
on or our so than that the their them then there these they this to us was we were what when
where which who why will with would you your about tell said say discussed remember anything
something please pluto
kya kab kaun kahan kaise kyun tha thi the hai hain ho hoga ka ki ke ko se me mein mai main par pe
wala wali wale aur ya bhi to toh na nahi mujhe mera meri mere hum humne maine tum tumne aap
baare bare baat baatein
""".split())


def search_terms(query: str) -> list:
    """Meaningful words from a question, lower-cased, stop-words removed, de-duplicated."""
    words = [w.lower() for w in _TOKEN.findall(query or "")]
    return list(dict.fromkeys(w for w in words if len(w) > 1 and w not in STOPWORDS))


def _match_query(terms) -> str:
    """Safe FTS5 query: each word quoted (so punctuation can't break the syntax), OR-ed, with
    prefix matching for longer words ("birthday" also finds "birthdays"). BM25 ranks rows that
    match more of the words higher."""
    parts = []
    for t in terms:
        q = '"' + t.replace('"', '""') + '"'
        parts.append(q + "*" if len(t) >= 4 else q)
    return " OR ".join(parts)


def _search(table, fts, cols, user_id, query, k, extra_where="", extra_args=()):
    terms = search_terms(query)
    if not terms or k <= 0:
        return []
    if HAS_FTS:
        try:
            with conn() as c:
                return c.execute(
                    f"SELECT t.* FROM {fts} JOIN {table} t ON t.id = {fts}.rowid"
                    f" WHERE {fts} MATCH ? AND t.user_id=? {extra_where}"
                    f" ORDER BY bm25({fts}), t.id DESC LIMIT ?",
                    (_match_query(terms), user_id, *extra_args, k),
                ).fetchall()
        except sqlite3.OperationalError:
            pass  # unexpected query trouble: fall back to LIKE below
    like = " OR ".join(f"{col} LIKE ?" for _ in terms for col in cols)
    args = [f"%{t}%" for t in terms for _ in cols]
    with conn() as c:
        return c.execute(
            f"SELECT t.* FROM {table} t WHERE t.user_id=? AND ({like}) {extra_where}"
            " ORDER BY t.id DESC LIMIT ?",
            (user_id, *args, *extra_args, k),
        ).fetchall()


def search_items(user_id, query, k=10, exclude_ids=()):
    """The user's saved items that best match the words in `query`."""
    ex = tuple(exclude_ids)
    where = f"AND t.id NOT IN ({','.join('?' * len(ex))})" if ex else ""
    return _search("items", "items_fts", ("title", "original_text"), user_id, query, k, where, ex)


def search_messages(user_id, query, k=10, before_id=None):
    """Past chat messages (both sides) that best match `query`, oldest-first in the result.
    `before_id` limits the search to messages older than that id (i.e. not already in the
    recent conversation)."""
    where, args = ("AND t.id < ?", (before_id,)) if before_id else ("", ())
    rows = _search("messages", "messages_fts", ("text",), user_id, query, k, where, args)
    return sorted(rows, key=lambda r: r["id"])


def existing_item_ids(user_id, ids) -> set:
    ids = tuple(ids)
    if not ids:
        return set()
    with conn() as c:
        rows = c.execute(
            f"SELECT id FROM items WHERE user_id=? AND id IN ({','.join('?' * len(ids))})",
            (user_id, *ids),
        ).fetchall()
    return {r["id"] for r in rows}


def add_item(user_id, kind, title, original_text, due_at=None, now=None, priority="normal") -> int:
    if priority not in PRIORITIES:
        priority = "normal"
    with conn() as c:
        cur = c.execute(
            "INSERT INTO items(user_id, kind, title, original_text, due_at, created_at, priority)"
            " VALUES (?,?,?,?,?,?,?)",
            (user_id, kind, title, original_text, due_at, to_str(now or now_utc()), priority),
        )
        item_id = cur.lastrowid
    _changed()
    return item_id


def get_item(user_id, item_id):
    with conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE id=? AND user_id=?", (item_id, user_id)
        ).fetchone()


def list_open(user_id, limit=30):
    with conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE user_id=? AND status='open'"
            f" ORDER BY COALESCE(due_at,'9999') ASC, {_PRIORITY_ORDER}, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def recall_pool(user_id, limit=300):
    """Everything the user has saved (open, done or archived), newest first."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM items WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def context_items(user_id, open_limit=150, closed_limit=20):
    """Items the assistant always sees: open items (the most urgent `open_limit`) plus the
    most recently closed ones. Older items reach it through search_items()."""
    with conn() as c:
        opened = c.execute(
            "SELECT * FROM items WHERE user_id=? AND status='open'"
            f" ORDER BY COALESCE(due_at,'9999') ASC, {_PRIORITY_ORDER}, id DESC LIMIT ?",
            (user_id, open_limit),
        ).fetchall()
        closed = c.execute(
            "SELECT * FROM items WHERE user_id=? AND status!='open' ORDER BY id DESC LIMIT ?",
            (user_id, closed_limit),
        ).fetchall()
    return sorted(opened + closed, key=lambda r: r["id"])


# ---------- settings ----------

def get_setting(user_id, key, default=None):
    with conn() as c:
        row = c.execute(
            "SELECT value FROM settings WHERE user_id=? AND key=?", (user_id, key)
        ).fetchone()
    return row["value"] if row else default


def set_setting(user_id, key, value) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO settings(user_id, key, value) VALUES (?,?,?)"
            " ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
            (user_id, key, value),
        )


# ---------- conversation log ----------

def add_message(user_id, role, text, now=None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO messages(user_id, role, text, created_at) VALUES (?,?,?,?)",
            (user_id, role, text, to_str(now or now_utc())),
        )
        return cur.lastrowid


def recent_messages(user_id, limit=100):
    """The last `limit` messages, oldest first."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return rows[::-1]


def forget_messages(user_id) -> int:
    """Clear the conversation log (saved items are kept)."""
    with conn() as c:
        return c.execute("DELETE FROM messages WHERE user_id=?", (user_id,)).rowcount


def export_messages(user_id):
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE user_id=? ORDER BY id", (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def set_status(user_id, item_id, status) -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with conn() as c:
        cur = c.execute(
            "UPDATE items SET status=? WHERE id=? AND user_id=?",
            (status, item_id, user_id),
        )
        changed = cur.rowcount > 0
    if changed:
        _changed()
    return changed


def delete_item(user_id, item_id) -> bool:
    """Remove an item, its nudge history, and the chat messages it was made from, for good."""
    with conn() as c:
        row = c.execute(
            "SELECT original_text FROM items WHERE id=? AND user_id=?", (item_id, user_id)
        ).fetchone()
        if row is None:
            return False
        c.execute("DELETE FROM items WHERE id=? AND user_id=?", (item_id, user_id))
        c.execute("DELETE FROM nudges WHERE item_id=? AND user_id=?", (item_id, user_id))
        for line in row["original_text"].split("\n"):
            c.execute(
                "DELETE FROM messages WHERE user_id=? AND role='user' AND text=?",
                (user_id, line),
            )
    _changed()
    return True


def snooze(user_id, item_id, hours=24, now=None) -> bool:
    until = to_str((now or now_utc()) + timedelta(hours=hours))
    with conn() as c:
        cur = c.execute(
            "UPDATE items SET snoozed_until=? WHERE id=? AND user_id=? AND status='open'",
            (until, item_id, user_id),
        )
        return cur.rowcount > 0


def export_all(user_id):
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM items WHERE user_id=? ORDER BY id", (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- nudges ----------

DUE_GAP_HOURS = 6        # min gap between nudges for an overdue item
DUE_MAX_NUDGES = 5
IDEA_AFTER_DAYS = 3      # an idea is "stale" after this many days
IDEA_GAP_DAYS = 3
IDEA_MAX_NUDGES = 3
DAILY_CAP = 5            # max nudges per user per rolling 24 hours


def nudges_last_24h(user_id, now=None) -> int:
    since = to_str((now or now_utc()) - timedelta(hours=24))
    with conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM nudges WHERE user_id=? AND sent_at>?",
            (user_id, since),
        ).fetchone()[0]


def pick_nudges(now=None, quiet=False, idea_nudges=lambda uid: True):
    """Return (item_row, reason) pairs that should be nudged now.

    reason is:
      'reminder' - the first alert for something due: the user asked for this time,
                   so it always goes out, ignoring the daily cap and quiet hours;
      'due'      - a repeat nudge for an item still open after its due time;
      'stale'    - an idea left untouched for a while.
    Repeats and stale ideas respect the daily cap (per user) and are held back
    while `quiet` (quiet hours) is true. `idea_nudges(uid)` says whether that user
    gets separate stale-idea nudges. Overdue items go before stale ideas;
    within each, items nudged the fewest times go first, then the oldest."""
    now = now or now_utc()
    now_s = to_str(now)
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM items WHERE status='open'"
            " AND (snoozed_until IS NULL OR snoozed_until<=?)",
            (now_s,),
        ).fetchall()

    reminders, candidates = [], []
    for r in rows:
        last = from_str(r["last_nudged_at"]) if r["last_nudged_at"] else None
        if r["due_at"]:
            if r["due_at"] > now_s:
                continue
            if r["nudge_count"] == 0:
                reminders.append((r, "reminder"))
            elif (
                r["nudge_count"] < DUE_MAX_NUDGES
                and (last is None or now - last >= timedelta(hours=DUE_GAP_HOURS))
            ):
                candidates.append((0, r["nudge_count"], r["due_at"], r["id"], r, "due"))
        elif r["kind"] == "idea" and idea_nudges(r["user_id"]):
            age = now - from_str(r["created_at"])
            if (
                age >= timedelta(days=IDEA_AFTER_DAYS)
                and r["nudge_count"] < IDEA_MAX_NUDGES
                and (last is None or now - last >= timedelta(days=IDEA_GAP_DAYS))
            ):
                candidates.append((1, r["nudge_count"], r["created_at"], r["id"], r, "stale"))
    reminders.sort(key=lambda t: (t[0]["due_at"], t[0]["id"]))
    if quiet:
        return reminders
    candidates.sort(key=lambda t: t[:4])

    picked, per_user = list(reminders), {}
    for r, _ in reminders:
        per_user[r["user_id"]] = per_user.get(r["user_id"], nudges_last_24h(r["user_id"], now)) + 1
    for *_, r, reason in candidates:
        uid = r["user_id"]
        if uid not in per_user:
            per_user[uid] = nudges_last_24h(uid, now)
        if per_user[uid] >= DAILY_CAP:
            continue
        per_user[uid] += 1
        picked.append((r, reason))
    return picked


# ---------- daily digest ----------

def count_items(user_id) -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM items WHERE user_id=?", (user_id,)).fetchone()[0]


def digest_rows(user_id, now, end_of_day, idea_older_than):
    """Rows for the morning digest: (overdue, due_today, one_old_idea_or_None).

    Snoozed, done and archived items are left out. `end_of_day` is local midnight
    (as UTC). The idea is the open idea created before `idea_older_than` that was
    least recently shown, and not shown since `idea_older_than`."""
    now_s, eod_s, old_s = to_str(now), to_str(end_of_day), to_str(idea_older_than)
    awake = "status='open' AND (snoozed_until IS NULL OR snoozed_until<=?)"
    with conn() as c:
        overdue = c.execute(
            f"SELECT * FROM items WHERE user_id=? AND {awake} AND due_at IS NOT NULL AND due_at<?"
            " ORDER BY due_at, id",
            (user_id, now_s, now_s),
        ).fetchall()
        today = c.execute(
            f"SELECT * FROM items WHERE user_id=? AND {awake} AND due_at>=? AND due_at<?"
            " ORDER BY due_at, id",
            (user_id, now_s, now_s, eod_s),
        ).fetchall()
        idea = c.execute(
            f"SELECT * FROM items WHERE user_id=? AND {awake} AND kind='idea' AND due_at IS NULL"
            " AND created_at<=? AND (last_nudged_at IS NULL OR last_nudged_at<=?)"
            " ORDER BY COALESCE(last_nudged_at, created_at), id LIMIT 1",
            (user_id, now_s, old_s, old_s),
        ).fetchone()
    return overdue, today, idea


def touch_nudged(item_id, user_id, now=None) -> None:
    """Record that an item was shown (e.g. in the digest) without using the daily nudge cap."""
    with conn() as c:
        c.execute(
            "UPDATE items SET last_nudged_at=?, nudge_count=nudge_count+1 WHERE id=? AND user_id=?",
            (to_str(now or now_utc()), item_id, user_id),
        )


def mark_nudged(item_id, user_id, now=None) -> None:
    now_s = to_str(now or now_utc())
    with conn() as c:
        c.execute(
            "UPDATE items SET last_nudged_at=?, nudge_count=nudge_count+1 WHERE id=? AND user_id=?",
            (now_s, item_id, user_id),
        )
        c.execute(
            "INSERT INTO nudges(user_id,item_id,sent_at) VALUES (?,?,?)",
            (user_id, item_id, now_s),
        )
