"""Pluto's conversation flow, independent of Telegram.

bot.py (Telegram) and simulate.py (terminal) are thin adapters around this
module, so both run exactly the same logic. Everything here is synchronous;
the bot runs it in a worker thread.

`llm` is any object with:
    extract(text: str, now_local: datetime) -> dict
    answer(question: str, items: list, now_local: datetime) -> str
(llm.py talks to Gemini; offline.py and the tests provide stand-ins.)
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import db

log = logging.getLogger("pluto")

TZ = ZoneInfo(os.getenv("PLUTO_TZ", "Asia/Kolkata"))
QUIET_START, QUIET_END = 22, 8  # no nudges from 22:00 to 08:00 local
KIND_ICON = {"idea": "💡", "promise": "🤝", "task": "✅", "note": "📝"}
TITLE_MAX = 120
MAX_REPLY = 4000  # Telegram's limit is 4096 characters

START_TEXT = (
    "Pluto here. Just tell me anything — type it or send a voice note.\n\n"
    "I will remember it, and nudge you if it needs follow-up.\n"
    "Ask me later: “what was that YouTube idea?”\n\n"
    "/list  open items\n/done <id>  mark done\n/snooze <id>  +1 day\n"
    "/delete <id>  remove forever\n/export  download everything"
)


@dataclass
class Reply:
    text: str
    buttons: list = field(default_factory=list)  # [(label, callback_data), ...]


# ---------- formatting ----------

def to_local(dt: datetime) -> datetime:
    return dt.astimezone(TZ)


def fmt_due(due_utc) -> str:
    if not due_utc:
        return ""
    return to_local(db.from_str(due_utc)).strftime("%a %d %b, %H:%M")


def item_line(r) -> str:
    due = f" — due {fmt_due(r['due_at'])}" if r["due_at"] else ""
    return f"{KIND_ICON.get(r['kind'], '•')} #{r['id']} {r['title']}{due}"


def item_buttons(item_id: int, with_archive: bool = False) -> list:
    b = [("✅ Done", f"done:{item_id}"), ("😴 +1 day", f"snz:{item_id}")]
    if with_archive:
        b.append(("🗄 Drop", f"arc:{item_id}"))
    return b


def _short(text: str) -> str:
    return text if len(text) <= MAX_REPLY else text[: MAX_REPLY - 1] + "…"


# ---------- capture ----------

def parse_due(due_local, tz=None):
    """'YYYY-MM-DDTHH:MM' in local time -> UTC string, or None if absent/invalid."""
    if not due_local or not isinstance(due_local, str):
        return None
    try:
        dt = datetime.fromisoformat(due_local.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or TZ)
    return db.to_str(dt)


def _normalize(result) -> dict:
    """Check the labeller's output; raise ValueError if it is unusable."""
    if not isinstance(result, dict):
        raise ValueError("labeller did not return an object")
    items = result.get("items") or []
    if not isinstance(items, list):
        raise ValueError("items is not a list")
    return {
        "is_question": result.get("is_question") is True,
        "items": [it for it in items if isinstance(it, dict)],
    }


def save_raw(uid, text, now) -> Reply:
    item_id = db.add_item(uid, "note", text[:80], text, now=now)
    return Reply(f"Saved as a plain note (#{item_id}). I could not label it just now.")


def capture(uid, text, llm, now=None) -> list:
    """Handle a free-text message: save it as item(s), or answer it as a recall question."""
    now = now or db.now_utc()
    text = (text or "").strip()
    if not text:
        return [Reply("Tell me something to remember.")]

    try:
        result = _normalize(llm.extract(text, to_local(now)))
    except Exception:
        # Never lose a thought: if labelling fails, keep it raw as a note.
        log.exception("labelling failed; saving raw note")
        return [save_raw(uid, text, now)]

    if result["is_question"]:
        return [recall(uid, text, llm, now)]

    items = result["items"] or [{"kind": "note", "title": text[:80], "due_local": None}]
    replies = []
    for it in items:
        kind = it.get("kind") if it.get("kind") in KIND_ICON else "note"
        title = it.get("title") if isinstance(it.get("title"), str) else ""
        title = (title.strip() or text[:80])[:TITLE_MAX]
        due_utc = parse_due(it.get("due_local"))
        item_id = db.add_item(uid, kind, title, text, due_utc, now=now)
        row = db.get_item(uid, item_id)
        follow_up = kind in ("task", "promise") or due_utc is not None
        replies.append(
            Reply(f"Saved. {item_line(row)}", item_buttons(item_id) if follow_up else [])
        )
    return replies


