"""Shared fixtures: a fresh SQLite file per test, a fixed clock, and a fake labeller."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLUTO_TZ", "Asia/Kolkata")

import db  # noqa: E402

# Monday 2 Nov 2026, 12:00 in India (06:30 UTC) - well outside quiet hours.
NOW = datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init()


class FakeLLM:
    """Stands in for llm.py. `result` may be a dict, a callable(message, history, items) -> dict,
    or an exception to raise. Records every call."""

    def __init__(self, result=None, choices=(("gemini", "Gemini (g-1)"), ("groq", "Groq (q-1)"))):
        self._result = result if result is not None else {"reply": "🙂"}
        self._choices = list(choices)
        self.calls = []

    def choices(self):
        return self._choices

    def respond(self, message, history, items, now_local, prefer=None, related=(), summary=""):
        self.calls.append({"message": message, "history": list(history), "items": list(items),
                           "now": now_local, "prefer": prefer, "related": list(related),
                           "summary": summary})
        r = self._result
        if isinstance(r, Exception):
            raise r
        return r(message, history, items) if callable(r) else r


def says(reply="", save=(), done=()):
    """Build an assistant result.

    save entries are (kind, title, due_local) or (kind, title, due_local, extra-dict)."""
    items = []
    for s in save:
        kind, title, due, *extra = s
        it = {"kind": kind, "title": title, "due_local": due}
        if extra:
            it.update(extra[0])
        items.append(it)
    return {"reply": reply, "save": items, "done": list(done)}
