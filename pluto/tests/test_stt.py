"""Voice-to-text: Groq Whisper -> Gemini -> Sarvam fallback, with fakes (no network)."""
import asyncio
from types import SimpleNamespace

import pytest

import stt

KEYS = ("GROQ_API_KEY", "GEMINI_API_KEY", "SARVAM_API_KEY", "STT_PROVIDERS")


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)


def run(coro):
    return asyncio.run(coro)


class FakeUpload:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def __call__(self, url, headers, filename, audio, mime, data):
        self.calls.append(SimpleNamespace(url=url, headers=headers, filename=filename,
                                          audio=audio, mime=mime, data=data))
        for frag, ans in self.answers.items():
            if frag in url:
                if isinstance(ans, Exception):
                    raise ans
                return ans
        raise AssertionError(url)


def test_providers_follow_keys_and_order(monkeypatch):
    assert stt.providers() == []
    monkeypatch.setenv("SARVAM_API_KEY", "s")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    assert stt.providers() == ["groq", "sarvam"]
    monkeypatch.setenv("STT_PROVIDERS", "sarvam,groq")
    assert stt.providers() == ["sarvam", "groq"]


def test_no_key_is_a_clear_error():
    with pytest.raises(stt.TranscriptionFailed, match="GROQ_API_KEY, GEMINI_API_KEY or SARVAM_API_KEY"):
        run(stt.transcribe(b"x"))


def test_groq_whisper_request(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq")
    up = FakeUpload({"groq.com": {"text": "  kal 5 baje Rahul ko call karna hai "}})
    monkeypatch.setattr(stt, "_post_file", up)
    assert run(stt.transcribe(b"OggS", "voice.ogg", "audio/ogg")) == "kal 5 baje Rahul ko call karna hai"
    c = up.calls[0]
    assert c.url == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert c.headers == {"Authorization": "Bearer test-groq"}
    assert c.data["model"] == "whisper-large-v3"
    assert (c.filename, c.audio, c.mime) == ("voice.ogg", b"OggS", "audio/ogg")


def test_groq_failure_falls_back_to_gemini(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GEMINI_API_KEY", "m")
    monkeypatch.setattr(stt, "_post_file", FakeUpload({"groq.com": RuntimeError("429")}))
    seen = {}

    async def fake_gemini(audio, filename, mime):
        seen.update(audio=audio, mime=mime)
        return "remind me tomorrow"

    monkeypatch.setitem(stt._CALLS, "gemini", fake_gemini)
    assert run(stt.transcribe(b"OggS", "v.ogg", "audio/ogg")) == "remind me tomorrow"
    assert seen == {"audio": b"OggS", "mime": "audio/ogg"}


def test_sarvam_request_uses_the_transcription_model(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "test-sarvam")
    up = FakeUpload({"sarvam.ai": {"transcript": "namaste"}})
    monkeypatch.setattr(stt, "_post_file", up)
    assert run(stt.transcribe(b"x")) == "namaste"
    c = up.calls[0]
    assert c.headers == {"api-subscription-key": "test-sarvam"}
    assert c.data["model"] == "saarika:v2.5"


def test_all_failing_raises_with_a_summary_and_no_keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "SECRET-G")
    monkeypatch.setenv("SARVAM_API_KEY", "SECRET-S")
    monkeypatch.setattr(stt, "_post_file", FakeUpload({"groq.com": RuntimeError("Bearer SECRET-G bad"),
                                                       "sarvam.ai": ValueError("bad")}))
    with pytest.raises(stt.TranscriptionFailed) as e:
        run(stt.transcribe(b"x"))
    assert "groq: RuntimeError" in str(e.value) and "sarvam: ValueError" in str(e.value)
    assert "SECRET" not in str(e.value)


def test_silence_returns_empty(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setattr(stt, "_post_file", FakeUpload({"groq.com": {"text": "   "}}))
    assert run(stt.transcribe(b"x")) == ""
