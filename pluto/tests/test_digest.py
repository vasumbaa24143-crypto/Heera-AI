"""The morning digest: what's in it, when it's sent, and when it stays silent."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import bot
import core
import db
import simulate
import offline

UID, OTHER = 111, 222
H, D = timedelta(hours=1), timedelta(days=1)


def ist(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=core.TZ).astimezone(timezone.utc)


MON_0830 = ist(2026, 11, 2, 8, 30)   # Monday 2 Nov, digest time
LONG_AGO = ist(2026, 10, 1, 9, 0)


def add(uid=UID, kind="task", title="t", due=None, created=LONG_AGO, priority="normal"):
    return db.add_item(uid, kind, title, title, db.to_str(due) if due else None,
                       now=created, priority=priority)


def run(now, users=(UID,)):
    """One digest-job pass (like bot.digest_job): returns the texts that were sent."""
    sent = []
    for uid, reply, to_mark in core.due_digests(list(users), now):
        if reply is not None:
            sent.append(reply.text)
        core.digest_done(uid, to_mark, now)
    return sent


# ---------- content ----------

def test_full_digest_layout():
    a = add(title="Renew passport", due=ist(2026, 11, 1, 10), priority="high")
    b = add(title="Send deck to Rahul", due=ist(2026, 11, 1, 18))
    c = add(title="Call bank", due=ist(2026, 11, 2, 15))
    i = add(kind="idea", title="YouTube tech news segment", created=ist(2026, 10, 21, 9))
    add(kind="note", title="wifi password in the drawer")          # notes never appear
    reply = core.build_digest(UID, MON_0830)
    assert reply.text == (
        "☀️ Good morning! Here's your day (Mon 02 Nov):\n\n"
        "⚠️ Overdue (2)\n"
        f"• #{a} 🔴 Renew passport (was due Sun 01 Nov, 10:00)\n"
        f"• #{b} Send deck to Rahul (was due Sun 01 Nov, 18:00)\n\n"
        "📅 Today (1)\n"
        f"• #{c} Call bank — 15:00\n\n"
        "💡 From a while back\n"
        f"• #{i} YouTube tech news segment (you told me 11 days ago). Still interested?"
    )
    assert reply.buttons == [
        [("✅ #%d" % a, f"done:{a}"), ("✅ #%d" % b, f"done:{b}"), ("✅ #%d" % c, f"done:{c}")],
        [("🗄 Drop idea", f"arc:{i}")],
    ]


def test_high_priority_first_within_the_same_day():
    n = add(title="normal", due=ist(2026, 11, 1, 9))
    h = add(title="urgent", due=ist(2026, 11, 1, 20), priority="high")
    old = add(title="older", due=ist(2026, 10, 30, 20))
    text = core.build_digest(UID, MON_0830).text
    assert text.index(f"#{old} ") < text.index(f"#{h} ") < text.index(f"#{n} ")


def test_today_ends_at_local_midnight():
    tonight = add(title="tonight", due=ist(2026, 11, 2, 23, 30))
    tomorrow = add(title="tomorrow", due=ist(2026, 11, 3, 0, 30))
    text = core.build_digest(UID, MON_0830).text
    assert f"#{tonight} tonight — 23:30" in text and "tomorrow" not in text


def test_sections_are_capped_at_5_with_a_pointer_to_list():
    for n in range(7):
        add(title=f"late {n}", due=ist(2026, 11, 1, 9, n))
    text = core.build_digest(UID, MON_0830).text
    assert "⚠️ Overdue (7)" in text and "late 4" in text and "late 5" not in text
    assert "…and 2 more — /list" in text


def test_done_archived_snoozed_and_other_users_items_are_left_out():
    d = add(title="done one", due=ist(2026, 11, 1, 9))
    db.set_status(UID, d, "done")
    z = add(title="snoozed one", due=ist(2026, 11, 1, 9))
    db.snooze(UID, z, 24, now=MON_0830 - H)
    add(OTHER, title="someone else's", due=ist(2026, 11, 1, 9))
    assert core.build_digest(UID, MON_0830) is None


def test_idea_must_be_a_week_old_and_not_shown_this_week():
    add(kind="idea", title="fresh idea", created=MON_0830 - 6 * D)
    assert core.build_digest(UID, MON_0830) is None
    i = add(kind="idea", title="old idea", created=MON_0830 - 8 * D)
    db.touch_nudged(i, UID, MON_0830 - 3 * D)             # nudged 3 days ago
    assert core.build_digest(UID, MON_0830) is None
    later = core.build_digest(UID, MON_0830 + 5 * D).text   # both are eligible by now:
    assert "fresh idea" in later and "old idea" not in later  # the never-shown one goes first


def test_ideas_take_turns_day_by_day():
    a = add(kind="idea", title="idea A", created=LONG_AGO)
    b = add(kind="idea", title="idea B", created=LONG_AGO + D)
    first = run(MON_0830)[0]
    second = run(MON_0830 + D)[0]
    assert f"#{a} idea A" in first and f"#{b} idea B" in second


# ---------- when it's sent ----------

def test_sent_once_at_digest_time_then_again_next_day():
    add(title="x", due=ist(2026, 11, 1, 9))
    assert run(MON_0830 - timedelta(minutes=1)) == []
    assert len(run(MON_0830)) == 1
    assert run(MON_0830 + timedelta(minutes=1)) == []        # once per day
    assert run(MON_0830 + 2 * H) == []
    assert len(run(MON_0830 + D)) == 1                      # Tuesday


def test_catch_up_within_4_hours_but_not_later():
    add(title="x", due=ist(2026, 11, 1, 9))
    assert len(run(ist(2026, 11, 2, 12, 15))) == 1           # bot was down at 08:30
    assert run(ist(2026, 11, 3, 12, 31)) == []               # Tuesday: too late
    assert db.get_setting(UID, "digest_last_date") == "2026-11-03"  # and not retried


def test_nothing_to_say_sends_nothing_and_doesnt_keep_checking():
    add(kind="note", title="just a note")
    assert run(MON_0830) == []
    assert db.get_setting(UID, "digest_last_date") == "2026-11-02"
    add(title="added later", due=ist(2026, 11, 2, 7))       # overdue now, but today is done
    assert run(MON_0830 + H) == []


def test_new_user_with_nothing_saved_gets_nothing():
    assert core.due_digests([UID], MON_0830) == []


def test_off_and_custom_time():
    add(title="x", due=ist(2026, 11, 1, 9))
    core.cmd_digest(UID, "off")
    assert run(MON_0830) == []
    assert core.cmd_digest(UID, "7:05").text == "Done — your morning digest will come at 07:05."
    assert run(ist(2026, 11, 2, 7, 4)) == []
    assert len(run(ist(2026, 11, 2, 7, 5))) == 1


def test_digest_settings_messages():
    assert "on, at 08:30" in core.cmd_digest(UID).text
    assert core.cmd_digest(UID, "25:00").text.startswith("Usage:")
    assert core.cmd_digest(UID, "off").text.startswith("Morning digest is off")
    assert "off" in core.cmd_digest(UID).text
    assert core.cmd_digest(UID, "on").text == "Morning digest is on, at 08:30 every day."


def test_preview_doesnt_count_as_sent():
    add(title="x", due=ist(2026, 11, 1, 9))
    assert core.cmd_digest(UID, "now", MON_0830).text.startswith("☀️ Good morning!")
    assert len(run(MON_0830)) == 1
    db.set_status(UID, 1, "done")
    assert "Nothing for today's digest" in core.cmd_digest(UID, "now", MON_0830 + H).text


# ---------- how it fits with reminders and nudges ----------

def test_overdue_items_dont_get_a_repeat_nudge_right_after_the_digest():
    i = add(title="late", due=ist(2026, 11, 1, 9))         # first reminder never went out
    run(MON_0830)
    assert core.due_nudges(MON_0830 + H) == []
    assert db.get_item(UID, i)["nudge_count"] == 1


def test_items_due_later_today_still_get_their_own_reminder():
    i = add(title="call bank", due=ist(2026, 11, 2, 15))
    assert len(run(MON_0830)) == 1
    [(_, item_id, reply)] = core.due_nudges(ist(2026, 11, 2, 15))
    assert item_id == i and reply.text.startswith("⏰ Reminder: call bank")


def test_digest_does_not_use_up_the_daily_nudge_cap():
    for n in range(3):
        add(title=f"late {n}", due=ist(2026, 11, 1, 9))
    run(MON_0830)
    assert db.nudges_last_24h(UID, MON_0830) == 0


# ---------- Telegram and simulator ----------

class FakeBot:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("Telegram down")
        self.sent.append((chat_id, text, reply_markup))


def test_bot_digest_job_sends_to_the_allowed_user(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    monkeypatch.setattr(db, "now_utc", lambda: MON_0830)
    add(title="x", due=ist(2026, 11, 1, 9))
    add(OTHER, title="y", due=ist(2026, 11, 1, 9))
    b = FakeBot()
    asyncio.run(bot.digest_job(SimpleNamespace(bot=b)))
    [(chat, text, markup)] = b.sent
    assert chat == UID and text.startswith("☀️ Good morning!")
    assert markup.inline_keyboard[0][0].callback_data == "done:1"


def test_bot_digest_job_retries_after_a_failed_send(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    monkeypatch.setattr(db, "now_utc", lambda: MON_0830)
    add(title="x", due=ist(2026, 11, 1, 9))
    asyncio.run(bot.digest_job(SimpleNamespace(bot=FakeBot(fail=True))))
    assert db.get_setting(UID, "digest_last_date") is None
    b = FakeBot()
    asyncio.run(bot.digest_job(SimpleNamespace(bot=b)))
    assert len(b.sent) == 1


def test_digest_command_is_registered_and_in_start_text(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-not-a-real-token")
    app = bot.build_app()
    names = {c for hs in app.handlers.values() for h in hs for c in getattr(h, "commands", ())}
    assert "digest" in names
    assert "/digest" in core.START_TEXT


def test_simulator_digest_commands(monkeypatch):
    monkeypatch.setattr(db, "now_utc", lambda: MON_0830)
    s = simulate.Session(offline, uid=UID)
    add(title="x", due=ist(2026, 11, 1, 9))
    assert s.handle("/digest now")[0].startswith("☀️ Good morning!")
    assert s.handle("/digest 09:00") == ["Done — your morning digest will come at 09:00."]
