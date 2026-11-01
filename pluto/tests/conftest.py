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
    """Stands in for llm.py. `extract` may be a dict, a callable(text) -> dict,
    or an exception to raise. Records every call."""

    def __init__(self, extract=None, answer="(fake answer)"):
        self._extract = extract if extract is not None else {"is_question": False, "items": []}
        self._answer = answer
        self.extract_calls, self.answer_calls = [], []

    def extract(self, text, now_local):
        self.extract_calls.append((text, now_local))
        r = self._extract
        if isinstance(r, Exception):
            raise r
        return r(text) if callable(r) else r

    def answer(self, question, items, now_local):
        self.answer_calls.append((question, list(items), now_local))
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def labels(*items, question=False):
    """Build a labeller result: labels(("task", "Send deck", "2026-11-03T18:00"), ...)."""
    return {
        "is_question": question,
        "items": [{"kind": k, "title": t, "due_local": d} for k, t, d in items],
    }


@pytest.fixture
def fake_llm():
    return FakeLLM
