"""The Error log (things that went wrong, and requests Pluto can't do, shown in the sheet's
"Error log" tab) and asking before saving the same thing twice in one day."""
import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import bot
import core
import db
import llm
from conftest import NOW, FakeLLM, says

UID, OTHER = 111, 222


def issues(uid=UID):
    return [(r["kind"], r["request"], r["detail"]) for r in db.recent_issues(uid)]


def ask(text, result, now=NOW, **kw):
    fake = FakeLLM(result)
    return core.chat(UID, text, fake, now=now, **kw), fake


# ---------- error log ----------

def test_failed_assistant_is_logged_with_the_users_words():
    ask("kal dentist 4 baje", RuntimeError("all providers down"))
    [(kind, request, detail)] = issues()
    assert kind == "error" and request == "kal dentist 4 baje"
    assert "plain note" in detail and "all providers down" not in detail   # no raw error text


def test_a_request_pluto_cant_do_is_logged_and_still_answered():
    result = says("Main sheet mein tab nahi bana sakta, par reminder laga sakta hoon.")
    result["cant"] = "  Create an error-log worksheet in the sheet  "
    replies, _ = ask("sheet mein error log tab bana do", result)
    assert replies[0].text.startswith("Main sheet mein tab nahi")
    assert issues() == [("cant", "sheet mein error log tab bana do",
                         "Create an error-log worksheet in the sheet")]


def test_no_cant_means_no_log():
    for cant in (None, "", "   ", 5, ["x"]):
        result = says("ok")
        result["cant"] = cant
        ask("hello", result)
    assert issues() == []


def test_withheld_answer_is_logged():
    ask("what about #77?", says("#77 is your gym plan"))
    [(kind, _, detail)] = issues()
    assert kind == "error" and "held it back" in detail


def test_sheet_read_error_is_logged(monkeypatch):
    monkeypatch.setattr(core.sheet_reader, "reading_enabled", lambda: False)
    ask("total?", {"reply": "", "read": ["https://docs.google.com/spreadsheets/d/abc"]})
    [(kind, request, detail)] = issues()
    assert kind == "error" and request == "total?" and "can't open Google Sheets" in detail


def test_error_log_tab_in_the_sheet():
    tables, styles = core._sheet(UID, NOW)
    assert list(tables) == list(core.SHEET_TABS) and core.SHEET_TABS[-1] == "Error log"
    assert tables["Error log"] == [["When", "Type", "What you asked", "What happened"],
                                   ["Nothing has gone wrong so far.", "", "", ""]]
    assert styles["Error log"]["roles"] == ["header", "note"]

    core.log_issue(UID, "error", "=evil()", "AI down", now=NOW)
    core.log_issue(UID, "cant", "book a cab", "Book a cab", now=NOW + timedelta(minutes=5))
    core.log_issue(OTHER, "error", "not mine", "x", now=NOW)
    tables, styles = core._sheet(UID, NOW)
    rows = tables["Error log"]
    assert rows[1] == ["2026-11-02 12:05", "🚫 Can't do yet", "book a cab", "Book a cab"]  # newest first
    assert rows[2] == ["2026-11-02 12:00", "⚠️ Error", "=evil()", "AI down"]
    assert len(rows) == 3
    assert styles["Error log"]["roles"] == ["header", "urgent", "alert"]
    assert all(r in core.SHEET_ROLES for r in styles["Error log"]["roles"])


def test_log_issue_trims_and_never_breaks(monkeypatch):
    core.log_issue(UID, "weird", "x" * 900, "y" * 900, now=NOW)
    [(kind, request, detail)] = issues()
    assert kind == "error" and len(request) == core.ISSUE_REQUEST_MAX
    assert len(detail) == core.ISSUE_DETAIL_MAX

    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "add_issue", broken)
    core.log_issue(UID, "error", "a", "b")          # no exception


