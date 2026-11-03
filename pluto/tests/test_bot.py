"""The Telegram adapter (bot.py), driven with fake Telegram objects - no network."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import bot
import db
from conftest import NOW, FakeLLM, says

ME, STRANGER = 111, 999


class FakeMessage:
    def __init__(self, text=None, voice=None):
        self.text, self.voice, self.audio = text, voice, None
        self.replies, self.documents = [], []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))

    async def reply_document(self, document, filename=None):
        self.documents.append((document.getvalue(), filename))


class FakeQuery:
    def __init__(self, data):
        self.data, self.message = data, FakeMessage()
        self.answered = self.markup_cleared = False

    async def answer(self):
        self.answered = True

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_cleared = True


class FakeBot:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("Telegram is down")
        self.sent.append((chat_id, text, reply_markup))


class FakeVoice:
    file_name, mime_type = None, "audio/ogg"

    async def get_file(self):
        return self

    async def download_to_memory(self, buf):
        buf.write(b"OggS-fake-audio")


def update(uid=ME, text=None, voice=None, data=None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid),
        message=FakeMessage(text, voice),
        callback_query=FakeQuery(data) if data else None,
    )


def ctx(*args, fake_bot=None):
    return SimpleNamespace(args=list(args), bot=fake_bot or FakeBot())


def go(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", str(ME))
    monkeypatch.setattr(db, "now_utc", lambda: NOW)


def use_llm(monkeypatch, llm):
    monkeypatch.setattr(bot, "llm", llm)
    return llm


def buttons_of(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def test_text_is_saved_and_replied_with_buttons(monkeypatch):
    use_llm(monkeypatch, FakeLLM(says("Pakka 👍", [("task", "Send deck", "2026-11-03T18:00")])))
    u = update(text="remind me kal 6 baje deck bhejna")
    go(bot.on_text(u, ctx()))
    [(first, no_markup), (text, markup)] = u.message.replies
    assert (first, no_markup) == ("Pakka 👍", None)
    assert text.startswith("Added ✅ #1 Send deck")
    assert buttons_of(markup) == [("✅ Done", "done:1"), ("😴 +1 day", "snz:1")]


def test_stranger_is_ignored(monkeypatch):
    llm = use_llm(monkeypatch, FakeLLM(says("hi", [("note", "x", None)])))
    u = update(STRANGER, text="hello")
    go(bot.on_text(u, ctx()))
    go(bot.cmd_list(u, ctx()))
    go(bot.cmd_export(u, ctx()))
    assert u.message.replies == [] and u.message.documents == []
    assert llm.calls == [] and db.recall_pool(STRANGER) == []
    assert db.recent_messages(STRANGER) == []


def test_no_allowed_user_configured_means_nobody(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", "")
    use_llm(monkeypatch, FakeLLM(says("hi", [("note", "x", None)])))
    u = update(text="hello")
    go(bot.on_text(u, ctx()))
    assert u.message.replies == [] and db.recall_pool(ME) == []


def test_labelling_failure_still_saves(monkeypatch):
    use_llm(monkeypatch, FakeLLM(RuntimeError("Gemini unreachable")))
    u = update(text="don't lose this")
    go(bot.on_text(u, ctx()))
    assert "plain note" in u.message.replies[0][0]
    assert db.recall_pool(ME)[0]["original_text"] == "don't lose this"


def test_commands(monkeypatch):
    i = db.add_item(ME, "task", "Pay rent", "pay rent", now=NOW)
    u = update()
    go(bot.cmd_list(u, ctx()))
    go(bot.cmd_snooze(u, ctx(str(i))))
    go(bot.cmd_done(u, ctx(f"#{i}")))
    go(bot.cmd_done(u, ctx()))
    go(bot.cmd_export(u, ctx()))
    go(bot.cmd_delete(u, ctx(str(i))))
    go(bot.cmd_forget(u, ctx()))
    texts = [t for t, _ in u.message.replies]
    assert texts == [
        f"✅ #{i} Pay rent", f"Snoozed #{i} for a day 😴", f"Done #{i} ✅",
        "Usage: /done <id>", f"Deleted #{i} forever.",
        "Cleared our chat history (0 messages). Your saved items are untouched.",
    ]
    [(raw, name)] = u.message.documents
    assert name == "pluto-export.json"
    assert json.loads(raw)["items"][0]["original_text"] == "pay rent"


def test_button_press(monkeypatch):
    i = db.add_item(ME, "task", "t", "t", now=NOW)
    u = update(data=f"done:{i}")
    go(bot.on_button(u, ctx()))
    q = u.callback_query
    assert q.answered and q.markup_cleared
    assert q.message.replies == [(f"Done #{i} ✅", None)]
    assert db.get_item(ME, i)["status"] == "done"


def test_strangers_button_press_does_nothing_but_is_answered():
    i = db.add_item(ME, "task", "t", "t", now=NOW)
    u = update(STRANGER, data=f"done:{i}")
    go(bot.on_button(u, ctx()))
    assert u.callback_query.answered and u.callback_query.message.replies == []
    assert db.get_item(ME, i)["status"] == "open"


def test_voice_note_is_transcribed_then_saved(monkeypatch):
    use_llm(monkeypatch, FakeLLM(says("", [("idea", "Podcast", None)])))
    seen = {}

    async def fake_transcribe(audio, filename, mime):
        seen.update(audio=audio, mime=mime)
        return "podcast ka idea"

    monkeypatch.setattr(bot.stt, "transcribe", fake_transcribe)
    u = update(voice=FakeVoice())
    go(bot.on_voice(u, ctx()))
    assert [t for t, _ in u.message.replies] == ["🎙 Heard: podcast ka idea", "Noted 💡 #1 Podcast"]
    assert seen == {"audio": b"OggS-fake-audio", "mime": "audio/ogg"}
    assert db.recall_pool(ME)[0]["original_text"] == "podcast ka idea"


def test_voice_transcription_failure(monkeypatch):
    async def broken(*a, **k):
        raise bot.stt.TranscriptionFailed("groq: HTTPStatusError 500")

    monkeypatch.setattr(bot.stt, "transcribe", broken)
    monkeypatch.setattr(bot.stt, "providers", lambda: ["groq"])
    u = update(voice=FakeVoice())
    go(bot.on_voice(u, ctx()))
    assert "couldn't make out that voice note" in u.message.replies[0][0]
    assert db.recall_pool(ME) == []


def test_voice_without_any_key_says_what_to_add(monkeypatch):
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "SARVAM_API_KEY", "STT_PROVIDERS"):
        monkeypatch.delenv(k, raising=False)
    u = update(voice=FakeVoice())
    go(bot.on_voice(u, ctx()))
    assert "GROQ_API_KEY, GEMINI_API_KEY or SARVAM_API_KEY" in u.message.replies[0][0]


def test_nudge_job_sends_and_marks():
    i = db.add_item(ME, "task", "Call mom", "call mom", db.to_str(NOW), now=NOW)
    c = ctx()
    go(bot.nudge_job(c))
    [(chat, text, markup)] = c.bot.sent
    assert (chat, text) == (ME, f"⏰ Reminder: Call mom (#{i})")
    assert ("✅ Done", f"done:{i}") in buttons_of(markup)
    assert db.get_item(ME, i)["nudge_count"] == 1


def test_nudge_job_failed_send_is_retried_later():
    i = db.add_item(ME, "task", "Call mom", "call mom", db.to_str(NOW), now=NOW)
    go(bot.nudge_job(ctx(fake_bot=FakeBot(fail=True))))
    assert db.get_item(ME, i)["nudge_count"] == 0


def test_nudge_job_sends_a_reminder_at_night_but_holds_repeats(monkeypatch):
    from datetime import timedelta
    night = NOW + timedelta(hours=11)  # 23:00 in India
    i = db.add_item(ME, "task", "x", "x", db.to_str(night), now=NOW)
    monkeypatch.setattr(db, "now_utc", lambda: night)
    c = ctx()
    go(bot.nudge_job(c))
    assert [t for _, t, _ in c.bot.sent] == [f"⏰ Reminder: x (#{i})"]
    later = night + timedelta(hours=7)  # 06:00, still quiet; a repeat would be due
    monkeypatch.setattr(db, "now_utc", lambda: later)
    c2 = ctx()
    go(bot.nudge_job(c2))
    assert c2.bot.sent == []


def test_nudge_job_runs_every_minute():
    assert bot.NUDGE_EVERY_SECONDS == 60


def test_config_check(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "ALLOWED_USER_ID", "GEMINI_API_KEY", "GROQ_API_KEY",
              "ZAI_API_KEY", "Z_AI_API_KEY", "NVIDIA_API_KEY", "PLUTO_PROVIDERS"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID, an AI key"):
        bot.check_config()


def test_config_accepts_any_one_ai_key(monkeypatch):
    for k in ("GEMINI_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY", "NVIDIA_API_KEY", "PLUTO_PROVIDERS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_ID", str(ME))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    bot.check_config()  # does not exit


def test_app_builds_with_all_handlers(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-not-a-real-token")
    app = bot.build_app()
    assert sum(len(h) for h in app.handlers.values()) == 13
    assert app.job_queue is not None  # needs python-telegram-bot[job-queue]


def test_env_example_has_a_slot_for_every_key():
    import pathlib
    names = {ln.split("=")[0] for ln in
             (pathlib.Path(bot.__file__).parent / ".env.example").read_text().splitlines()
             if ln and not ln.startswith("#") and "=" in ln}
    assert names == {n for n, _, _ in bot.KEY_REPORT}


def test_key_check_lists_every_key_and_never_shows_values(monkeypatch):
    for n, _, _ in bot.KEY_REPORT:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.delenv("Z_AI_API_KEY", raising=False)
    monkeypatch.delenv("PLUTO_PROVIDERS", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET-TG")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-SECRET")
    out = bot.key_report()
    for n, _, _ in bot.KEY_REPORT:
        assert n in out
    assert "SECRET" not in out
    assert "✓ set      NVIDIA_API_KEY" in out and "✗ MISSING  ALLOWED_USER_ID" in out
    assert "AI order: NVIDIA Nemotron" in out
