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

`llm` is any object with
respond(message, history, items, now_local, prefer=None, related=(), summary="") -> dict,
and (for rolling summaries) summarise(previous, messages, now_local, prefer=None) -> list,
and choices() -> [(name, label)] for the /model menu.
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
HISTORY_TURNS = 30   # most recent messages the assistant sees, word for word
SEARCH_HITS = 10     # older items, and older messages, found by search for each message
SUMMARY_BATCH_MIN = 20     # summarise once this many messages have left the recent window
SUMMARY_BATCH_MAX = 100    # at most this many per summarising call
SUMMARY_MAX_LINES = 15
SUMMARY_MAX_CHARS = 1500
SUMMARY_RETRY = timedelta(minutes=10)  # after a failed summary, wait before trying again

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
    "/model  choose which AI thinks first\n"
    "/summary  what I remember from older chats\n"
    "/sheet  your Google Sheet dashboard\n"
    "/digest  morning digest (on at 08:30; /digest now to preview)\n"
    "/export  download everything"
)


@dataclass
class Reply:
    text: str
    # [(label, callback_data), ...] shown as one row, or [[...], [...]] for several rows
    buttons: list = field(default_factory=list)


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
    # Search everything older for this message's words, so "what did we discuss about X"
    # works however long ago it was, without sending the whole history every time.
    rows = sorted(rows + db.search_items(uid, text, SEARCH_HITS, exclude_ids=[r["id"] for r in rows]),
                  key=lambda r: r["id"])
    oldest_recent = history[0]["id"] if history else None
    related = db.search_messages(uid, text, SEARCH_HITS, before_id=oldest_recent) if history else []
    db.add_message(uid, "user", text, now=now)  # the user's words, kept as said

    try:
        result = _normalize(
            llm.respond(text, history_views(history), item_views(rows, now), to_local(now),
                        prefer=db.get_setting(uid, "model"), related=history_views(related),
                        summary=current_summary(uid))
        )
    except Exception:
        # Never lose a thought: if the assistant fails, keep the message as a note.
        log.exception("assistant failed; saving raw note")
        reply = save_raw(uid, text, now)
        db.add_message(uid, "pluto", reply.text, now=now)
        return [reply]

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
    cited = _cited_ids(reply)
    invented = cited - db.existing_item_ids(uid, cited) - {r["id"] for r in saved}
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


def cmd_model(uid, llm, arg=None) -> Reply:
    """/model: show the AI options as buttons, or pick one directly (/model groq)."""
    options = llm.choices() if hasattr(llm, "choices") else []
    if not options:
        return Reply("There's no AI model to choose from right now (offline, or no AI keys set).")
    if arg:
        return choose_model(uid, llm, arg.strip().lower())
    current = db.get_setting(uid, "model", "auto")
    if current not in {n for n, _ in options}:
        current = "auto"
    names = " → ".join(lbl.split(" (")[0] for _, lbl in options)
    buttons = [(("✓ " if current == "auto" else "") + f"⚡ Auto ({names})", "model:auto")]
    buttons += [(("✓ " if current == n else "") + lbl, f"model:{n}") for n, lbl in options]
    lines = [
        "🧠 Which AI should think first? The others stay as backup if it's busy or down.",
        "",
        f"Now: {'Auto' if current == 'auto' else dict(options)[current]}",
    ]
    return Reply("\n".join(lines), [[b] for b in buttons])


def choose_model(uid, llm, name) -> Reply:
    options = dict(llm.choices() if hasattr(llm, "choices") else [])
    if name == "auto":
        db.set_setting(uid, "model", "auto")
        order = " → ".join(lbl.split(" (")[0] for lbl in options.values())
        return Reply(f"⚡ Auto: I'll use {order}, falling back in that order.")
    if name not in options:
        avail = ", ".join(["auto", *options]) or "none"
        return Reply(f"I don't have “{name}” set up. Options: {avail}.")
    db.set_setting(uid, "model", name)
    others = [lbl.split(" (")[0] for n, lbl in options.items() if n != name]
    verb = "stays" if len(others) == 1 else "stay"
    backup = f" {' and '.join(others)} {verb} as backup." if others else ""
    return Reply(f"🧠 Done — I'll think with {options[name]} first.{backup}")


