"""Full-text search (SQLite FTS5): finding old items and messages, keeping the index in step,
and what the assistant is sent for each message."""
import sqlite3
from datetime import timedelta

import pytest

import core
import db
import llm
from conftest import NOW, FakeLLM, says

UID, OTHER = 111, 222


def add(title, text=None, uid=UID, kind="note", status="open", due=None, priority="normal", days_ago=0):
    i = db.add_item(uid, kind, title, text or title, due, now=NOW - timedelta(days=days_ago),
                    priority=priority)
    if status != "open":
        db.set_status(uid, i, status)
    return i


def ids(rows):
    return [r["id"] for r in rows]


# ---------- the index ----------

def test_fts5_is_available_here():
    assert db.HAS_FTS is True


def test_finds_items_by_title_or_original_words_best_match_first():
    a = add("Goa trip plan", "December mein Goa jana hai with Rahul")
    b = add("Rahul's birthday", "Rahul ka birthday 5 March ko hai")
    add("Wifi password", "it's in the drawer")
    assert ids(db.search_items(UID, "what did we say about Goa?")) == [a]
    found = db.search_items(UID, "Rahul Goa")
    assert ids(found)[0] == a and set(ids(found)) == {a, b}   # matching both words ranks first


def test_prefix_matching_finds_word_forms():
    i = add("Ask about birthdays", "plan the office birthdays")
    assert ids(db.search_items(UID, "birthday")) == [i]


def test_hinglish_and_english_filler_words_are_ignored():
    i = add("YouTube tech news segment", "YouTube pe tech news segment ka idea hai", kind="idea")
    add("Kya karna hai list", "random note about lists")
    assert db.search_terms("YouTube wala idea kya tha?") == ["youtube", "idea"]
    assert ids(db.search_items(UID, "YouTube wala idea kya tha?")) == [i]
    assert db.search_items(UID, "kya tha? what was it?") == []   # nothing meaningful to look for


def test_punctuation_and_quotes_cannot_break_the_query():
    i = add("C++ course", 'notes on "C++" and foo-bar AND OR NOT ( ) *')
    for q in ['C++ "', "foo-bar", "AND OR NOT", "(", '"*"', "NEAR(a b)"]:
        db.search_items(UID, q)  # must not raise
    assert ids(db.search_items(UID, "foo-bar")) == [i]


def test_devanagari_words_are_searchable():
    i = add("मम्मी का जन्मदिन", "मम्मी का जन्मदिन 12 मई")
    assert ids(db.search_items(UID, "जन्मदिन कब है?")) == [i]


def test_search_is_per_user():
    add("Goa trip", uid=OTHER)
    assert db.search_items(UID, "Goa") == []
    db.add_message(OTHER, "user", "Goa is lovely", now=NOW)
    assert db.search_messages(UID, "Goa") == []


def test_exclude_ids_and_limit():
    found = [add(f"Goa note {n}") for n in range(5)]
    assert len(db.search_items(UID, "Goa", k=3)) == 3
    assert set(ids(db.search_items(UID, "Goa", exclude_ids=found[:4]))) == {found[4]}


def test_messages_search_only_before_a_given_id_and_oldest_first():
    m1 = db.add_message(UID, "user", "Thinking about a Goa trip in December", now=NOW)
    db.add_message(UID, "pluto", "Sounds fun!", now=NOW)
    m3 = db.add_message(UID, "user", "Goa flights are expensive", now=NOW)
    db.add_message(UID, "user", "Goa again", now=NOW)
    assert ids(db.search_messages(UID, "Goa flights", before_id=m3 + 1)) == [m1, m3]


def test_delete_removes_the_item_and_its_messages_from_search():
    i = add("Pay rent to Sharma ji", "remind me to pay rent to Sharma ji")
    db.add_message(UID, "user", "remind me to pay rent to Sharma ji", now=NOW)
    assert db.search_items(UID, "Sharma") and db.search_messages(UID, "Sharma")
    core.cmd_delete(UID, str(i))
    assert db.search_items(UID, "Sharma") == [] and db.search_messages(UID, "Sharma") == []
    with db.conn() as c:  # the index itself holds nothing for it any more
        assert c.execute("SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'sharma'").fetchone()[0] == 0


def test_forget_removes_messages_from_search():
    db.add_message(UID, "user", "my secret Goa plan", now=NOW)
    core.cmd_forget(UID)
    assert db.search_messages(UID, "Goa") == []


