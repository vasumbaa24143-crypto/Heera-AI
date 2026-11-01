"""Pluto's conversation flow, independent of Telegram.

bot.py (Telegram) and simulate.py (terminal) are thin adapters around this
module, so both run exactly the same logic. Everything here is synchronous;
the bot runs it in a worker thread.

Every message goes to the assistant (`llm.respond`), which sees the recent
conversation and the user's saved items and returns a reply plus actions:

    {"reply": str,
     "save": [{"kind", "title", "due_local", "priority", "uses_previous_message"}],
     "done": [item_id, ...]}

The code, not the model, decides what actually happens: actions are checked
against the user's own items, saves are confirmed with real ids, and a reply
that cites an item the user does not have is withheld.

`llm` is any object with respond(message, history, items, now_local) -> dict.
(llm.py talks to Gemini; offline.py and the tests provide stand-ins.)
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import db

log = logging.getLogger("pluto")

TZ = ZoneInfo(os.getenv("PLUTO_TZ", "Asia/Kolkata"))
QUIET_START, QUIET_END = 22, 8  # no nudges from 22:00 to 08:00 local
KIND_ICON = {"idea": "💡", "promise": "🤝", "task": "✅", "note": "📝"}
ACTION_KINDS = ("task", "promise")  # created only when the user asks for them
PRIORITY_MARK = {"high": "🔴 ", "normal": "", "low": "⚪ "}
TITLE_MAX = 120
MAX_REPLY = 4000  # Telegram's limit is 4096 characters
HISTORY_TURNS = 100  # messages of conversation the assistant sees

START_TEXT = (
    "Hi, I'm Pluto 👋 Talk to me like you would to an assistant.\n\n"
    "I quietly remember useful things you tell me (facts, ideas, details).\n"
    "For to-dos and reminders, just say so — and tell me if it's urgent:\n"
    "• “remind me to call Rahul kal 5 baje”\n"
    "• “todo: renew passport, urgent”\n\n"
    "Then ask me things like:\n"
    "• “what's on my plate today?” / “is hafte kya kya hai?”\n"
    "• “what have we discussed about the YouTube idea?”\n\n"
    "/list  open items · /done <id> · /snooze <id> (+1 day)\n"
    "/delete <id>  remove forever · /forget  clear our chat history\n"
    "/export  download everything"
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
    mark = PRIORITY_MARK.get(r["priority"], "") if r["kind"] in ACTION_KINDS else ""
    return f"{KIND_ICON.get(r['kind'], '•')} #{r['id']} {mark}{r['title']}{due}"


def item_buttons(item_id: int, with_archive: bool = False) -> list:
    b = [("✅ Done", f"done:{item_id}"), ("😴 +1 day", f"snz:{item_id}")]
    if with_archive:
        b.append(("🗄 Drop", f"arc:{item_id}"))
    return b


def _short(text: str) -> str:
    return text if len(text) <= MAX_REPLY else text[: MAX_REPLY - 1] + "…"


# ---------- time buckets ----------

def bucket(due_utc, now) -> str:
    """Where a due date falls relative to now, in local time.

    One of: overdue, today, tomorrow, this week, this month, later, no date.
    Worked out here so the assistant never has to do date arithmetic."""
    if not due_utc:
        return "no date"
    d, n = to_local(db.from_str(due_utc)), to_local(now)
    if d < n:
        return "overdue"
    if d.date() == n.date():
        return "today"
    if d.date() == n.date() + timedelta(days=1):
        return "tomorrow"
    if d.date() <= n.date() + timedelta(days=6 - n.weekday()):  # through Sunday
        return "this week"
    if (d.year, d.month) == (n.year, n.month):
        return "this month"
    return "later"


def item_views(rows, now) -> list:
    """Saved items as the assistant sees them: local times and a time bucket."""
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "kind": r["kind"],
            "priority": r["priority"],
            "status": r["status"],
            "title": r["title"],
            "original_text": r["original_text"],
            "saved_local": to_local(db.from_str(r["created_at"])).strftime("%a %Y-%m-%d %H:%M"),
            "due_local": to_local(db.from_str(r["due_at"])).strftime("%a %Y-%m-%d %H:%M")
            if r["due_at"] else None,
            "bucket": bucket(r["due_at"], now) if r["status"] == "open" else r["status"],
        })
    return out


def history_views(rows) -> list:
    return [
        {"role": r["role"],
         "when": to_local(db.from_str(r["created_at"])).strftime("%a %Y-%m-%d %H:%M"),
         "text": r["text"]}
        for r in rows
    ]


# ---------- the conversation ----------

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


def parse_id(arg):
    try:
        return int(str(arg).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None


def _normalize(result) -> dict:
    """Check the assistant's output; raise ValueError if it is unusable."""
    if not isinstance(result, dict):
        raise ValueError("assistant did not return an object")
    reply = result.get("reply") or ""
    save = result.get("save") or []
    done = result.get("done") or []
    if not isinstance(reply, str) or not isinstance(save, list) or not isinstance(done, list):
        raise ValueError("assistant output has the wrong shape")
    return {
        "reply": reply.strip(),
        "save": [s for s in save if isinstance(s, dict)],
        "done": [i for i in (parse_id(d) for d in done) if i is not None],
    }