def test_error_log_keeps_the_newest(monkeypatch):
    monkeypatch.setattr(db, "ISSUES_KEEP", 3)
    for i in range(5):
        db.add_issue(UID, "error", f"r{i}", "d", now=NOW)
    assert [r["request"] for r in db.recent_issues(UID)] == ["r4", "r3", "r2"]


def test_forget_and_delete_clear_the_log():
    core.chat(UID, "call the bank", FakeLLM(says("", save=[("task", "Call the bank", None)])),
              now=NOW)
    item = db.recall_pool(UID)[0]["id"]
    core.log_issue(UID, "cant", "call the bank", "Call someone", now=NOW)
    core.log_issue(UID, "error", "something else", "x", now=NOW)
    core.cmd_delete(UID, str(item))
    assert [r for _, r, _ in issues()] == ["something else"]
    core.cmd_forget(UID)
    assert issues() == []


def test_export_includes_the_log():
    core.log_issue(UID, "cant", "book a cab", "Book a cab", now=NOW)
    data = json.loads(core.export_json(UID))
    assert data["error_log"][0]["request"] == "book a cab"


def test_sheet_sync_failure_is_logged_once_per_outage():
    def broken(tables, styles):
        raise RuntimeError("401 https://script.google.com/macros/s/SECRETID/exec")

    for minute in (0, 5, 10):
        try:
            core.sync_sheet(UID, broken, now=NOW + timedelta(minutes=minute))
        except RuntimeError:
            pass
    logged = [d for k, r, d in issues() if "Google Sheet" in d]
    assert len(logged) == 1 and "SECRETID" not in logged[0]
    core.sync_sheet(UID, lambda t, s: "", now=NOW + timedelta(minutes=15))   # fixed
    try:
        core.sync_sheet(UID, broken, force=True, now=NOW + timedelta(minutes=20))
    except RuntimeError:
        pass
    assert len([d for k, r, d in issues() if "Google Sheet" in d]) == 2


def test_voice_failure_is_logged(monkeypatch):
    from test_bot import FakeVoice, ctx, go, update

    monkeypatch.setattr(bot, "ALLOWED", str(UID))

    async def broken(audio, filename, mime):
        raise bot.stt.TranscriptionFailed("all down")

    monkeypatch.setattr(bot.stt, "transcribe", broken)
    monkeypatch.setattr(bot.stt, "providers", lambda: ["groq"])
    go(bot.on_voice(update(uid=UID, voice=FakeVoice()), ctx()))
    [(kind, request, detail)] = issues()
    assert kind == "error" and request == "(voice note)" and "transcription" in detail


def test_apps_script_accepts_the_error_log_tab():
    script = (Path(__file__).parent.parent / "apps_script" / "Code.gs").read_text()
    line = next(x for x in script.splitlines() if x.startswith("const TABS"))
    assert all(f"'{t}'" in line for t in core.SHEET_TABS)


def test_prompt_explains_cant_and_repeats():
    assert '"cant"' in llm.SYSTEM and "THINGS YOU CAN'T DO" in llm.SYSTEM
    assert "Save it again?" in llm.SYSTEM


# ---------- asking before saving the same thing twice in a day ----------

BANK = says("Pakka, 5 baje yaad dila dunga.", save=[("task", "Call the bank", "2026-11-02T17:00")])


def test_similar_words_match():
    db.add_item(UID, "task", "Call the bank", "remind me to call the bank", now=NOW)
    later = NOW + timedelta(hours=1)
    assert core.similar_today(UID, "call bank at 5", later)["title"] == "Call the bank"
    assert core.similar_today(UID, "Call Mom", later) is None
    assert core.similar_today(UID, "Call the bank", later, kind="note") is None   # a note isn't a repeat
    assert core.similar_today(OTHER, "Call the bank", later) is None
    assert core.similar_today(UID, "the", later) is None                          # only filler words
    tomorrow = NOW + timedelta(days=1)
    assert core.similar_today(UID, "Call the bank", tomorrow) is None             # a new day


