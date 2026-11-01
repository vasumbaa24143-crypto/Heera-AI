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


def context():
    db.add_item(1, "task", "Renew passport", "todo: renew passport, urgent",
                "2026-11-02T12:30:00Z", now=NOW, priority="high")
    db.add_message(1, "user", "hello", now=NOW)
    db.add_message(1, "pluto", "Hi!", now=NOW)
    return (core.history_views(db.recent_messages(1)),
            core.item_views(db.context_items(1), NOW))


def test_request_has_time_conversation_items_and_asks_for_json(gemini):
    g = gemini('{"reply": "Aaj passport renew karna hai (#1).", "save": [], "done": []}')
    history, items = context()
    out = llm.respond("aaj kya hai?", history, items, core.to_local(NOW))
    assert out == {"reply": "Aaj passport renew karna hai (#1).", "save": [], "done": []}
    c = g.calls[0]
    assert "Current local time: Monday, 2026-11-02 12:00" in c.contents
    assert "[Mon 2026-11-02 12:00] User: hello" in c.contents
    assert "Pluto: Hi!" in c.contents
    assert "#1 [task, high priority, today] saved Mon 2026-11-02 12:00 | due Mon 2026-11-02 18:00" in c.contents
    assert "original: todo: renew passport, urgent" in c.contents
    assert c.contents.rstrip().endswith("aaj kya hai?")
    assert c.config.response_mime_type == "application/json"


def test_prompt_carries_the_product_rules(gemini):
    g = gemini('{"reply": "hi"}')
    llm.respond("hi", [], [], core.to_local(NOW))
    system = g.calls[0].config.system_instruction
    assert "INFORMATION is saved automatically" in system
    assert "ACTIONS are saved ONLY when the user asks" in system
    assert "Never invent, guess or embellish a memory" in system
    assert "Never invent a time" in system
    assert "Never guess a priority" in system
    assert "(nothing saved yet)" in g.calls[0].contents


def test_tolerates_code_fences_and_fills_defaults(gemini):
    gemini('```json\n{"reply": "ok"}\n```')
    assert llm.respond("x", [], [], core.to_local(NOW)) == {"reply": "ok", "save": [], "done": []}


def test_garbage_raises_so_core_saves_a_raw_note(gemini):
    gemini("sorry, I can't help with that")
    with pytest.raises(ValueError):
        llm.respond("x", [], [], core.to_local(NOW))
    gemini("")
    with pytest.raises(ValueError):
        llm.respond("x", [], [], core.to_local(NOW))


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        llm.respond("x", [], [], core.to_local(NOW))
