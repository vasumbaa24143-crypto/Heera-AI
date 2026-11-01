"""simulate.py runs the same flow as the bot; check a scripted session with the offline assistant."""
import db
import offline
import simulate
from conftest import NOW, FakeLLM, says


def test_scripted_session_offline(monkeypatch):
    monkeypatch.setattr(db, "now_utc", lambda: NOW)  # Monday 12:00 in India
    s = simulate.Session(offline, uid=1)

    assert s.handle("hi") == ["Good afternoon! What can I do for you?"]
    assert s.handle("Rahul ka birthday 5 March ko hai") == ["Noted 📝 #1 Rahul ka birthday 5 March ko hai"]
    assert s.handle("mujhe bank jana hai") == ["Add it as a to-do?"]
    out = s.handle("haan")
    assert out[1].startswith("Added ✅ #2") and "/tap done:2" in out[2]
    assert s.handle("remind me to call Rahul")[0].startswith("Kab yaad dilaun?")
    out = s.handle("kal 5 baje")
    assert out[1] == "Added ✅ #3 call Rahul — due Tue 03 Nov, 17:00"
    assert s.handle("todo: renew passport, urgent")[1] == "Added ✅ #4 🔴 renew passport"

    assert s.handle("what is on my plate today?") == ["Here's today:\n• #4 🔴 renew passport (no date)"]
    week = s.handle("what about this week?")[0]
    assert "#3 call Rahul (tomorrow" in week and "#4" not in week
    about = s.handle("what have we discussed about Rahul?")[0]
    assert "#1 Rahul ka birthday" in about and "#3 call Rahul" in about
    assert s.handle("what about Goa?") == ["I don't have anything about that saved."]
    assert s.handle("thanks") == ["Anytime! 🙂"]

    assert s.handle("/nudges") == ["(nothing to nudge right now)"]
    s.handle("/advance 2d")
    assert any("Reminder: call Rahul" in o for o in s.handle("/nudges"))
    assert s.handle("/tap done:3") == ["Done #3 ✅"]
    assert s.handle("/delete 1") == ["Deleted #1 forever."]
    assert s.handle("/forget")[0].startswith("Cleared our chat history")
    assert s.handle("/bogus")[0].startswith("Unknown command")


def test_session_falls_back_to_raw_note_when_the_assistant_fails():
    s = simulate.Session(FakeLLM(RuntimeError("no network")), uid=5)
    assert "plain note (#1)" in s.handle("remember this")[0]
    s2 = simulate.Session(FakeLLM(says("Pakka", [("promise", "Deck to Rahul", None)])), uid=5)
    assert s2.handle("track this promise: deck to rahul")[:2] == ["Pakka", "Added 🤝 #2 Deck to Rahul"]
