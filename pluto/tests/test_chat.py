"""The assistant flow (core.chat): what gets saved, follow-ups, agenda context,
marking done, the never-invent guard, and the raw-note fallback."""
from datetime import timedelta

import core
import db
from conftest import NOW, FakeLLM, says

UID, OTHER = 111, 222


def items(uid=UID):
    return db.recall_pool(uid)[::-1]  # oldest first


def chat(text, result, uid=UID, now=NOW):
    llm = result if isinstance(result, FakeLLM) else FakeLLM(result)
    return core.chat(uid, text, llm, now), llm


# ---------- conversation ----------

def test_small_talk_is_answered_and_not_saved():
    replies, llm = chat("hi Pluto!", says("Hi! Kya help karun?"))
    assert [r.text for r in replies] == ["Hi! Kya help karun?"]
    assert items() == []
    log = db.recent_messages(UID)
    assert [(m["role"], m["text"]) for m in log] == [("user", "hi Pluto!"), ("pluto", "Hi! Kya help karun?")]


def test_assistant_sees_recent_conversation_and_local_time():
    chat("hello", says("Hi!"))
    _, llm = chat("kaise ho?", says("Badhiya!"))
    call = llm.calls[0]
    assert call["message"] == "kaise ho?"
    assert [(m["role"], m["text"]) for m in call["history"]] == [("user", "hello"), ("pluto", "Hi!")]
    assert call["now"].utcoffset() == timedelta(hours=5, minutes=30)


def test_empty_reply_and_no_actions_still_answers():
    replies, _ = chat("hmm", says(""))
    assert [r.text for r in replies] == ["🙂"]


def test_long_reply_is_cut_to_telegram_size():
    replies, _ = chat("tell me everything", says("a" * 10000))
    assert len(replies[0].text) <= core.MAX_REPLY


# ---------- saving ----------

def test_information_is_noted_with_original_text():
    text = "Rahul ka birthday 5 March ko hai"
    replies, _ = chat(text, says("", [("note", "Rahul's birthday: 5 March", None)]))
    [row] = items()
    assert (row["kind"], row["title"], row["original_text"]) == ("note", "Rahul's birthday: 5 March", text)
    assert [r.text for r in replies] == ["Noted 📝 #1 Rahul's birthday: 5 March"]
    assert replies[0].buttons == []  # nothing to follow up


def test_reminder_is_added_with_due_time_and_buttons():
    text = "remind me kal shaam 6 baje Rahul ko deck bhejna"
    replies, _ = chat(text, says("Pakka 👍", [("task", "Rahul ko deck bhejna", "2026-11-03T18:00")]))
    [row] = items()
    assert row["due_at"] == "2026-11-03T12:30:00Z"  # 18:00 India = 12:30 UTC
    assert row["original_text"] == text
    assert replies[0].text == "Pakka 👍"
    assert replies[1].text == "Added ✅ #1 Rahul ko deck bhejna — due Tue 03 Nov, 18:00"
    assert replies[1].buttons == [("✅ Done", "done:1"), ("😴 +1 day", "snz:1")]


def test_several_things_in_one_message():
    chat("note the wifi password is in the drawer and remind me to pay rent tomorrow 10am",
         says("Done.", [("note", "Wifi password in the drawer", None),
                        ("task", "Pay rent", "2026-11-03T10:00")]))
    assert [(r["kind"], r["title"]) for r in items()] == [
        ("note", "Wifi password in the drawer"), ("task", "Pay rent")]


def test_priority_is_kept_and_shown():
    replies, _ = chat("todo: renew passport, urgent",
                      says("", [("task", "Renew passport", None, {"priority": "high"})]))
    assert items()[0]["priority"] == "high"
    assert replies[0].text == "Added ✅ #1 🔴 Renew passport"


def test_bad_kind_priority_title_and_due_are_cleaned_up():
    chat("something", says("", [("reminder", "x" * 300, "next tuesday-ish", {"priority": "MEGA"})]))
    row = items()[0]
    assert row["kind"] == "note" and row["priority"] == "normal"
    assert len(row["title"]) == core.TITLE_MAX and row["due_at"] is None


def test_follow_up_question_then_save_keeps_both_messages():
    replies, _ = chat("remind me to call Rahul", says("Kab yaad dilaun?"))
    assert [r.text for r in replies] == ["Kab yaad dilaun?"] and items() == []

    def answer(message, history, _items):
        assert history[-1] == {**history[-1], "role": "pluto", "text": "Kab yaad dilaun?"}
        return says("Pakka 👍", [("task", "Call Rahul", "2026-11-03T17:00",
                                  {"uses_previous_message": True})])

    chat("kal 5 baje", answer)
    [row] = items()
    assert row["title"] == "Call Rahul"
    assert row["original_text"] == "remind me to call Rahul\nkal 5 baje"