def save_raw(uid, text, now) -> Reply:
    item_id = db.add_item(uid, "note", text[:80], text, now=now)
    return Reply(
        f"I couldn't think properly just now, so I saved your message as a plain note "
        f"(#{item_id}) to be safe. /delete {item_id} if you didn't need it."
    )


def _previous_user_message(history):
    for m in reversed(history):
        if m["role"] == "user":
            return m["text"]
    return None


def _cited_ids(text) -> set:
    return {int(n) for n in re.findall(r"#(\d+)", text)}


def chat(uid, text, llm, now=None) -> list:
    """Handle one free-text message from the user and return Pluto's replies."""
    now = now or db.now_utc()
    text = (text or "").strip()
    if not text:
        return [Reply("I'm listening — say anything 🙂")]

    history = db.recent_messages(uid, HISTORY_TURNS)
    rows = db.context_items(uid)
    db.add_message(uid, "user", text, now=now)  # the user's words, kept as said

    try:
        result = _normalize(
            llm.respond(text, history_views(history), item_views(rows, now), to_local(now))
        )
    except Exception:
        # Never lose a thought: if the assistant fails, keep the message as a note.
        log.exception("assistant failed; saving raw note")
        reply = save_raw(uid, text, now)
        db.add_message(uid, "pluto", reply.text, now=now)
        return [reply]

    known_ids = {r["id"] for r in rows}
    open_ids = {r["id"] for r in rows if r["status"] == "open"}

    # Mark done only items that really are this user's open items.
    done_lines = []
    for i in dict.fromkeys(result["done"]):
        if i in open_ids and db.set_status(uid, i, "done"):
            done_lines.append(f"✅ Marked #{i} done.")

    # Save what the assistant decided to keep (information automatically;
    # tasks, promises and reminders only when the user asked for them).
    saved = []
    prev = _previous_user_message(history)
    for it in result["save"]:
        kind = it.get("kind") if it.get("kind") in KIND_ICON else "note"
        title = it.get("title") if isinstance(it.get("title"), str) else ""
        title = (title.strip() or text[:80])[:TITLE_MAX]
        due_utc = parse_due(it.get("due_local"))
        original = text
        if it.get("uses_previous_message") is True and prev:
            original = f"{prev}\n{text}"  # e.g. "remind me to call Rahul" + "kal 5 baje"
        priority = it.get("priority") if it.get("priority") in db.PRIORITIES else "normal"
        item_id = db.add_item(uid, kind, title, original, due_utc, now=now, priority=priority)
        saved.append(db.get_item(uid, item_id))

    # Never invent a memory: withhold a reply that cites items the user doesn't have.
    reply = result["reply"]
    invented = _cited_ids(reply) - known_ids - {r["id"] for r in saved}
    if invented:
        log.warning("assistant cited unknown items %s; reply withheld", sorted(invented))
        reply = "I couldn't find that in what you've saved with me."

    replies = []
    head = "\n".join(x for x in [reply, *done_lines] if x)
    if head:
        replies.append(Reply(_short(head)))
    for r in saved:
        follow_up = r["kind"] in ACTION_KINDS or r["due_at"] is not None
        verb = "Added" if r["kind"] in ACTION_KINDS else "Noted"
        replies.append(
            Reply(f"{verb} {item_line(r)}", item_buttons(r["id"]) if follow_up else [])
        )
    if not replies:
        replies.append(Reply("🙂"))

    db.add_message(uid, "pluto", "\n".join(r.text for r in replies), now=now)
    return replies


# ---------- commands ----------

def cmd_list(uid) -> Reply:
    rows = db.list_open(uid)
    if not rows:
        return Reply("Nothing open. Say “remind me …” or “note: …” and I'll keep it.")
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


def cmd_forget(uid) -> Reply:
    n = db.forget_messages(uid)
    return Reply(f"Cleared our chat history ({n} messages). Your saved items are untouched.")


def export_json(uid) -> str:
    return json.dumps(
        {"items": db.export_all(uid), "messages": db.export_messages(uid)},
        ensure_ascii=False,
        indent=2,
    )


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
            reply = Reply(f"⏰ Reminder: {r['title']} (#{r['id']})", item_buttons(r["id"]))
        else:
            days = (now - db.from_str(r["created_at"])).days
            reply = Reply(
                f"💡 You told me this {days} days ago: “{r['title']}” (#{r['id']}). Still want it?",
                item_buttons(r["id"], with_archive=True),
            )
        out.append((r["user_id"], r["id"], reply))
    return out
