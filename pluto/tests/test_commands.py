"""/list /done /snooze /delete /export, inline buttons, and user isolation."""
import json

import core
import db
from conftest import NOW

UID, OTHER = 111, 222


def add(uid, kind="task", title="t", due=None, text=None):
    return db.add_item(uid, kind, title, text or title, due, now=NOW)


def test_list_shows_open_items_due_first():
    a = add(UID, "note", "no due")
    b = add(UID, "task", "later", "2026-11-05T04:30:00Z")
    c = add(UID, "task", "sooner", "2026-11-03T04:30:00Z")
    d = add(UID, "task", "finished")
    db.set_status(UID, d, "done")
    lines = core.cmd_list(UID).text.splitlines()
    assert [ln.split()[1] for ln in lines] == [f"#{c}", f"#{b}", f"#{a}"]
    assert "due Tue 03 Nov, 10:00" in lines[0]


def test_list_empty():
    assert core.cmd_list(UID).text.startswith("Nothing open")


def test_done():
    i = add(UID)
    assert core.cmd_done(UID, f"#{i}").text == f"Done #{i} ✅"
    assert db.get_item(UID, i)["status"] == "done"
    assert core.cmd_list(UID).text.startswith("Nothing open")


def test_bad_arguments_show_usage():
    assert core.cmd_done(UID, None).text == "Usage: /done <id>"
    assert core.cmd_snooze(UID, "abc").text == "Usage: /snooze <id>"
    assert core.cmd_delete(UID, "").text == "Usage: /delete <id>"
    assert "can't find #99" in core.cmd_done(UID, "99").text


def test_list_puts_high_priority_first_among_undated():
    a = db.add_item(UID, "task", "normal one", "x", now=NOW)
    b = db.add_item(UID, "task", "urgent one", "x", now=NOW, priority="high")
    lines = core.cmd_list(UID).text.splitlines()
    assert lines == [f"✅ #{b} 🔴 urgent one", f"✅ #{a} normal one"]


def test_snooze_sets_a_day_and_only_for_open_items():
    i = add(UID)
    assert "Snoozed" in core.cmd_snooze(UID, str(i), NOW).text
    assert db.get_item(UID, i)["snoozed_until"] == "2026-11-03T06:30:00Z"
    db.set_status(UID, i, "done")
    assert "can't find an open item" in core.cmd_snooze(UID, str(i), NOW).text


def test_delete_removes_forever():
    i = add(UID, "task", "Pay rent", "2026-11-01T04:30:00Z", text="remind me to pay rent\nkal 10 baje")
    db.mark_nudged(i, UID, NOW)
    for role, text in [("user", "remind me to pay rent"), ("pluto", "Kab?"),
                       ("user", "kal 10 baje"), ("user", "unrelated")]:
        db.add_message(UID, role, text, now=NOW)
    assert core.cmd_delete(UID, str(i)).text == f"Deleted #{i} forever."

    assert db.get_item(UID, i) is None
    with db.conn() as c:  # item, its nudges and the messages it came from are gone
        assert c.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM nudges").fetchone()[0] == 0
    assert [m["text"] for m in db.recent_messages(UID)] == ["Kab?", "unrelated"]
    assert "can't find" in core.cmd_delete(UID, str(i)).text


def test_delete_also_removes_its_nudges_from_the_chat():
    i = add(UID, "task", "Barber", None, text="barber at 6.30")
    other = add(UID, "task", "Gym", None, text="gym")
    core.record_nudge(UID, i, core.Reply(f"⏰ Reminder: Barber (#{i})"), NOW)
    core.record_nudge(UID, other, core.Reply(f"⏰ Reminder: Gym (#{other})"), NOW)
    core.cmd_delete(UID, str(i))
    assert [m["text"] for m in db.recent_messages(UID)] == [f"⏰ Reminder: Gym (#{other})"]


def test_deleting_an_item_with_only_a_nudge_in_the_chat_drops_the_summary():
    i = add(UID, "task", "Barber", None, text="barber at 6.30")
    core.record_nudge(UID, i, core.Reply(f"⏰ Reminder: Barber (#{i})"), NOW)
    db.add_summary(UID, 1, 1, "Reminded about the barber.", now=NOW)
    core.cmd_delete(UID, str(i))
    assert db.latest_summary(UID) is None


def test_forget_clears_chat_but_keeps_items():
    i = add(UID)
    db.add_message(UID, "user", "hello", now=NOW)
    db.add_message(OTHER, "user", "theirs", now=NOW)
    assert core.cmd_forget(UID).text == (
        "Cleared our chat history (1 messages), its summary and the profile facts "
        "built from it. Your saved items are untouched.")
    assert db.recent_messages(UID) == []
    assert [m["text"] for m in db.recent_messages(OTHER)] == ["theirs"]
    assert db.get_item(UID, i) is not None


def test_export_has_items_and_conversation():
    a = add(UID, "idea", "Podcast", text="ek podcast start karna hai")
    b = add(UID, "task", "Done thing")
    db.set_status(UID, b, "done")
    add(OTHER, "note", "not mine")
    db.add_message(UID, "user", "ek podcast start karna hai", now=NOW)
    db.add_message(OTHER, "user", "not mine", now=NOW)
    data = json.loads(core.export_json(UID))
    assert [(r["id"], r["status"]) for r in data["items"]] == [(a, "open"), (b, "done")]
    assert data["items"][0]["original_text"] == "ek podcast start karna hai"
    assert [m["text"] for m in data["messages"]] == ["ek podcast start karna hai"]


def test_buttons():
    a, b, c = add(UID), add(UID), add(UID, "idea")
    assert core.button(UID, f"done:{a}", NOW).text == f"Done #{a} ✅"
    assert core.button(UID, f"snz:{b}", NOW).text.startswith(f"Snoozed #{b}")
    assert core.button(UID, f"arc:{c}", NOW).text == f"Dropped #{c} 🗄"
    assert db.get_item(UID, c)["status"] == "archived"
    assert core.button(UID, "done:999", NOW).text == "That item is no longer there."
    assert core.button(UID, "zap:1", NOW) is None
    assert core.button(UID, "done:abc", NOW) is None
    assert core.button(UID, None, NOW) is None


def test_users_cannot_touch_each_others_items():
    mine = add(UID, "task", "mine")
    theirs = add(OTHER, "task", "theirs")

    assert "can't find" in core.cmd_done(OTHER, str(mine)).text
    assert "can't find" in core.cmd_snooze(OTHER, str(mine), NOW).text
    assert "can't find" in core.cmd_delete(OTHER, str(mine)).text
    for data in (f"done:{mine}", f"snz:{mine}", f"arc:{mine}"):
        assert core.button(OTHER, data, NOW).text == "That item is no longer there."

    row = db.get_item(UID, mine)
    assert (row["status"], row["snoozed_until"]) == ("open", None)
    assert db.get_item(OTHER, mine) is None
    assert f"#{theirs}" not in core.cmd_list(UID).text
    assert f"#{mine}" not in core.cmd_list(OTHER).text
    assert [r["title"] for r in json.loads(core.export_json(OTHER))["items"]] == ["theirs"]
