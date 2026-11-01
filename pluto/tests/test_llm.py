"""llm.py with a fake Gemini client: request shape and response parsing."""
from types import SimpleNamespace

import pytest

import core
import db
import llm
from conftest import NOW


class FakeModels:
    def __init__(self, text):
        self.text, self.calls = text, []

    def generate_content(self, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        return SimpleNamespace(text=self.text)


@pytest.fixture
def gemini(monkeypatch):
    def make(text):
        models = FakeModels(text)
        monkeypatch.setattr(llm, "_client", SimpleNamespace(models=models))
        return models
    return make


def test_extract_sends_local_time_and_asks_for_json(gemini):
    g = gemini('{"is_question": false, "items": [{"kind": "task", "title": "x", "due_local": null}]}')
    out = llm.extract("do x", core.to_local(NOW))
    assert out["items"][0]["kind"] == "task"
    call = g.calls[0]
    assert "Monday, 2026-11-02 12:00" in call.contents and "do x" in call.contents
    assert call.config.response_mime_type == "application/json"
    assert "Never invent a deadline" in call.config.system_instruction


def test_extract_tolerates_code_fences_and_fills_defaults(gemini):
    gemini('```json\n{"items": []}\n```')
    assert llm.extract("hi", core.to_local(NOW)) == {"items": [], "is_question": False}


def test_extract_raises_on_garbage_so_core_saves_raw_note(gemini):
    gemini("sorry, I can't help with that")
    with pytest.raises(ValueError):
        llm.extract("hi", core.to_local(NOW))
    gemini("")
    with pytest.raises(ValueError):
        llm.extract("hi", core.to_local(NOW))


def test_answer_gets_items_in_local_time(gemini):
    g = gemini("  #1 tha.  ")
    i = db.add_item(1, "idea", "YouTube", "youtube idea", now=NOW)
    out = llm.answer("kya tha?", db.recall_pool(1), core.to_local(NOW))
    assert out == "#1 tha."
    assert f"#{i} [idea, open] saved 2026-11-02 12:00" in g.calls[0].contents
    assert "original: youtube idea" in g.calls[0].contents
    assert "Never invent" in g.calls[0].config.system_instruction


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        llm.extract("x", core.to_local(NOW))
