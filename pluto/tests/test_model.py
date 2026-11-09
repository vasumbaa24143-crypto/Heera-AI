"""/model: choose which AI provider thinks first."""
import asyncio
from types import SimpleNamespace

import bot
import core
import db
import llm
import offline
from conftest import NOW, FakeLLM, says

UID, OTHER = 111, 222


def test_menu_lists_auto_and_each_configured_provider():
    r = core.cmd_model(UID, FakeLLM())
    assert "Which AI should think first" in r.text and "Now: Auto" in r.text
    assert r.buttons == [
        [("✓ ⚡ Auto (Gemini → Groq)", "model:auto")],
        [("Gemini (g-1)", "model:gemini")],
        [("Groq (q-1)", "model:groq")],
    ]


def test_choosing_by_button_is_remembered_and_marked():
    fake = FakeLLM()
    r = core.button(UID, "model:groq", NOW, llm=fake)
    assert r.text == "🧠 Done — I'll think with Groq (q-1) first. Gemini stays as backup."
    assert db.get_setting(UID, "model") == "groq"
    menu = core.cmd_model(UID, fake)
    assert "Now: Groq (q-1)" in menu.text
    assert [("✓ Groq (q-1)", "model:groq")] in menu.buttons
    assert db.get_setting(OTHER, "model") is None  # per user


def test_choice_reaches_the_assistant():
    fake = FakeLLM(says("hi"))
    core.chat(UID, "hello", fake, NOW)
    core.cmd_model(UID, fake, "groq")
    core.chat(UID, "hello again", fake, NOW)
    assert [c["prefer"] for c in fake.calls] == [None, "groq"]


def test_typed_choice_auto_and_unknown():
    fake = FakeLLM()
    assert core.cmd_model(UID, fake, "Gemini").text.startswith("🧠 Done — I'll think with Gemini")
    assert core.cmd_model(UID, fake, "auto").text == "⚡ Auto: I'll use Gemini → Groq, falling back in that order."
    assert db.get_setting(UID, "model") == "auto"
    assert core.cmd_model(UID, fake, "gpt9").text == "I don't have “gpt9” set up. Options: auto, gemini, groq."


def test_choice_whose_key_was_removed_falls_back_to_auto():
    db.set_setting(UID, "model", "zai")
    assert "Now: Auto" in core.cmd_model(UID, FakeLLM()).text


def test_offline_or_no_keys_has_nothing_to_choose():
    assert "no AI model to choose" in core.cmd_model(UID, offline).text
    assert "no AI model to choose" in core.cmd_model(UID, FakeLLM(choices=())).text


def test_real_llm_preference_moves_provider_first(monkeypatch):
    for k in ("PLUTO_PROVIDERS", "Z_AI_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "ZAI_API_KEY"):
        monkeypatch.setenv(k, "x")
    assert llm.providers() == ["gemini", "groq", "zai"]
    assert llm.providers("zai") == ["zai", "gemini", "groq"]
    assert llm.providers("bogus") == ["gemini", "groq", "zai"]
    assert [n for n, _ in llm.choices()] == ["gemini", "groq", "zai"]
    assert llm.describe("groq").startswith("Groq")


def test_telegram_model_menu_and_button(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    monkeypatch.setattr(bot, "llm", FakeLLM())
    sent = []

    async def reply_text(text, reply_markup=None):
        sent.append((text, reply_markup))

    msg = SimpleNamespace(reply_text=reply_text)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=UID), message=msg, callback_query=None)
    asyncio.run(bot.cmd_model(update, SimpleNamespace(args=[])))
    text, markup = sent[0]
    rows = [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]
    assert rows[1] == [("Gemini (g-1)", "model:gemini")] and len(rows) == 3

    answered = []

    async def answer():
        answered.append(True)

    async def edit(reply_markup=None):
        pass

    q = SimpleNamespace(data="model:groq", answer=answer, edit_message_reply_markup=edit, message=msg)
    asyncio.run(bot.on_button(SimpleNamespace(effective_user=SimpleNamespace(id=UID), callback_query=q),
                              SimpleNamespace(args=[])))
    assert answered and sent[-1][0].startswith("🧠 Done — I'll think with Groq")
    assert db.get_setting(UID, "model") == "groq"
