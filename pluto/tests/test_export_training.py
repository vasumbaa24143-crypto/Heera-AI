"""export_training turns the stored conversation into chat-style JSONL without changing the database."""
import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

import db
import export_training as ex

UID = 7
T0 = datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)


def say(role, text, minutes=0, uid=UID):
    db.add_message(uid, role, text, now=T0 + timedelta(minutes=minutes))


def chats(**kw):
    return ex.sessions(ex.read_messages(db.DB_PATH), **kw)


def test_turns_become_alternating_user_and_assistant_messages():
    say("user", "kal 5 baje Rahul ko call karna hai")
    say("pluto", "Added #1 call Rahul", 1)
    assert chats() == [{
        "started_at": db.to_str(T0),
        "messages": [
            {"role": "user", "content": "kal 5 baje Rahul ko call karna hai"},
            {"role": "assistant", "content": "Added #1 call Rahul"},
        ],
    }]


def test_a_long_silence_starts_a_new_chat():
    say("user", "morning")
    say("pluto", "hi", 1)
    say("user", "evening", 60 * 7)
    say("pluto", "hello", 60 * 7 + 1)
    assert [len(c["messages"]) for c in chats()] == [2, 2]
    assert len(chats(gap_hours=8)) == 1


def test_consecutive_messages_from_one_side_are_merged():
    say("user", "one")
    say("user", "two", 1)
    say("pluto", "ok", 2)
    assert chats()[0]["messages"][0] == {"role": "user", "content": "one\ntwo"}


def test_api_keys_and_bot_tokens_are_redacted():
    google, groq, telegram = "AIza" + "a" * 35, "gsk_" + "b" * 30, "123456789:" + "c" * 35
    say("user", f"my keys {google} {groq} and {telegram}, thanks")
    out = json.dumps(chats())
    assert not any(k in out for k in (google, groq, telegram))
    assert out.count("[REDACTED_KEY]") == 3
    assert "thanks" in out


def test_forgotten_and_deleted_messages_are_not_exported(tmp_path):
    secret = "my passport number ends 4821"
    item = db.add_item(UID, "note", "Passport", secret, now=T0)
    say("user", secret)
    say("pluto", "Noted", 1)
    say("user", "keep this one", 2)
    db.delete_item(UID, item)
    out = tmp_path / "chats.jsonl"
    ex.export(db.DB_PATH, out)
    assert secret not in out.read_text() and "keep this one" in out.read_text()

    db.forget_messages(UID)
    assert ex.export(db.DB_PATH, out) == (0, 0)
    assert out.read_text() == ""


def test_file_is_private_utf8_json_lines(tmp_path):
    say("user", "kal subah गाना sunna hai")
    say("pluto", "Noted", 1)
    out = tmp_path / "sub" / "chats.jsonl"
    assert ex.export(db.DB_PATH, out) == (2, 1)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert "गाना" in out.read_text(encoding="utf-8")
    rec = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert rec["session"] == 1 and rec["messages"][1]["role"] == "assistant"


def test_export_never_changes_the_database(tmp_path):
    say("user", "hello")
    before = open(db.DB_PATH, "rb").read()
    ex.export(db.DB_PATH, tmp_path / "chats.jsonl")
    assert open(db.DB_PATH, "rb").read() == before


def test_dry_run_writes_nothing_and_prints_counts_only(tmp_path, capsys):
    say("user", "private words")
    say("pluto", "ok", 1)
    out = tmp_path / "chats.jsonl"
    ex.main(["--db", db.DB_PATH, "--out", str(out), "--dry-run"])
    printed = capsys.readouterr().out
    assert "2 turns in 1 chats" in printed and "private words" not in printed
    assert not out.exists()


def test_unreadable_database_is_a_clear_error(tmp_path):
    with pytest.raises(SystemExit) as e:
        ex.main(["--db", str(tmp_path / "missing.db"), "--dry-run"])
    assert "Could not read" in str(e.value)
