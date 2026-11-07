"""Shared fixtures: a fresh SQLite file per test, a fixed clock, and a fake labeller."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLUTO_TZ", "Asia/Kolkata")

import db  # noqa: E402
import sheet_reader  # noqa: E402

REAL_TOKEN = sheet_reader._token  # kept so one test can exercise it; every other test is blocked from it

# Monday 2 Nov 2026, 12:00 in India (06:30 UTC) - well outside quiet hours.
NOW = datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init()


@pytest.fixture(autouse=True)
def no_google(monkeypatch):
    """No test sees the developer's real Google settings or can sign in to Google."""
    for k in ("GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_SHEETS_ID",
              "GOOGLE_APPS_SCRIPT_URL", "GOOGLE_APPS_SCRIPT_SECRET"):
        monkeypatch.delenv(k, raising=False)

    def no_sign_in():
        raise AssertionError("a test tried to sign in to Google")

    monkeypatch.setattr(sheet_reader, "_token", no_sign_in)
    sheet_reader._reset()


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