def test_existing_database_is_indexed_on_upgrade(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    i = add("Goa trip plan")
    db.add_message(UID, "user", "Goa trip with Rahul", now=NOW)
    with db.conn() as c:  # pretend this database was made before search existed
        for t in ("items_fts", "messages_fts"):
            for trig in ("ai", "ad", "au"):
                c.execute(f"DROP TRIGGER {t}_{trig}")
            c.execute(f"DROP TABLE {t}")
    db.init()
    assert ids(db.search_items(UID, "Goa")) == [i]
    assert len(db.search_messages(UID, "Rahul")) == 1


def test_falls_back_to_like_without_fts5(monkeypatch):
    i = add("Goa trip plan", "December mein Goa")
    monkeypatch.setattr(db, "HAS_FTS", False)
    assert ids(db.search_items(UID, "goa")) == [i]
    db.add_message(UID, "user", "Goa trip with Rahul", now=NOW)
    assert len(db.search_messages(UID, "rahul")) == 1


def test_init_survives_sqlite_without_fts5(tmp_path, monkeypatch):
    real_connect = sqlite3.connect

    class NoFTS(sqlite3.Connection):
        def execute(self, sql, *a):
            if "fts5" in sql.lower():
                raise sqlite3.OperationalError("no such module: fts5")
            return super().execute(sql, *a)

    monkeypatch.setattr(sqlite3, "connect", lambda p: real_connect(p, factory=NoFTS))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "nofts.db"))
    db.init()
    assert db.HAS_FTS is False
    i = add("Goa trip")
    assert ids(db.search_items(UID, "Goa")) == [i]


# ---------- what the assistant is sent ----------

def test_old_closed_item_reaches_the_assistant_through_search():
    old = add("Goa trip plan", "December mein Goa jana hai", status="done", days_ago=300)
    for n in range(30):                       # newer closed items push it out of "recent"
        add(f"done thing {n}", status="done")
    fake = FakeLLM(says(f"You planned it in #{old}."))
    replies = core.chat(UID, "Goa ke baare mein kya discuss kiya tha?", fake, NOW)
    sent = [v["id"] for v in fake.calls[0]["items"]]
    assert old in sent
    assert replies[0].text == f"You planned it in #{old}."   # citing a real old item is fine


def test_old_messages_reach_the_assistant_as_related():
    db.add_message(UID, "user", "I want to start a podcast about AI tools", now=NOW - timedelta(days=60))
    for n in range(40):
        db.add_message(UID, "user", f"small talk {n}", now=NOW)
    fake = FakeLLM(says("You said you want to start a podcast."))
    core.chat(UID, "what did I say about the podcast?", fake, NOW)
    call = fake.calls[0]
    assert len(call["history"]) == core.HISTORY_TURNS
    assert [m["text"] for m in call["related"]] == ["I want to start a podcast about AI tools"]


def test_messages_already_in_the_recent_conversation_are_not_repeated():
    db.add_message(UID, "user", "podcast idea", now=NOW)
    fake = FakeLLM(says("ok"))
    core.chat(UID, "podcast?", fake, NOW)
    assert fake.calls[0]["related"] == []


def test_open_items_are_capped_by_urgency():
    for n in range(160):
        add(f"note {n}")
    urgent = add("Renew passport", kind="task", due=db.to_str(NOW + timedelta(days=1)))
    fake = FakeLLM(says("ok"))
    core.chat(UID, "hello", fake, NOW)
    sent = [v["id"] for v in fake.calls[0]["items"]]
    assert urgent in sent and len(sent) == 150


def test_citing_an_item_that_doesnt_exist_is_still_withheld():
    add("Goa trip plan")
    add("Someone else's thing", uid=OTHER)          # #2 belongs to another user
    replies = core.chat(UID, "Goa?", FakeLLM(says("See #1, #2 and #99")), NOW)
    assert replies[0].text == "I couldn't find that in what you've saved with me."


def test_prompt_includes_the_older_matching_messages():
    text = llm.format_context(
        [{"role": "user", "when": "Mon 2026-11-02 12:00", "text": "hi"}], [],
        [{"role": "user", "when": "Sat 2026-09-05 10:00", "text": "podcast about AI tools"}],
    )
    assert "Older messages that match the new message (found by search):" in text
    assert "[Sat 2026-09-05 10:00] User: podcast about AI tools" in text
    assert "Older messages" not in llm.format_context([], [], [])