def test_local_day_not_utc_day():
    # 23:00 in India on 1 Nov is 17:30 UTC; 23:30 is 18:00 UTC (same day); 00:30 on 2 Nov is
    # 19:00 UTC (next day), though all three are 1 Nov in UTC.
    db.add_item(UID, "task", "Gym", "gym", now=NOW.replace(day=1, hour=17, minute=30))
    assert core.similar_today(UID, "Gym", NOW.replace(day=1, hour=18, minute=0)) is not None
    assert core.similar_today(UID, "Gym", NOW.replace(day=1, hour=19, minute=0)) is None


def test_same_order_twice_in_a_day_asks_first():
    first, _ = ask("remind me to call the bank at 5pm", BANK)
    assert first[-1].text.startswith("Added")
    replies, _ = ask("call the bank at 5 yaad dilana", BANK, now=NOW + timedelta(hours=2))
    assert len(db.recall_pool(UID)) == 1                       # nothing new saved yet
    [question] = replies                                       # no "Pakka" for an unsaved item
    assert question.text.startswith("🔁 You already asked me for this today:\n✅ #1 Call the bank")
    assert question.text.endswith("Save it again?")
    assert question.buttons == core.REPEAT_BUTTONS
    # The assistant sees a plain account, not the question itself (a model copies it otherwise).
    assert db.recent_messages(UID)[-1]["text"] == (
        "(Pluto's app asked whether to save “Call the bank” again, as it was already saved today "
        "as #1. Nothing new was saved yet.)")


def test_save_again_button_saves_the_held_item():
    ask("remind me to call the bank at 5pm", BANK)
    ask("call the bank at 5 yaad dilana", BANK, now=NOW + timedelta(hours=2))
    reply = core.button(UID, "rep:add", now=NOW + timedelta(hours=2, minutes=1))
    assert reply.text.startswith("Added ✅ #2 Call the bank")
    two = db.get_item(UID, 2)
    assert two["original_text"] == "call the bank at 5 yaad dilana" and two["due_at"]
    assert reply.buttons == core.item_buttons(2)
    assert "expired" in core.button(UID, "rep:add", now=NOW + timedelta(hours=3)).text   # only once


def test_skip_button_saves_nothing():
    ask("remind me to call the bank at 5pm", BANK)
    ask("call the bank at 5 yaad dilana", BANK, now=NOW + timedelta(hours=2))
    assert core.button(UID, "rep:skip", now=NOW + timedelta(hours=2)).text == "Okay, I didn't save it again."
    assert len(db.recall_pool(UID)) == 1


def test_plain_yes_or_no_is_answered_without_the_ai():
    ask("remind me to call the bank at 5pm", BANK)
    ask("call the bank at 5 yaad dilana", BANK, now=NOW + timedelta(hours=2))
    replies, fake = ask("Haan!", RuntimeError("the AI must not be asked"),
                        now=NOW + timedelta(hours=2, minutes=1))
    assert fake.calls == [] and replies[0].text.startswith("Added ✅ #2 Call the bank")
    assert db.get_item(UID, 2)["original_text"] == "call the bank at 5 yaad dilana"
    assert db.get_item(UID, 2)["due_at"] == db.get_item(UID, 1)["due_at"]
    assert issues() == []

    ask("pay rent", says("", save=[("task", "Pay rent", None)]), now=NOW + timedelta(hours=3))
    ask("pay rent yaad dilana", says("", save=[("task", "Pay rent", None)]), now=NOW + timedelta(hours=3))
    replies, fake = ask("nahi rehne do", RuntimeError("no AI"), now=NOW + timedelta(hours=3))
    assert replies[0].text == "Okay, I didn't save it again." and fake.calls == []
    assert len(db.recall_pool(UID)) == 3