def recall(uid, question, llm, now=None) -> Reply:
    now = now or db.now_utc()
    pool = db.recall_pool(uid)
    if not pool:
        # Nothing to search, so there is nothing to ask the model. Never invent a memory.
        return Reply("I don't have anything saved yet, so I can't answer that.")
    try:
        ans = (llm.answer(question, pool, to_local(now)) or "").strip()
    except Exception:
        log.exception("recall failed")
        ans = ""
    if not ans:
        return Reply("I could not search just now. Your items are safe — try /list.")
    return Reply(_short(ans))


# ---------- commands ----------

def parse_id(arg):
    try:
        return int(str(arg).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None


def cmd_list(uid) -> Reply:
    rows = db.list_open(uid)
    if not rows:
        return Reply("Nothing open. Tell me something to remember.")
    return Reply(_short("\n".join(item_line(r) for r in rows)))


def cmd_done(uid, arg) -> Reply:
    i = parse_id(arg)
    if i is None:
        return Reply("Usage: /done <id>")
    return Reply(f"Done #{i} ✅" if db.set_status(uid, i, "done") else f"I can't find #{i}.")


def cmd_snooze(uid, arg, now=None) -> Reply:
    i = parse_id(arg)
    if i is None:
        return Reply("Usage: /snooze <id>")
    ok = db.snooze(uid, i, 24, now=now)
    return Reply(f"Snoozed #{i} for a day 😴" if ok else f"I can't find an open item #{i}.")


def cmd_delete(uid, arg) -> Reply:
    i = parse_id(arg)
    if i is None:
        return Reply("Usage: /delete <id>")
    return Reply(f"Deleted #{i} forever." if db.delete_item(uid, i) else f"I can't find #{i}.")


def export_json(uid) -> str:
    return json.dumps(db.export_all(uid), ensure_ascii=False, indent=2)


def button(uid, data, now=None):
    """Handle an inline-button press. Returns a Reply, or None for unknown data."""
    action, _, raw = (data or "").partition(":")
    item_id = parse_id(raw)
    if item_id is None:
        return None
    if action == "done":
        ok, text = db.set_status(uid, item_id, "done"), f"Done #{item_id} ✅"
    elif action == "arc":
        ok, text = db.set_status(uid, item_id, "archived"), f"Dropped #{item_id} 🗄"
    elif action == "snz":
        ok, text = db.snooze(uid, item_id, 24, now=now), f"Snoozed #{item_id} for a day 😴"
    else:
        return None
    return Reply(text if ok else "That item is no longer there.")


# ---------- nudges ----------

def in_quiet_hours(now) -> bool:
    h = to_local(now).hour
    if QUIET_START > QUIET_END:  # window wraps past midnight
        return h >= QUIET_START or h < QUIET_END
    return QUIET_START <= h < QUIET_END


def due_nudges(now=None) -> list:
    """Nudges to send now, as (user_id, item_id, Reply).

    The caller sends each one and then calls db.mark_nudged(item_id, user_id, now),
    so a failed send is retried on the next run."""
    now = now or db.now_utc()
    if in_quiet_hours(now):
        return []
    out = []
    for r, reason in db.pick_nudges(now):
        if reason == "due":
            reply = Reply(f"⏰ Due: {r['title']} (#{r['id']})", item_buttons(r["id"]))
        else:
            days = (now - db.from_str(r["created_at"])).days
            reply = Reply(
                f"💡 You told me this {days} days ago: “{r['title']}” (#{r['id']}). Still want it?",
                item_buttons(r["id"], with_archive=True),
            )
        out.append((r["user_id"], r["id"], reply))
    return out
