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
    def __init__(self, fail=False, voice=None):
        self.sent, self.fail, self.voice, self.files = [], fail, voice, []

    async def get_file(self, file_id, **timeouts):
        self.files.append(file_id)
        return await self.voice.get_file(**timeouts)

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("Telegram is down")
        self.sent.append((chat_id, text, reply_markup))


class FakeVoice:
    """A voice note. `fail` lists errors to raise on the first downloads, one per attempt."""
    file_name, mime_type, file_id = None, "audio/ogg", "VOICE-FILE-ID"

    def __init__(self, fail=()):
        self.fail, self.attempts, self.timeouts = list(fail), 0, []

    async def get_file(self, **timeouts):
        self.attempts += 1
        self.timeouts.append(timeouts)
        if self.fail:
            raise self.fail.pop(0)
        return self

    async def download_to_memory(self, buf, **timeouts):
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
    monkeypatch.setattr(bot, "VOICE_RETRY_WAIT", (0, 0))


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
        "Cleared our chat history (0 messages), its summary and the profile facts "
        "built from it. Your saved items are untouched.",
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


def transcribes(monkeypatch, text="podcast ka idea"):
    async def fake_transcribe(audio, filename, mime):
        return text
    monkeypatch.setattr(bot.stt, "transcribe", fake_transcribe)


def test_voice_download_retries_a_slow_telegram(monkeypatch):
    from telegram.error import TimedOut
    use_llm(monkeypatch, FakeLLM(says("", [("idea", "Podcast", None)])))
    transcribes(monkeypatch)
    v = FakeVoice(fail=[TimedOut(), bot.NetworkError("connection reset")])
    u = update(voice=v)
    go(bot.on_voice(u, ctx()))
    assert v.attempts == 3
    assert v.timeouts[0]["read_timeout"] == bot.VOICE_TIMEOUT > 5        # not the 5 s default
    assert [t for t, _ in u.message.replies] == ["🎙 Heard: podcast ka idea", "Noted 💡 #1 Podcast"]


def test_voice_download_gives_up_with_a_try_again_button(monkeypatch):
    from telegram.error import TimedOut
    transcribes(monkeypatch)
    v = FakeVoice(fail=[TimedOut()] * bot.VOICE_ATTEMPTS)
    u = update(voice=v)
    go(bot.on_voice(u, ctx()))
    assert v.attempts == bot.VOICE_ATTEMPTS
    [(text, markup)] = u.message.replies
    assert "too slow" in text
    assert markup.inline_keyboard[0][0].callback_data == bot.VOICE_RETRY
    assert json.loads(db.get_setting(ME, "pending_voice"))["file_id"] == "VOICE-FILE-ID"
    assert db.recall_pool(ME) == []


def test_voice_refused_by_telegram_is_not_retried(monkeypatch):
    v = FakeVoice(fail=[bot.BadRequest("File is too big")])
    u = update(voice=v)
    go(bot.on_voice(u, ctx()))
    assert v.attempts == 1 and "Try again" in u.message.replies[0][0]


def test_try_again_button_retries_the_same_voice_note(monkeypatch):
    from telegram.error import TimedOut
    use_llm(monkeypatch, FakeLLM(says("", [("idea", "Podcast", None)])))
    transcribes(monkeypatch)
    go(bot.on_voice(update(voice=FakeVoice(fail=[TimedOut()] * bot.VOICE_ATTEMPTS)), ctx()))

    fake_bot = FakeBot(voice=FakeVoice())
    u = update(data=bot.VOICE_RETRY)
    u.effective_message = u.callback_query.message
    go(bot.on_button(u, ctx(fake_bot=fake_bot)))
    assert fake_bot.files == ["VOICE-FILE-ID"] and u.callback_query.markup_cleared
    assert [t for t, _ in u.callback_query.message.replies] == \
        ["🎙 Heard: podcast ka idea", "Noted 💡 #1 Podcast"]
    assert db.get_setting(ME, "pending_voice") == ""            # done: nothing left to retry

    again = update(data=bot.VOICE_RETRY)
    again.effective_message = again.callback_query.message
    go(bot.on_button(again, ctx(fake_bot=fake_bot)))
    assert "don't have a voice note waiting" in again.callback_query.message.replies[0][0]