def test_yes_or_no_words():
    for yes in ("yes", "Haan", "ok.", "save it again", "ji haan", "theek hai 👍"):
        assert core._yes_or_no(yes) == "add", yes
    for no in ("no", "Nahi", "skip", "rehne do!", "no thanks"):
        assert core._yes_or_no(no) == "skip", no
    for other in ("haan but at 6pm", "yes and also buy milk", "what?"):
        assert core._yes_or_no(other) is None, other
    assert core._yes_or_no("haan") is not None


def test_plain_yes_with_no_question_goes_to_the_ai():
    replies, fake = ask("haan", says("Kis baare mein?"))
    assert len(fake.calls) == 1 and replies[0].text == "Kis baare mein?"


def test_a_longer_yes_goes_to_the_ai_and_keeps_the_first_words():
    ask("remind me to call the bank at 5pm", BANK)
    ask("call the bank at 5 yaad dilana", BANK, now=NOW + timedelta(hours=2))
    yes = says("Theek hai.", save=[("task", "Call the bank", None, {"uses_previous_message": True})])
    replies, fake = ask("haan, wahi wala dobara save karo", yes, now=NOW + timedelta(hours=2, minutes=1))
    assert len(fake.calls) == 1
    two = db.get_item(UID, 2)
    assert two is not None and replies[-1].text.startswith("Added ✅ #2")
    assert two["original_text"] == "call the bank at 5 yaad dilana\nhaan, wahi wala dobara save karo"
    assert two["due_at"] == db.get_item(UID, 1)["due_at"]       # the time from the first ask


def test_the_question_lasts_one_message():
    ask("remind me to call the bank at 5pm", BANK)
    ask("call the bank at 5 yaad dilana", BANK, now=NOW + timedelta(hours=2))
    ask("aaj mausam kaisa hai", says("Pata nahi 🙂"), now=NOW + timedelta(hours=2, minutes=1))
    assert "expired" in core.button(UID, "rep:add", now=NOW + timedelta(hours=2, minutes=2)).text
    assert len(db.recall_pool(UID)) == 1


def test_a_third_ask_after_the_question_saves_it():
    ask("remind me to call the bank at 5pm", BANK)
    ask("call the bank at 5 yaad dilana", BANK, now=NOW + timedelta(hours=2))
    replies, _ = ask("call the bank at 5 yaad dilana", BANK, now=NOW + timedelta(hours=2, minutes=1))
    assert len(db.recall_pool(UID)) == 2 and replies[-1].text.startswith("Added")


def test_different_things_and_other_days_save_straight_away():
    ask("remind me to call the bank at 5pm", BANK)
    replies, _ = ask("buy milk", says("", save=[("task", "Buy milk", None)]), now=NOW + timedelta(hours=1))
    assert replies[-1].text.startswith("Added ✅ #2 Buy milk")
    replies, _ = ask("call the bank at 5", BANK, now=NOW + timedelta(days=1))
    assert replies[-1].text.startswith("Added ✅ #3")


def test_one_repeat_and_one_new_item_in_a_message():
    ask("remind me to call the bank at 5pm", BANK)
    both = says("Done 👍", save=[("task", "Call the bank", None), ("task", "Pay rent", None)])
    replies, _ = ask("call the bank and pay rent", both, now=NOW + timedelta(hours=1))
    assert [r.text.split("\n")[0] for r in replies] == [
        "Done 👍", "Added ✅ #2 Pay rent", "🔁 You already asked me for this today:"]


def test_save_again_button_is_wired_up(monkeypatch):
    from test_bot import ctx, go, update

    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    ask("remind me to call the bank at 5pm", BANK)
    ask("call the bank at 5 yaad dilana", BANK, now=NOW + timedelta(hours=2))
    monkeypatch.setattr(db, "now_utc", lambda: NOW + timedelta(hours=2, minutes=1))
    u = update(uid=UID, data="rep:add")
    go(bot.on_button(u, ctx()))
    [(text, markup)] = u.callback_query.message.replies
    assert text.startswith("Added ✅ #2") and markup is not None   # with Done / Snooze buttons
    assert u.callback_query.markup_cleared
