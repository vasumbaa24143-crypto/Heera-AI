"""Understanding layer: label a message, and answer recall questions.

Uses Google Gemini (google-genai SDK). Swap this file to change the model
provider; core.py only needs extract() and answer().
"""
import json
import os
from datetime import datetime

import db

MODEL = os.getenv("PLUTO_MODEL", "gemini-2.5-flash")
_client = None


def _c():
    global _client
    if _client is None:
        from google import genai

        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _client = genai.Client(api_key=key)
    return _client


def _generate(system: str, user: str, json_mode: bool) -> str:
    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0.2,
        max_output_tokens=2048,
        response_mime_type="application/json" if json_mode else "text/plain",
    )
    resp = _c().models.generate_content(model=MODEL, contents=user, config=config)
    text = resp.text
    if not text:
        raise ValueError("empty response from model")
    return text


def _json_from(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON in model output")
    return json.loads(text[start : end + 1])


EXTRACT_SYSTEM = """You are the understanding layer of a personal memory assistant.
The user speaks or types in English, Hindi or Hinglish. Read their message and return JSON only.

Schema:
{
  "is_question": true/false,   // true if the user is ASKING about something they saved earlier
  "items": [
    {
      "kind": "idea" | "promise" | "task" | "note",
      "title": "short title in the user's own language, max 80 chars",
      "due_local": "YYYY-MM-DDTHH:MM" or null
    }
  ]
}

Rules:
- One message can hold several items. Split them.
- idea = something to maybe explore. promise = a commitment to someone. task = a thing to do.
  note = information to keep. Do NOT turn every casual idea into a task.
- due_local only if the user gave or clearly implied a time. Use the current local time given
  below to resolve words like "tomorrow" or "this weekend" (weekend -> Saturday 10:00).
  If no time was given, use null. Never invent a deadline.
- If the message is a question about past saved items, set is_question true and items [].
- Keep titles faithful to what was said. Do not add facts.
"""


def extract(text: str, now_local: datetime) -> dict:
    out = _generate(
        EXTRACT_SYSTEM,
        f"Current local time: {now_local:%A, %Y-%m-%d %H:%M}\n\nMessage:\n{text}",
        json_mode=True,
    )
    data = _json_from(out)
    data.setdefault("is_question", False)
    data.setdefault("items", [])
    return data


ANSWER_SYSTEM = """You answer a question using ONLY the saved items provided.
Reply in the same language as the question (Hindi, English or Hinglish), briefly.
Cite item ids like #12. If nothing in the items answers the question, say plainly that
you do not have it saved. Never invent or guess a memory."""


def _local(s: str, tz) -> str:
    return db.from_str(s).astimezone(tz).strftime("%Y-%m-%d %H:%M")


def format_items(items: list, tz) -> str:
    lines = []
    for r in items:
        due = f" | due {_local(r['due_at'], tz)}" if r["due_at"] else ""
        lines.append(
            f"#{r['id']} [{r['kind']}, {r['status']}] saved {_local(r['created_at'], tz)}{due}\n"
            f"  title: {r['title']}\n  original: {r['original_text']}"
        )
    return "\n".join(lines) if lines else "(nothing saved yet)"


def answer(question: str, items: list, now_local: datetime) -> str:
    context = format_items(items, now_local.tzinfo)
    return _generate(
        ANSWER_SYSTEM,
        f"Now: {now_local:%A, %Y-%m-%d %H:%M}\n\nSaved items:\n{context}\n\nQuestion: {question}",
        json_mode=False,
    ).strip()
