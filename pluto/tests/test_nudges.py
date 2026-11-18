"""Nudge timing, quiet hours, the daily cap and snooze (core.due_nudges / db.pick_nudges)."""
from datetime import datetime, timedelta, timezone

import core
import db
from conftest import NOW

import pytest

UID, OTHER = 111, 222
H, D = timedelta(hours=1), timedelta(days=1)


@pytest.fixture(autouse=True)
def digest_off():
    """These tests cover the separate nudges, which run in full when the digest is off."""
    for uid in (UID, OTHER):
        db.set_setting(uid, "digest_enabled", "off")


def test_with_the_digest_on_ideas_are_not_nudged_separately():
    db.set_setting(UID, "digest_enabled", "on")
    add(kind="idea", created=NOW - 10 * D)
    task = add(due=NOW - H)
    assert run(NOW) == [task]


def add(uid=UID, kind="task", title="t", due=None, created=NOW):
    return db.add_item(uid, kind, title, title, db.to_str(due) if due else None, now=created)


def run(now):
    """One nudge-job pass: return the item ids nudged, marking them as sent."""
    out = core.due_nudges(now)
    for uid, item_id, _ in out:
        db.mark_nudged(item_id, uid, now)
    return [item_id for _, item_id, _ in out]


def ist(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=core.TZ).astimezone(timezone.utc)


# ---------- overdue items ----------

def test_not_nudged_before_due():
    add(due=NOW + H)
    assert run(NOW) == []


def test_overdue_task_nudged_every_6_hours_up_to_5_times():
    i = add(due=NOW)
    t = NOW
    assert run(t) == [i]
    assert run(t + 5 * H) == []          # too soon
    sent = 1
    while True:
        t += 6 * H
        if core.in_quiet_hours(t):
            continue
        got = run(t)
        if not got:
            break
        sent += 1
    assert sent == db.DUE_MAX_NUDGES
    assert db.get_item(UID, i)["nudge_count"] == db.DUE_MAX_NUDGES


def test_due_nudge_message_and_buttons():
    i = add(title="Send deck to Rahul", due=NOW - H)
    [(uid, item_id, reply)] = core.due_nudges(NOW)
    assert (uid, item_id) == (UID, i)
    assert reply.text == f"⏰ Reminder: Send deck to Rahul (#{i})"
    db.mark_nudged(i, UID, NOW)
    [(_, _, repeat)] = core.due_nudges(NOW + 6 * H)
    assert repeat.text.startswith(f"⏰ Still pending: Send deck to Rahul (#{i}) — due Mon 02 Nov")
    assert reply.buttons == [("✅ Done", f"done:{i}"), ("😴 +1 day", f"snz:{i}")]


def test_done_and_archived_items_are_never_nudged():
    a, b = add(due=NOW - H), add(kind="idea", created=NOW - 10 * D)
    db.set_status(UID, a, "done")
    db.set_status(UID, b, "archived")
    assert run(NOW) == []


def test_notes_and_tasks_without_due_are_never_nudged():
    add(kind="note", created=NOW - 30 * D)
    add(kind="task", created=NOW - 30 * D)
    add(kind="promise", created=NOW - 30 * D)
    assert run(NOW) == []


# ---------- stale ideas ----------

def test_idea_nudged_after_3_days_then_every_3_days_max_3():
    i = add(kind="idea", title="YouTube segment", created=NOW)
    assert run(NOW + 2 * D) == []
    assert run(NOW + 3 * D) == [i]
    assert run(NOW + 5 * D) == []
    assert run(NOW + 6 * D) == [i]
    assert run(NOW + 9 * D) == [i]
    assert run(NOW + 12 * D) == []      # max 3
    assert run(NOW + 30 * D) == []


def test_stale_idea_message_offers_drop():
    i = add(kind="idea", title="YouTube segment", created=NOW - 4 * D)
    [(_, _, reply)] = core.due_nudges(NOW)
    assert reply.text == f"💡 You told me this 4 days ago: “YouTube segment” (#{i}). Still want it?"
    assert ("🗄 Drop", f"arc:{i}") in reply.buttons


def test_idea_with_future_due_date_waits_for_the_date():
    i = add(kind="idea", due=NOW + 10 * D, created=NOW - 5 * D)
    assert run(NOW) == []
    assert run(NOW + 10 * D) == [i]


