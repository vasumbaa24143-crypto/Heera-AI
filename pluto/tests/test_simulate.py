"""simulate.py drives the same flow as the bot; check a short scripted session."""
import db
import offline
import simulate
from conftest import NOW, FakeLLM, labels


def test_scripted_session_offline(monkeypatch):
    monkeypatch.setattr(db, "now_utc", lambda: NOW)
    s = simulate.Session(offline, uid=1)
    out = s.handle("I need to call the bank tomorrow")
    assert out[0].startswith("Saved. ✅ #1")
    assert "/tap done:1" in out[1]
    out = s.handle("YouTube tech news ka idea")
    assert out == ["Saved. 💡 #2 YouTube tech news ka idea"]
    assert s.handle("YouTube wala idea kya tha?") == ["#2 YouTube tech news ka idea (open)"]
    assert s.handle("what about Goa?") == ["I don't have that saved."]
    assert s.handle("/nudges") == ["(nothing to nudge right now)"]
    s.handle("/advance 4d")
    out = s.handle("/nudges")
    assert any("Due: I need to call the bank" in o for o in out)
    assert any("You told me this 4 days ago" in o for o in out)
    assert s.handle("/tap done:1") == ["Done #1 ✅"]
    assert s.handle("/delete 2") == ["Deleted #2 forever."]
    assert s.handle("/list") == ["Nothing open. Tell me something to remember."]
    assert s.handle("/bogus")[0].startswith("Unknown command")


def test_session_uses_given_llm_and_falls_back_to_raw_note():
    s = simulate.Session(FakeLLM(RuntimeError("no network")), uid=5)
    assert "plain note (#1)" in s.handle("remember this")[0]
    s2 = simulate.Session(FakeLLM(labels(("promise", "Deck to Rahul", None))), uid=5)
    assert s2.handle("deck to rahul")[0] == "Saved. 🤝 #2 Deck to Rahul"