def export_json(uid) -> str:
    return json.dumps(
        {"items": db.export_all(uid), "messages": db.export_messages(uid),
         "summaries": db.export_summaries(uid)},
        ensure_ascii=False,
        indent=2,
    )


def button(uid, data, now=None, llm=None):
    """Handle an inline-button press. Returns a Reply, or None for unknown data."""
    action, _, raw = (data or "").partition(":")
    if action == "model":
        return choose_model(uid, llm, raw) if llm is not None else None
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

    A reminder's first alert goes out at the time the user asked for, even in
    quiet hours. Repeats and stale-idea nudges wait for daytime and respect the
    daily cap. The caller sends each one and then calls
    db.mark_nudged(item_id, user_id, now), so a failed send is retried."""
    now = now or db.now_utc()
    out = []
    # With the morning digest on, old ideas come up there (one a day), so they
    # don't also get their own nudges.
    no_digest = lambda uid: not digest_settings(uid)[0]  # noqa: E731
    for r, reason in db.pick_nudges(now, quiet=in_quiet_hours(now), idea_nudges=no_digest):
        if reason == "reminder":
            reply = Reply(f"⏰ Reminder: {r['title']} (#{r['id']})", item_buttons(r["id"]))
        elif reason == "due":
            reply = Reply(f"⏰ Still pending: {r['title']} (#{r['id']}) — due {fmt_due(r['due_at'])}",
                          item_buttons(r["id"]))
        else:
            days = (now - db.from_str(r["created_at"])).days
            reply = Reply(
                f"💡 You told me this {days} days ago: “{r['title']}” (#{r['id']}). Still want it?",
                item_buttons(r["id"], with_archive=True),
            )
        out.append((r["user_id"], r["id"], reply))
    return out


# ---------- daily digest ----------

DIGEST_DEFAULT_TIME = "08:30"
DIGEST_GRACE = timedelta(hours=4)    # if the bot was down at digest time, catch up within this
DIGEST_MAX_PER_SECTION = 5
DIGEST_IDEA_AFTER = timedelta(days=7)  # an idea must be this old, and not shown this recently
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def digest_settings(uid) -> tuple:
    """(enabled, "HH:MM"). On by default."""
    enabled = db.get_setting(uid, "digest_enabled", "on") != "off"
    when = db.get_setting(uid, "digest_time", DIGEST_DEFAULT_TIME)
    return enabled, when if _HHMM.match(when) else DIGEST_DEFAULT_TIME


def _digest_start(uid, now):
    """Today's digest time (local) for this user, as an aware datetime."""
    _, when = digest_settings(uid)
    h, m = (int(x) for x in when.split(":"))
    return to_local(now).replace(hour=h, minute=m, second=0, microsecond=0)


def _digest_content(uid, now):
    """(Reply or None, ids to mark as shown). None means there's nothing worth saying."""
    local = to_local(now)
    end_of_day = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    overdue, today, idea = db.digest_rows(uid, now, end_of_day, now - DIGEST_IDEA_AFTER)
    if not overdue and not today and idea is None:
        return None, []

    rank = {"high": 0, "normal": 1, "low": 2}
    overdue = sorted(overdue, key=lambda r: (to_local(db.from_str(r["due_at"])).date(),
                                             rank.get(r["priority"], 1), r["due_at"], r["id"]))

    def section(title, rows, line):
        out = [f"{title} ({len(rows)})"] + [line(r) for r in rows[:DIGEST_MAX_PER_SECTION]]
        if len(rows) > DIGEST_MAX_PER_SECTION:
            out.append(f"…and {len(rows) - DIGEST_MAX_PER_SECTION} more — /list")
        return "\n".join(out)

    def mark(r):
        return PRIORITY_MARK.get(r["priority"], "") if r["kind"] in ACTION_KINDS else ""

    def overdue_line(r):
        return f"• #{r['id']} {mark(r)}{r['title']} (was due {fmt_due(r['due_at'])})"

    def today_line(r):
        return f"• #{r['id']} {mark(r)}{r['title']} — {to_local(db.from_str(r['due_at'])):%H:%M}"

    parts = [f"☀️ Good morning! Here's your day ({local:%a %d %b}):"]
    if overdue:
        parts.append(section("⚠️ Overdue", overdue, overdue_line))
    if today:
        parts.append(section("📅 Today", today, today_line))
    if idea is not None:
        days = (now - db.from_str(idea["created_at"])).days
        parts.append(f"💡 From a while back\n• #{idea['id']} {idea['title']} "
                     f"(you told me {days} days ago). Still interested?")

    shown = overdue[:DIGEST_MAX_PER_SECTION] + today[:DIGEST_MAX_PER_SECTION]
    done_buttons = [(f"✅ #{r['id']}", f"done:{r['id']}") for r in shown]
    rows = [done_buttons[i:i + 4] for i in range(0, len(done_buttons), 4)]
    if idea is not None:
        rows.append([("🗄 Drop idea", f"arc:{idea['id']}")])

    # Overdue items and the idea count as nudged, so a repeat nudge doesn't follow
    # an hour later. Items due later today are NOT marked: their own reminder
    # still fires at the time the user set.
    to_mark = [r["id"] for r in overdue[:DIGEST_MAX_PER_SECTION]]
    if idea is not None:
        to_mark.append(idea["id"])
    return Reply(_short("\n\n".join(parts)), rows), to_mark


