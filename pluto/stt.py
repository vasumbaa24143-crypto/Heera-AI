"""Voice-to-text via Sarvam (good at Hindi/Hinglish).

NOTE: check the field names and model name against Sarvam's current docs
(https://docs.sarvam.ai) before relying on this. They are kept in env vars
so you can change them without editing code.
"""
import os

import httpx

URL = os.getenv("SARVAM_STT_URL", "https://api.sarvam.ai/speech-to-text")
MODEL = os.getenv("SARVAM_STT_MODEL", "saaras:v3")


async def transcribe(audio: bytes, filename: str = "voice.ogg", mime: str = "audio/ogg") -> str:
    key = os.getenv("SARVAM_API_KEY")
    if not key:
        raise RuntimeError("SARVAM_API_KEY is not set")
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            URL,
            headers={"api-subscription-key": key},
            files={"file": (filename, audio, mime)},
            data={"model": MODEL},
        )
    r.raise_for_status()
    return (r.json().get("transcript") or "").strip()
