"""llm.py with fake Gemini/Groq/Z AI: request shape, parsing and provider fallback."""
import json
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


KEYS = ("GEMINI_API_KEY", "GROQ_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY", "PLUTO_PROVIDERS")


@pytest.fixture(autouse=True)
def only_gemini(monkeypatch):
    """Start every test with fake Gemini only; tests add other providers as needed."""
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")


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
    with pytest.raises(llm.AllProvidersFailed):
        llm.respond("x", [], [], core.to_local(NOW))
    gemini("")
    with pytest.raises(llm.AllProvidersFailed):
        llm.respond("x", [], [], core.to_local(NOW))


def test_no_key_at_all_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(llm.AllProvidersFailed, match="GEMINI_API_KEY, GROQ_API_KEY or ZAI_API_KEY"):
        llm.respond("x", [], [], core.to_local(NOW))


# ---------- fallback to Groq and Z AI ----------

class FakeHTTP:
    """Replaces llm._post. `answers` maps a URL fragment to a dict, or an exception to raise."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, url, headers, payload):
        self.calls.append(SimpleNamespace(url=url, headers=headers, payload=payload))
        for frag, ans in self.answers.items():
            if frag in url:
                if isinstance(ans, Exception):
                    raise ans
                return {"choices": [{"message": {"content": json.dumps(ans)}}]}
        raise AssertionError(f"unexpected URL {url}")


def broken_gemini(monkeypatch, exc=RuntimeError("429 RESOURCE_EXHAUSTED")):
    class Models:
        calls = 0

        def generate_content(self, **kw):
            Models.calls += 1
            raise exc

    monkeypatch.setattr(llm, "_client", SimpleNamespace(models=Models()))
    return Models


def test_providers_follow_keys_and_order(monkeypatch):
    assert llm.providers() == ["gemini"]
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("Z_AI_API_KEY", "z")  # alternative spelling works too
    assert llm.providers() == ["gemini", "groq", "zai"]
    monkeypatch.setenv("PLUTO_PROVIDERS", "zai, groq,bogus")
    assert llm.providers() == ["zai", "groq"]
    assert llm.describe().startswith("Z AI")


def test_gemini_failure_falls_back_to_groq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("ZAI_API_KEY", "test-zai-key")
    gem = broken_gemini(monkeypatch)
    http = FakeHTTP({"groq.com": {"reply": "Groq here"}})
    monkeypatch.setattr(llm, "_post", http)

    out = llm.respond("hi", [], [], core.to_local(NOW))
    assert out == {"reply": "Groq here", "save": [], "done": []}
    assert gem.calls == 1
    [call] = http.calls  # Z AI was not needed
    assert call.headers["Authorization"] == "Bearer test-groq-key"
    assert call.payload["model"] == llm.GROQ_MODEL
    assert call.payload["response_format"] == {"type": "json_object"}
    system, user = call.payload["messages"]
    assert system["role"] == "system" and "JSON" in system["content"]
    assert user["content"].rstrip().endswith("hi")


def test_groq_failure_falls_back_to_zai(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("ZAI_API_KEY", "test-zai-key")
    broken_gemini(monkeypatch)
    http = FakeHTTP({"groq.com": RuntimeError("timeout"), "z.ai": {"reply": "Z AI here"}})
    monkeypatch.setattr(llm, "_post", http)
    assert llm.respond("hi", [], [], core.to_local(NOW))["reply"] == "Z AI here"
    assert [c.url for c in http.calls] == [llm.GROQ_URL, llm.ZAI_URL]
    assert http.calls[1].payload["model"] == llm.ZAI_MODEL


def test_bad_json_from_one_provider_counts_as_failure(monkeypatch, gemini):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    gemini("I think the answer is... (no JSON)")
    monkeypatch.setattr(llm, "_post", FakeHTTP({"groq.com": {"reply": "fixed"}}))
    assert llm.respond("hi", [], [], core.to_local(NOW))["reply"] == "fixed"


def test_all_failing_raises_so_core_saves_a_raw_note(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("ZAI_API_KEY", "z")
    broken_gemini(monkeypatch)
    monkeypatch.setattr(llm, "_post", FakeHTTP({"groq.com": RuntimeError("down"),
                                                "z.ai": RuntimeError("down")}))
    with pytest.raises(llm.AllProvidersFailed, match="gemini.*groq.*zai"):
        llm.respond("hi", [], [], core.to_local(NOW))

    # The whole chain failing still keeps the user's message:
    reply = core.chat(1, "keep this thought", llm, NOW)[0]
    assert "plain note" in reply.text
    assert db.recall_pool(1)[0]["original_text"] == "keep this thought"


def test_keys_never_reach_the_log(monkeypatch, caplog):
    monkeypatch.setenv("GROQ_API_KEY", "SECRET-GROQ")
    broken_gemini(monkeypatch, RuntimeError("failed with key test-gemini-key"))
    monkeypatch.setattr(llm, "_post", FakeHTTP({"groq.com": RuntimeError("Bearer SECRET-GROQ rejected")}))
    with caplog.at_level("INFO"):
        with pytest.raises(llm.AllProvidersFailed) as e:
            llm.respond("hi", [], [], core.to_local(NOW))
    logged = caplog.text + str(e.value)
    assert "SECRET-GROQ" not in logged and "test-gemini-key" not in logged