def build_digest(uid, now=None):
    """Today's digest as a Reply, or None when there's nothing to say. Changes nothing."""
    return _digest_content(uid, now or db.now_utc())[0]


def due_digests(user_ids, now=None) -> list:
    """Digests to handle now, as (user_id, Reply or None, ids_to_mark).

    Due once per local day, from the user's digest time until DIGEST_GRACE later
    (so a bot that was down at 08:30 still sends at 09:10, but not at 15:00).
    Reply None means "nothing to say": call digest_done() without sending.
    After a successful send, call digest_done() so it isn't sent twice."""
    now = now or db.now_utc()
    today = to_local(now).date().isoformat()
    out = []
    for uid in user_ids:
        enabled, _ = digest_settings(uid)
        if not enabled or db.get_setting(uid, "digest_last_date") == today:
            continue
        if db.count_items(uid) == 0:
            continue  # nothing saved yet: no empty "good morning"s
        start = _digest_start(uid, now)
        if to_local(now) < start:
            continue
        if to_local(now) >= start + DIGEST_GRACE:
            out.append((uid, None, []))  # too late for a morning digest today
            continue
        reply, to_mark = _digest_content(uid, now)
        out.append((uid, reply, to_mark))
    return out


def digest_done(uid, ids, now=None) -> None:
    now = now or db.now_utc()
    for item_id in ids:
        db.touch_nudged(item_id, uid, now)
    db.set_setting(uid, "digest_last_date", to_local(now).date().isoformat())


def cmd_digest(uid, arg=None, now=None) -> Reply:
    """/digest            show the setting
    /digest now        preview today's digest (doesn't count as sent)
    /digest on|off     turn it on or off
    /digest 07:45      change the time"""
    arg = (arg or "").strip().lower()
    if arg == "now":
        return build_digest(uid, now) or Reply("Nothing for today's digest: nothing overdue, "
                                               "nothing due today, no old idea to revisit. 🎉")
    if arg in ("on", "off"):
        db.set_setting(uid, "digest_enabled", arg)
        if arg == "off":
            return Reply("Morning digest is off. /digest on to turn it back on.")
        return Reply(f"Morning digest is on, at {digest_settings(uid)[1]} every day.")
    if arg:
        m = _HHMM.match(arg)
        if not m:
            return Reply("Usage: /digest, /digest now, /digest on|off, or /digest 07:45")
        when = f"{int(m.group(1)):02d}:{m.group(2)}"
        db.set_setting(uid, "digest_time", when)
        db.set_setting(uid, "digest_enabled", "on")
        return Reply(f"Done — your morning digest will come at {when}.")
    enabled, when = digest_settings(uid)
    state = f"on, at {when} every day" if enabled else "off"
    return Reply(f"☀️ Morning digest is {state}. It lists what's overdue, what's due today and "
                 "one old idea, and is skipped when there's nothing to say.\n"
                 "/digest now to preview · /digest 07:45 to change the time · /digest off")



# ---------- rolling summary of older conversation ----------

def current_summary(uid) -> str:
    row = db.latest_summary(uid)
    return row["text"] if row else ""