def test_offer_then_yes_adds_a_todo():
    chat("mujhe bank jana hai", says("Add it as a to-do?"))
    assert items() == []
    chat("haan", says("Done 👍", [("task", "Bank jana", None, {"uses_previous_message": True})]))
    assert items()[0]["original_text"] == "mujhe bank jana hai\nhaan"


# ---------- answering from saved items ----------

def test_items_reach_the_assistant_with_time_buckets():
    # Now is Monday 2 Nov 2026, 12:00 in India.
    due = {
        "overdue": "2026-11-02T04:00:00Z",      # 09:30 today, already past
        "today": "2026-11-02T12:30:00Z",        # 18:00 today
        "tomorrow": "2026-11-03T04:30:00Z",
        "this week": "2026-11-08T04:30:00Z",    # Sunday
        "this month": "2026-11-20T04:30:00Z",
        "later": "2026-12-05T04:30:00Z",
        "no date": None,
    }
    for name, d in due.items():
        db.add_item(UID, "task", name, name, d, now=NOW)
    done = db.add_item(UID, "task", "finished", "finished", now=NOW)
    db.set_status(UID, done, "done")
    db.add_item(OTHER, "task", "someone else's", "x", now=NOW)

    _, llm = chat("what's on my plate this week?", says("…"))
    views = llm.calls[0]["items"]
    assert {v["title"]: v["bucket"] for v in views} == {**{k: k for k in due}, "finished": "done"}
    today = next(v for v in views if v["title"] == "today")
    assert today["due_local"] == "Mon 2026-11-02 18:00"


def test_reply_citing_real_items_is_passed_through():
    db.add_item(UID, "idea", "YouTube tech news", "youtube idea", now=NOW)
    replies, _ = chat("YouTube wala idea kya tha?", says("Woh #1 tha: YouTube tech news."))
    assert replies[0].text == "Woh #1 tha: YouTube tech news."
    assert len(items()) == 1  # the question was not saved


def test_reply_citing_items_the_user_does_not_have_is_withheld():
    db.add_item(OTHER, "note", "their secret", "x", now=NOW)        # #1 belongs to someone else
    db.add_item(UID, "note", "mine", "mine", now=NOW)               # #2
    replies, _ = chat("what did I say about Goa?", says("You planned Goa in #1 and #7!"))
    assert replies[0].text == "I couldn't find that in what you've saved with me."


def test_assistant_only_sees_this_users_items_and_history():
    chat("my thing", says("ok"), uid=OTHER)
    db.add_item(OTHER, "note", "their note", "x", now=NOW)
    _, llm = chat("what do I have?", says("Nothing yet."))
    assert llm.calls[0]["items"] == [] and llm.calls[0]["history"] == []


# ---------- marking done from chat ----------

def test_done_from_chat_only_touches_own_open_items():
    mine = db.add_item(UID, "task", "Send deck", "send deck", now=NOW)
    theirs = db.add_item(OTHER, "task", "Theirs", "x", now=NOW)
    replies, _ = chat("Rahul ko deck bhej diya", says("Badhiya!", done=[mine, theirs, 999, "x"]))
    assert replies[0].text == f"Badhiya!\n✅ Marked #{mine} done."
    assert db.get_item(UID, mine)["status"] == "done"
    assert db.get_item(OTHER, theirs)["status"] == "open"


# ---------- failures ----------

def test_assistant_failure_saves_raw_note():
    text = "buy milk and call the plumber"
    replies, _ = chat(text, RuntimeError("Gemini down"))
    [row] = items()
    assert (row["kind"], row["original_text"]) == ("note", text)
    assert "plain note (#1)" in replies[0].text


def test_malformed_output_saves_raw_note():
    for bad in (["not", "a", "dict"], {"reply": 5}, {"save": "oops"}, {"done": "x"}, None):
        chat("keep me", FakeLLM(lambda m, h, i, bad=bad: bad))
    assert [r["original_text"] for r in items()] == ["keep me"] * 5


def test_empty_message_does_nothing():
    replies, llm = chat("   ", says("?"))
    assert llm.calls == [] and items() == [] and db.recent_messages(UID) == []
    assert replies[0].text.startswith("I'm listening")
