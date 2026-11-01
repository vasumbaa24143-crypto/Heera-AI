"""Voice-to-text. Tries each provider that has a key, in order, until one works:

    Groq Whisper (GROQ_API_KEY) -> Gemini (GEMINI_API_KEY) -> Sarvam (SARVAM_API_KEY)

Groq's whisper-large-v3 handles Hindi, English and Hinglish well and accepts
Telegram's .ogg voice notes directly. Gemini understands audio natively.
Sarvam is a good Indian-language fallback. STT_PROVIDERS changes the order.
"""
import logging
import os

import httpx

log = logging.getLogger("pluto")

GROQ_STT_URL = os.getenv("GROQ_STT_URL", "https://api.groq.com/openai/v1/audio/transcriptions")
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3")
GEMINI_STT_MODEL = os.getenv("GEMINI_STT_MODEL", os.getenv("PLUTO_MODEL", "gemini-2.5-flash"))
SARVAM_STT_URL = os.getenv("SARVAM_STT_URL", "https://api.sarvam.ai/speech-to-text")
SARVAM_STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saarika:v2.5")
STT_PROVIDERS = ("groq", "gemini", "sarvam")
KEY_NAMES = "GROQ_API_KEY, GEMINI_API_KEY or SARVAM_API_KEY"
TIMEOUT = 90

GEMINI_PROMPT = (
    "Transcribe this voice note exactly as spoken. The speaker may use Hindi, English or "
    "Hinglish; write Hindi words in Latin script (Hinglish), as they would type them. "
    "Return only the transcript, with no commentary. If there is no speech, return nothing."
)


class TranscriptionFailed(RuntimeError):
    pass


def _key(name):
    return os.getenv({"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY",
                      "sarvam": "SARVAM_API_KEY"}[name])


def providers() -> list:
    order = [p.strip().lower() for p in os.getenv("STT_PROVIDERS", ",".join(STT_PROVIDERS)).split(",")]
    return [p for p in dict.fromkeys(order) if p in STT_PROVIDERS and _key(p)]


async def _post_file(url, headers, filename, audio, mime, data) -> dict:
    """One multipart upload; separated out so tests can replace it."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(url, headers=headers, files={"file": (filename, audio, mime)}, data=data)
    r.raise_for_status()
    return r.json()


async def _groq(audio, filename, mime) -> str:
    out = await _post_file(
        GROQ_STT_URL, {"Authorization": f"Bearer {_key('groq')}"}, filename, audio, mime,
        {"model": GROQ_STT_MODEL, "response_format": "json", "temperature": "0"},
    )
    return out.get("text") or ""


async def _sarvam(audio, filename, mime) -> str:
    out = await _post_file(
        SARVAM_STT_URL, {"api-subscription-key": _key("sarvam")}, filename, audio, mime,
        {"model": SARVAM_STT_MODEL, "language_code": "unknown"},
    )
    return out.get("transcript") or ""


async def _gemini(audio, filename, mime) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=_key("gemini"))
    resp = await client.aio.models.generate_content(
        model=GEMINI_STT_MODEL,
        contents=[types.Part.from_bytes(data=audio, mime_type=mime.split(";")[0]), GEMINI_PROMPT],
        config=types.GenerateContentConfig(temperature=0),
    )
    return resp.text or ""


_CALLS = {"groq": _groq, "gemini": _gemini, "sarvam": _sarvam}


async def transcribe(audio: bytes, filename: str = "voice.ogg", mime: str = "audio/ogg") -> str:
    """Return the transcript ("" for silence). Raises TranscriptionFailed if no provider works."""
    names = providers()
    if not names:
        raise TranscriptionFailed(f"no speech-to-text key set ({KEY_NAMES})")
    errors = []
    for name in names:
        try:
            text = (await _CALLS[name](audio, filename, mime)).strip()
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            log.warning("voice: %s failed (%s%s); trying next",
                        name, type(e).__name__, f" {status}" if status else "")
            errors.append(f"{name}: {type(e).__name__}{f' {status}' if status else ''}")
            continue
        if name != names[0]:
            log.info("voice transcribed by fallback provider %s", name)
        return text
    raise TranscriptionFailed("; ".join(errors))