def test_failed_transcription_can_be_retried(monkeypatch):
    async def broken(*a, **k):
        raise bot.stt.TranscriptionFailed("groq: HTTPStatusError 500")

    monkeypatch.setattr(bot.stt, "transcribe", broken)
    monkeypatch.setattr(bot.stt, "providers", lambda: ["groq"])
    u = update(voice=FakeVoice())
    go(bot.on_voice(u, ctx()))
    [(text, markup)] = u.message.replies
    assert "couldn't make out" in text and markup.inline_keyboard[0][0].callback_data == bot.VOICE_RETRY
    assert db.get_setting(ME, "pending_voice")


def edited(text):
    """An update for an edited message: Telegram leaves `message` empty."""
    u = update(text=text)
    u.effective_message, u.message = u.message, None
    return u


def test_edited_message_gets_an_answer(monkeypatch):
    use_llm(monkeypatch, FakeLLM(says("Pakka", [("task", "Call bank", None)])))
    u = edited("remind me to call bank")
    go(bot.on_text(u, ctx()))
    assert [t for t, _ in u.effective_message.replies][0] == "Pakka"
    assert db.recall_pool(ME)[0]["original_text"] == "remind me to call bank"


def test_edited_command_works(monkeypatch):
    db.add_item(ME, "task", "Call bank", "call bank", now=NOW)
    u = edited("/list")
    go(bot.cmd_list(u, ctx()))
    assert "Call bank" in u.effective_message.replies[0][0]


def test_typing_indicator_is_shown_while_thinking(monkeypatch):
    use_llm(monkeypatch, FakeLLM(says("hi")))
    actions = []

    async def send_action(action):
        actions.append(action)

    u = update(text="hello")
    u.message.chat = SimpleNamespace(send_action=send_action)
    go(bot.on_text(u, ctx()))
    assert actions == ["typing"]
    go(bot.typing(SimpleNamespace()))                           # no chat: quietly skipped


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


def test_a_sent_nudge_is_kept_in_the_chat_for_the_next_reply(monkeypatch):
    i = db.add_item(ME, "task", "Barber", "remind me barber 6.30", db.to_str(NOW), now=NOW)
    go(bot.nudge_job(ctx()))
    llm = use_llm(monkeypatch, FakeLLM(says(f"Done #{i}", done=[i])))
    go(bot.on_text(update(text="Done already"), ctx()))
    assert llm.calls[0]["history"][-1]["text"] == f"⏰ Reminder: Barber (#{i})"
    assert db.get_item(ME, i)["status"] == "done"


def test_replying_to_a_message_shows_it_to_the_assistant(monkeypatch):
    i = db.add_item(ME, "task", "Barber", "remind me barber 6.30", now=NOW)
    llm = use_llm(monkeypatch, FakeLLM(says(f"Done #{i}", done=[i])))
    u = update(text="Done already")
    u.message.reply_to_message = SimpleNamespace(text=f"⏰ Reminder: Barber (#{i})", caption=None)
    go(bot.on_text(u, ctx()))
    assert llm.calls[0]["message"] == f"[Replying to: ⏰ Reminder: Barber (#{i})]\nDone already"
    assert db.recent_messages(ME)[0]["text"] == "Done already"  # stored as the user wrote it
    assert db.get_item(ME, i)["status"] == "done"


def test_a_plain_message_is_sent_as_is(monkeypatch):
    llm = use_llm(monkeypatch, FakeLLM(says("hi")))
    go(bot.on_text(update(text="hello"), ctx()))
    assert llm.calls[0]["message"] == "hello"


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
    assert sum(len(h) for h in app.handlers.values()) == 16  # incl. /topic
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
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:VALUE-SHOULD-NOT-SHOW-TG")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-VALUE-SHOULD-NOT-SHOW")
    out = bot.key_report()
    for n, _, _ in bot.KEY_REPORT:
        assert n in out
    assert "VALUE-SHOULD-NOT-SHOW" not in out
    assert "✓ set      NVIDIA_API_KEY" in out and "✗ MISSING  ALLOWED_USER_ID" in out
    assert "AI order: NVIDIA Nemotron" in out