# ---------- quiet hours ----------

def test_quiet_hours_boundaries():
    assert not core.in_quiet_hours(ist(2026, 11, 2, 8, 0))
    assert not core.in_quiet_hours(ist(2026, 11, 2, 21, 59))
    assert core.in_quiet_hours(ist(2026, 11, 2, 22, 0))
    assert core.in_quiet_hours(ist(2026, 11, 3, 0, 30))
    assert core.in_quiet_hours(ist(2026, 11, 3, 7, 59))


def test_reminder_fires_at_its_time_even_at_night():
    i = add(due=ist(2026, 11, 2, 23, 15))
    assert run(ist(2026, 11, 2, 23, 14)) == []
    assert run(ist(2026, 11, 2, 23, 15)) == [i]   # the time the user asked for


def test_repeats_and_idea_nudges_wait_for_morning():
    i = add(due=ist(2026, 11, 2, 21, 0))
    idea = add(kind="idea", created=ist(2026, 10, 25, 9, 0))
    assert run(ist(2026, 11, 2, 21, 0)) == [i, idea]   # first reminder + idea, daytime
    assert run(ist(2026, 11, 3, 3, 30)) == []          # repeat due, but quiet hours
    assert run(ist(2026, 11, 3, 7, 59)) == []
    assert run(ist(2026, 11, 3, 8, 0)) == [i]


# ---------- daily cap ----------

def test_reminders_the_user_set_are_never_capped():
    ids = [add(title=f"t{n}", due=NOW - (10 - n) * H) for n in range(7)]
    assert run(NOW) == ids                       # all 7 first reminders go out


def overdue_already_reminded(uid, n, when):
    """n items whose first reminder went out at `when`."""
    ids = []
    for k in range(n):
        i = add(uid, title=f"{uid}-{k}", due=when - (10 - k) * H)
        db.mark_nudged(i, uid, when)
        ids.append(i)
    return ids


def test_daily_cap_limits_repeat_nudges_to_5_per_24_hours():
    earlier = NOW - 30 * H                       # first reminders were sent yesterday
    ids = overdue_already_reminded(UID, 7, earlier)
    first = run(NOW)
    assert first == ids[:5]                      # oldest due first
    assert run(NOW + 7 * H) == []                # remaining 2 wait: cap reached
    later = run(NOW + 25 * H)
    assert later[:2] == ids[5:]                  # least-nudged items go first
    assert len(later) == 5


def test_daily_cap_is_per_user():
    earlier = NOW - 30 * H
    overdue_already_reminded(UID, 6, earlier)
    overdue_already_reminded(OTHER, 6, earlier)
    out = core.due_nudges(NOW)
    assert sum(1 for uid, _, _ in out if uid == UID) == 5
    assert sum(1 for uid, _, _ in out if uid == OTHER) == 5


def test_overdue_items_take_priority_over_stale_ideas_under_the_cap():
    ideas = [add(kind="idea", created=NOW - 10 * D) for _ in range(5)]
    [task] = overdue_already_reminded(UID, 1, NOW - 30 * H)
    picked = run(NOW)
    assert picked[0] == task and len(picked) == 5
    assert set(picked[1:]) < set(ideas)


def test_failed_send_is_not_counted():
    i = add(due=NOW - H)
    assert [x[1] for x in core.due_nudges(NOW)] == [i]   # not marked: pretend send failed
    assert [x[1] for x in core.due_nudges(NOW + H)] == [i]


# ---------- snooze ----------

def test_snooze_holds_nudges_for_a_day():
    i = add(due=NOW - H)
    assert run(NOW) == [i]
    assert db.snooze(UID, i, 24, now=NOW + 6 * H)
    assert run(NOW + 7 * H) == []
    assert run(NOW + 29 * H) == []          # 23h into snooze (and also quiet hours)
    t = NOW + 30 * H                        # snooze over; 18:00 IST next day
    assert not core.in_quiet_hours(t)
    assert run(t) == [i]


def test_snoozed_idea_is_not_nudged():
    i = add(kind="idea", created=NOW - 5 * D)
    db.snooze(UID, i, 24, now=NOW)
    assert run(NOW + H) == []
    assert run(NOW + 25 * H) == [i]
