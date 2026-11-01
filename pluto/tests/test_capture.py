"""Capture, labelling, raw-note fallback and recall (core.capture / core.recall)."""
from datetime import timedelta

import core
import db
from conftest import NOW, FakeLLM, labels

UID, OTHER = 111, 222


def test_labelling_splits_message_into_items():
    text = "kal shaam 6 baje Rahul ko deck bhejna hai, aur YouTube tech news ka idea hai"
    llm = FakeLLM(labels(
        ("promise", "Rahul ko deck bhejna", "2026-11-03T18:00"),
        ("idea", "YouTube tech news segment", None),
    ))
    replies = core.capture(UID, text, llm, NOW)

    rows = db.recall_pool(UID)[::-1]
    assert [(r["kind"], r["title"]) for r in rows] == [
        ("promise", "Rahul ko deck bhejna"),
        ("idea", "YouTube tech news segment"),
    ]
    # The original message is stored untouched on every item.
    assert all(r["original_text"] == text for r in rows)
    # 18:00 in India is 12:30 UTC.
    assert rows[0]["due_at"] == "2026-11-03T12:30:00Z"
    assert rows[1]["due_at"] is None
    assert len(replies) == 2
    assert replies[0].text.startswith("Saved. 🤝 #1 Rahul ko deck bhejna — due Tue 03 Nov, 18:00")
    # Follow-up buttons on promises/tasks/anything with a due date, not on a plain idea.
    assert replies[0].buttons == [("✅ Done", "done:1"), ("😴 +1 day", "snz:1")]
    assert replies[1].buttons == []
    # The labeller is told the local time.
    assert llm.extract_calls[0][1].utcoffset() == timedelta(hours=5, minutes=30)


def test_original_text_is_kept_even_when_title_differs():
    text = "  remind me to   call mom  "
    core.capture(UID, text, FakeLLM(labels(("task", "Call mom", None))), NOW)
    row = db.recall_pool(UID)[0]
    assert row["title"] == "Call mom"
    assert row["original_text"] == text.strip()


def test_bad_kind_and_bad_due_are_cleaned_up():
    llm = FakeLLM(labels(("reminder", "x" * 300, "next tuesday-ish")))
    core.capture(UID, "something", llm, NOW)
    row = db.recall_pool(UID)[0]
    assert row["kind"] == "note"
    assert len(row["title"]) == core.TITLE_MAX
    assert row["due_at"] is None


def test_empty_label_result_saves_a_note():
    core.capture(UID, "wifi password is in the drawer", FakeLLM(labels()), NOW)
    row = db.recall_pool(UID)[0]
    assert (row["kind"], row["title"]) == ("note", "wifi password is in the drawer")


def test_labelling_failure_saves_raw_note():
    text = "buy milk and call the plumber"
    replies = core.capture(UID, text, FakeLLM(RuntimeError("API down")), NOW)
    rows = db.recall_pool(UID)
    assert len(rows) == 1
    assert rows[0]["kind"] == "note"
    assert rows[0]["original_text"] == text
    assert "plain note (#1)" in replies[0].text


def test_malformed_label_result_saves_raw_note():
    for bad in (["not", "a", "dict"], {"is_question": False, "items": "oops"}, None):
        core.capture(UID, "keep me", FakeLLM(lambda t, bad=bad: bad), NOW)
    assert [r["original_text"] for r in db.recall_pool(UID)] == ["keep me"] * 3


def test_empty_message_saves_nothing():
    llm = FakeLLM()
    assert core.capture(UID, "   ", llm, NOW)[0].text.startswith("Tell me")
    assert db.recall_pool(UID) == [] and llm.extract_calls == []


def test_question_goes_to_recall_and_saves_nothing():
    core.capture(UID, "YouTube idea", FakeLLM(labels(("idea", "YouTube tech news", None))), NOW)
    llm = FakeLLM(labels(question=True), answer="Woh #1 tha: YouTube tech news.")
    replies = core.capture(UID, "YouTube wala idea kya tha?", llm, NOW)

    assert [r.text for r in replies] == ["Woh #1 tha: YouTube tech news."]
    assert len(db.recall_pool(UID)) == 1  # the question itself was not saved
    question, pool, _ = llm.answer_calls[0]
    assert question == "YouTube wala idea kya tha?"
    assert [r["id"] for r in pool] == [1]


def test_recall_only_sees_own_items():
    core.capture(OTHER, "secret plan", FakeLLM(labels(("note", "secret plan", None))), NOW)
    core.capture(UID, "my plan", FakeLLM(labels(("note", "my plan", None))), NOW)
    llm = FakeLLM(labels(question=True))
    core.capture(UID, "what plans?", llm, NOW)
    assert [r["title"] for r in llm.answer_calls[0][1]] == ["my plan"]


def test_recall_with_nothing_saved_does_not_ask_the_model():
    llm = FakeLLM(labels(question=True), answer="You told me about a trip to Goa! #7")
    reply = core.capture(UID, "what did I say about Goa?", llm, NOW)[0]
    assert llm.answer_calls == []
    assert "don't have anything saved" in reply.text


def test_recall_failure_gives_safe_message():
    core.capture(UID, "note", FakeLLM(labels(("note", "n", None))), NOW)
    llm = FakeLLM(labels(question=True), answer=RuntimeError("timeout"))
    reply = core.capture(UID, "what was it?", llm, NOW)[0]
    assert "could not search" in reply.text
    assert len(db.recall_pool(UID)) == 1


def test_long_answer_is_cut_to_telegram_size():
    core.capture(UID, "note", FakeLLM(labels(("note", "n", None))), NOW)
    llm = FakeLLM(labels(question=True), answer="a" * 10000)
    assert len(core.capture(UID, "?", llm, NOW)[0].text) <= core.MAX_REPLY