def _clean_summary(lines) -> str:
    """Tidy the model's lines; raise ValueError if they're unusable (fail closed)."""
    if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
        raise ValueError("summary lines must be strings")
    out = []
    for ln in lines:
        ln = " ".join(ln.split())
        if not ln or ln.lower().strip("-• ()") in ("nothing", "none"):
            continue
        out.append(ln if ln.startswith(("-", "•")) else f"- {ln}")
    text = "\n".join(out[:SUMMARY_MAX_LINES])
    if len(text) > SUMMARY_MAX_CHARS:  # cut at a line boundary
        text = text[:SUMMARY_MAX_CHARS].rsplit("\n", 1)[0]
    return text


def update_summary(uid, llm, now=None, force=False) -> bool:
    """Fold messages that have left the recent window into the rolling summary.

    Runs when at least SUMMARY_BATCH_MIN such messages are waiting (any number with
    force=True). On any failure nothing changes (the previous summary stays) and it waits
    SUMMARY_RETRY before trying again. Returns True if a new summary was stored."""
    now = now or db.now_utc()
    if not hasattr(llm, "summarise"):
        return False
    retry_after = db.get_setting(uid, "summary_retry_after")
    if retry_after and not force and db.to_str(now) < retry_after:
        return False
    rows, waiting = db.unsummarised_messages(uid, HISTORY_TURNS, SUMMARY_BATCH_MAX)
    if not rows or (waiting < SUMMARY_BATCH_MIN and not force):
        return False
    prev = db.latest_summary(uid)
    try:
        lines = llm.summarise(prev["text"] if prev else "", history_views(rows), to_local(now),
                              prefer=db.get_setting(uid, "model"))
        text = _clean_summary(lines)
    except Exception:
        log.exception("summary failed; keeping the previous one")
        db.set_setting(uid, "summary_retry_after", db.to_str(now + SUMMARY_RETRY))
        return False
    start = prev["from_msg_id"] if prev else rows[0]["id"]
    db.add_summary(uid, start, rows[-1]["id"], text, now=now)
    return True


def cmd_summary(uid) -> Reply:
    """/summary: what Pluto remembers about older chats (besides saved items)."""
    text = current_summary(uid)
    if not text:
        return Reply("No summary yet. Once our chat grows past the last 30 messages, I keep a "
                     "short summary of the older part here. Your saved items are kept separately "
                     "(/list).")
    return Reply("🧠 What I remember from our older chats:\n" + text +
                 "\n\n/forget clears this and the chat history; saved items stay.")


# ---------- Google Sheet dashboard (data only; sheets.py does the Google calls) ----------

SHEET_TABS = ("Dashboard", "To-dos", "Done", "Notes & ideas")
_BUCKET_ORDER = ["overdue", "today", "tomorrow", "this week", "this month", "later", "no date"]
_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


def _local_str(utc_s):
    return to_local(db.from_str(utc_s)).strftime("%Y-%m-%d %H:%M") if utc_s else ""


