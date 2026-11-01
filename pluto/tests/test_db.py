"""Database upgrades: a pluto.db from phase 1 keeps working."""
import sqlite3

import db


def test_old_database_gets_priority_and_messages(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute(
        """CREATE TABLE items(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
           kind TEXT NOT NULL, title TEXT NOT NULL, original_text TEXT NOT NULL,
           status TEXT NOT NULL DEFAULT 'open', due_at TEXT, created_at TEXT NOT NULL,
           last_nudged_at TEXT, nudge_count INTEGER NOT NULL DEFAULT 0, snoozed_until TEXT)"""
    )
    c.execute("INSERT INTO items(user_id,kind,title,original_text,created_at)"
              " VALUES (1,'task','old','old words','2026-10-01T00:00:00Z')")
    c.commit()
    c.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    db.init()  # running it twice is harmless
    [row] = db.list_open(1)
    assert (row["title"], row["original_text"], row["priority"]) == ("old", "old words", "normal")
    db.add_message(1, "user", "hi")
    assert [m["text"] for m in db.recent_messages(1)] == ["hi"]
