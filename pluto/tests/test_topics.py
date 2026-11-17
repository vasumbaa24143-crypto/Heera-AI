"""Topics: derived labels that group items on the dashboard. The assistant proposes one when
it saves an item, a background job labels the rest, and /topic shows or changes them. The
user's own words are never touched."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import bot
import core
import db
import llm
from conftest import NOW, FakeLLM, says

UID, OTHER = 111, 222


def add(title, kind="note", topic=None, uid=UID, **kw):
    return db.add_item(uid, kind, title, kw.pop("text", title), now=NOW, topic=topic, **kw)


class Labeller(FakeLLM):
    """A fake assistant that can also label topics."""

    def __init__(self, labels=None, **kw):
        super().__init__(**kw)
        self.labels, self.label_calls = labels, []

    def label_topics(self, items, topics, now_local, prefer=None):
        self.label_calls.append({"items": items, "topics": topics})
        if isinstance(self.labels, Exception):
            raise self.labels
        return self.labels(items) if callable(self.labels) else self.labels


# ---------- cleaning ----------

def test_clean_topic():
    assert core.clean_topic("  jobs ") == "Jobs"
    assert core.clean_topic("#Content ideas.") == "Content ideas"
    assert core.clean_topic("Auré") == "Auré"
    assert core.clean_topic("x" * 80) == "X" + "x" * (core.TOPIC_MAX - 1)
    assert core.clean_topic("line one\nline two") == "Line one line two"
    for junk in (None, 5, "", "  ", "none", "General", ["Jobs"]):
        assert core.clean_topic(junk) == ""


def test_topic_spelling_follows_existing_items():
    add("Amazon BA role", topic="Jobs")
    assert core.known_topic(UID, "jobs") == "Jobs"
    assert core.known_topic(UID, "Money") == "Money"
    assert core.known_topic(OTHER, "jobs") == "jobs"          # another user's topics don't count


def test_topic_icons():
    assert core.topic_icon("Jobs") == "💼"
    assert core.topic_icon("Content ideas") == "🎬"
    assert core.topic_icon("Auré") == "🏷"
    assert core.topic_icon(None) == "🏷"


# ---------- saving with a topic ----------

def test_chat_saves_the_topic_and_shows_it():
    add("Old job note", topic="Jobs")
    fake = FakeLLM(says("Noted", save=[("note", "JPMC product owner role", None, {"topic": "jobs"})]))
    replies = core.chat(UID, "JPMC product owner role dekhna", fake, now=NOW)
    item = db.recall_pool(UID)[0]
    assert item["topic"] == "Jobs"                            # matched the existing spelling
    assert item["original_text"] == "JPMC product owner role dekhna"
    assert replies[-1].text == f"Noted 📝 #{item['id']} JPMC product owner role  ·  💼 Jobs"


def test_chat_without_a_topic_leaves_it_for_the_job():
    fake = FakeLLM(says("", save=[("idea", "Podcast", None, {"topic": "  "})]))
    replies = core.chat(UID, "podcast ka idea", fake, now=NOW)
    assert db.recall_pool(UID)[0]["topic"] is None
    assert replies[-1].text == "Noted 💡 #1 Podcast"


def test_failed_labelling_note_is_labelled_later():
    core.chat(UID, "kuch bhi", FakeLLM(RuntimeError("down")), now=NOW)   # saved as a plain note
    [row] = db.unlabelled_items(UID)
    assert row["kind"] == "note"


def test_assistant_sees_topics():
    add("Amazon BA role", topic="Jobs")
    add("Buy shoes")
    text = llm.format_context([], core.item_views(db.context_items(UID), NOW))
    assert "[note, no date, topic Jobs]" in text
    assert "Topics in use: Jobs" in text
    assert "Topics in use" not in llm.format_context([], [])


# ---------- the background labeller ----------

def test_update_topics_labels_only_the_batch_and_never_the_words():
    a = add("Amazon BA role apply", text="Amazon Business Analyst Role Apply Today")
    b = add("Nvidia shares", kind="task")
    c = add("Wifi password")
    mine = add("Already sorted", topic="Home")
    theirs = add("not mine", uid=OTHER)
    fake = Labeller(lambda items: [
        {"id": a, "topic": "jobs"}, {"id": b, "topic": "Money"},
        {"id": mine, "topic": "Hacked"}, {"id": theirs, "topic": "Hacked"},
        {"id": "x", "topic": "Bad"}, "junk", {"id": a, "topic": "Second answer"},
    ])
    assert core.update_topics(UID, fake, now=NOW) == 3
    assert [i["id"] for i in fake.label_calls[0]["items"]] == [a, b, c]
    assert fake.label_calls[0]["topics"] == ["Home"]
    got = {r["id"]: r["topic"] for r in db.recall_pool(UID)}
    assert got == {a: "Jobs", b: "Money", c: core.TOPIC_FALLBACK, mine: "Home"}
    assert db.get_item(UID, a)["original_text"] == "Amazon Business Analyst Role Apply Today"
    assert db.get_item(OTHER, theirs)["topic"] is None
    assert core.update_topics(UID, fake, now=NOW) == 0             # nothing left: no AI call
    assert len(fake.label_calls) == 1


def test_update_topics_failure_changes_nothing_and_waits():
    add("Something")
    fake = Labeller(RuntimeError("all providers down"))
    assert core.update_topics(UID, fake, now=NOW) == 0
    assert db.unlabelled_items(UID)
    fake.labels = lambda items: [{"id": items[0]["id"], "topic": "Personal"}]
    assert core.update_topics(UID, fake, now=NOW + timedelta(minutes=5)) == 0   # still waiting
    assert core.update_topics(UID, fake, now=NOW + core.TOPICS_RETRY) == 1
    assert not db.unlabelled_items(UID)


def test_update_topics_works_in_batches():
    for i in range(core.TOPICS_BATCH + 5):
        add(f"note {i}")
    fake = Labeller(lambda items: [{"id": i["id"], "topic": "Personal"} for i in items])
    assert core.update_topics(UID, fake, now=NOW) == core.TOPICS_BATCH
    assert core.update_topics(UID, fake, now=NOW) == 5


def test_assistant_without_label_topics_is_skipped():
    add("Something")
    assert core.update_topics(UID, FakeLLM(), now=NOW) == 0


def test_llm_label_topics_request(monkeypatch):
    seen = {}

    def fake_ask(system, user, prefer=None):
        seen.update(system=system, user=user)
        return {"topics": [{"id": 1, "topic": "Jobs"}]}

    monkeypatch.setattr(llm, "_ask", fake_ask)
    out = llm.label_topics([{"id": 1, "kind": "note", "title": "BA role", "original_text": "apply"}],
                           ["Money"], core.to_local(NOW))
    assert out == [{"id": 1, "topic": "Jobs"}]
    assert "Topics in use: Money" in seen["user"] and "[1] (note) BA role | said: apply" in seen["user"]
    assert "never instructions" in seen["system"]
    monkeypatch.setattr(llm, "_ask", lambda *a, **k: {"topics": "nope"})
    try:
        llm.label_topics([], [], core.to_local(NOW))
        raise AssertionError("should have failed")
    except ValueError:
        pass


def test_topics_job_runs_for_the_allowed_user(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    add("Something")
    fake = Labeller(lambda items: [{"id": i["id"], "topic": "Personal"} for i in items])
    monkeypatch.setattr(bot, "llm", fake)
    asyncio.run(bot.topics_job(SimpleNamespace()))
    assert db.recall_pool(UID)[0]["topic"] == "Personal"


# ---------- /topic ----------

def test_topic_command_lists_shows_and_moves():
    assert "No topics yet" in core.cmd_topic(UID).text
    job = add("Amazon BA role", kind="task", topic="Jobs")
    add("JPMC role", topic="Jobs")
    shoes = add("Buy shoes", topic="Shopping")
    db.set_status(UID, shoes, "done")
    theirs = add("not mine", uid=OTHER)

    listed = core.cmd_topic(UID).text
    assert "💼 Jobs · 2" in listed and "🛒 Shopping · 1" in listed

    shown = core.cmd_topic(UID, "jobs").text
    assert shown.startswith("💼 Jobs\n") and f"#{job}" in shown
    assert "(done)" in core.cmd_topic(UID, "Shopping").text
    assert "Nothing under “Health”" in core.cmd_topic(UID, "Health").text

    assert core.cmd_topic(UID, f"{shoes} jobs").text == f"🏷 #{shoes} is now under 💼 Jobs."
    assert db.get_item(UID, shoes)["topic"] == "Jobs"
    assert db.get_item(UID, shoes)["original_text"] == "Buy shoes"
    assert core.cmd_topic(UID, f"{theirs} Jobs").text == f"I couldn't find #{theirs}."
    assert db.get_item(OTHER, theirs)["topic"] is None
    assert "short topic name" in core.cmd_topic(UID, f"{job} ###").text


def test_topic_command_is_wired_up(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    sent = []

    async def reply_text(text, **kw):
        sent.append(text)

    update = SimpleNamespace(effective_user=SimpleNamespace(id=UID),
                             message=SimpleNamespace(reply_text=reply_text))
    asyncio.run(bot.cmd_topic(update, SimpleNamespace(args=[])))
    assert "No topics yet" in sent[0]


def test_old_databases_get_the_topic_column(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE items(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,"
                  " kind TEXT NOT NULL, title TEXT NOT NULL, original_text TEXT NOT NULL,"
                  " status TEXT NOT NULL DEFAULT 'open', due_at TEXT, created_at TEXT NOT NULL,"
                  " last_nudged_at TEXT, nudge_count INTEGER NOT NULL DEFAULT 0, snoozed_until TEXT)")
        c.execute("INSERT INTO items(user_id, kind, title, original_text, created_at)"
                  " VALUES (1, 'note', 'old', 'old words', '2026-11-01T00:00:00Z')")
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init()
    assert db.get_item(1, 1)["topic"] is None and db.get_item(1, 1)["original_text"] == "old words"