def sheet_tables(uid, now=None) -> dict:
    """The user's items as rows for each tab: {tab: [header, row, row, ...]}.

    Every cell is plain text or a number; sheets.py writes them as RAW values, so text
    a user typed (e.g. starting with "=") can never become a spreadsheet formula."""
    now = now or db.now_utc()
    rows = db.export_all(uid)
    is_todo = lambda r: r["kind"] in ACTION_KINDS or r["due_at"]  # noqa: E731
    todos = [r for r in rows if r["status"] == "open" and is_todo(r)]
    notes = [r for r in rows if r["status"] == "open" and not is_todo(r)]
    done = [r for r in rows if r["status"] != "open"]

    for r in todos:
        r["_bucket"] = bucket(r["due_at"], now)
    todos.sort(key=lambda r: (_BUCKET_ORDER.index(r["_bucket"]), r["due_at"] or "9999",
                              _PRIORITY_RANK.get(r["priority"], 1), r["id"]))
    notes.sort(key=lambda r: -r["id"])
    done.sort(key=lambda r: (r["closed_at"] or "", r["id"]), reverse=True)

    def count(pred):
        return sum(1 for r in todos if pred(r))

    week = {"overdue", "today", "tomorrow", "this week"}
    week_ago = db.to_str(now - timedelta(days=7))
    local = to_local(now)
    dashboard = [
        ["Pluto dashboard", ""],
        ["Last updated", f"{local:%Y-%m-%d %H:%M} ({TZ.key})"],
        ["", ""],
        ["⚠️ Overdue", count(lambda r: r["_bucket"] == "overdue")],
        ["📅 Due today", count(lambda r: r["_bucket"] == "today")],
        ["🗓 Due this week (incl. overdue)", count(lambda r: r["_bucket"] in week)],
        ["🔴 Urgent (high priority)", count(lambda r: r["priority"] == "high")],
        ["✅ Open to-dos", len(todos)],
        ["💡 Open ideas", sum(1 for r in notes if r["kind"] == "idea")],
        ["📝 Notes", sum(1 for r in notes if r["kind"] == "note")],
        ["🎉 Finished in the last 7 days",
         sum(1 for r in done if r["status"] == "done" and (r["closed_at"] or "") >= week_ago)],
        ["", ""],
        ["This sheet is updated by Pluto. Edits here are overwritten; change things in Telegram.", ""],
    ]
    return {
        "Dashboard": dashboard,
        "To-dos": [["ID", "To-do", "Type", "Priority", "Due", "When", "Added", "Your words"]] + [
            [r["id"], r["title"], r["kind"], r["priority"], _local_str(r["due_at"]),
             r["_bucket"], _local_str(r["created_at"]), r["original_text"]] for r in todos],
        "Done": [["ID", "Item", "Type", "Status", "Finished", "Due", "Added"]] + [
            [r["id"], r["title"], r["kind"], r["status"], _local_str(r["closed_at"]),
             _local_str(r["due_at"]), _local_str(r["created_at"])] for r in done],
        "Notes & ideas": [["ID", "Note / idea", "Type", "Added", "Your words"]] + [
            [r["id"], r["title"], r["kind"], _local_str(r["created_at"]), r["original_text"]]
            for r in notes],
    }


def _fingerprint(tables) -> str:
    """A hash of the sheet contents, ignoring the 'Last updated' time, so an unchanged
    dashboard isn't rewritten every few minutes."""
    import hashlib

    stable = {t: rows for t, rows in tables.items() if t != "Dashboard"}
    stable["Dashboard"] = [r for r in tables.get("Dashboard", []) if r[:1] != ["Last updated"]]
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def sync_sheet(uid, push, force=False, now=None) -> str:
    """Write this user's dashboard with `push(tables)` (sheets.push) if anything changed.

    Returns "synced" or "unchanged". On failure, records the error for /sheet and
    re-raises; the next run tries again."""
    now = now or db.now_utc()
    tables = sheet_tables(uid, now)
    fp = _fingerprint(tables)
    if not force and db.get_setting(uid, "sheet_hash") == fp:
        return "unchanged"
    try:
        link = push(tables)
    except Exception as e:
        db.set_setting(uid, "sheet_error", str(e)[:300])
        raise
    if isinstance(link, str) and link:
        db.set_setting(uid, "sheet_url", link)
    db.set_setting(uid, "sheet_hash", fp)
    db.set_setting(uid, "sheet_synced_at", db.to_str(now))
    db.set_setting(uid, "sheet_error", "")
    return "synced"


def cmd_sheet(uid, problem, link, robot_email="") -> Reply:
    """/sheet: where the dashboard is and whether syncing works."""
    if problem == "not set up":
        return Reply(
            "📊 No Google Sheet dashboard yet. The easiest setup (about 5 minutes) is in the "
            "README under “Google Sheet dashboard”: paste Pluto's Apps Script into your sheet, "
            "deploy it, and add GOOGLE_APPS_SCRIPT_URL and GOOGLE_APPS_SCRIPT_SECRET to .env.")
    if problem:
        return Reply(f"📊 The Google Sheet dashboard can't sync: {problem}.")
    synced = db.get_setting(uid, "sheet_synced_at")
    when = f"{to_local(db.from_str(synced)):%a %d %b, %H:%M}" if synced else "not yet"
    link = link or db.get_setting(uid, "sheet_url", "") or "(the link appears after the first update)"
    lines = [f"📊 Your dashboard: {link}", f"Last updated: {when} (checked every 5 minutes)."]
    err = db.get_setting(uid, "sheet_error")
    if err:
        lines.append(f"⚠️ Last attempt failed: {err}")
        if robot_email and ("403" in err or "permission" in err.lower()):
            lines.append(f"Share the sheet with {robot_email} as Editor, then send /sheet now.")
    lines.append("/sheet now to update it right away.")
    return Reply("\n".join(lines))
