"""Understanding layer: label a message, and answer recall questions.

Uses the Anthropic API. Swap this file to change the model provider.
"""
import json
import os
from datetime import datetime

from anthropic import Anthropic

MODEL = os.getenv("PLUTO_MODEL", "claude-haiku-5-5")
_client = None


def _c() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


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
    msg = _c().messages.create(
        model=MODEL,
        max_tokens=800,
        system=EXTRACT_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"Current local time: {now_local:%A, %Y-%m-%d %H:%M}\n\nMessage:\n{text}",
            }
        ],
    )
    data = _json_from(msg.content[0].text)
    data.setdefault("is_question", False)
    data.setdefault("items", [])
    return data


ANSWER_SYSTEM = """You answer a question using ONLY the saved items provided.
Reply in the same language as the question (Hindi, English or Hinglish), briefly.
Cite item ids like #12. If nothing in the items answers the question, say plainly that
you do not have it saved. Never invent or guess a memory."""


def answer(question: str, items: list, now_local: datetime) -> str:
    lines = []
    for r in items:
        due = f" | due {r['due_at']}" if r["due_at"] else ""
        lines.append(
            f"#{r['id']} [{r['kind']}, {r['status']}] saved {r['created_at']}{due}\n"
            f"  title: {r['title']}\n  original: {r['original_text']}"
        )
    context = "\n".join(lines) if lines else "(nothing saved yet)"
    msg = _c().messages.create(
        model=MODEL,
        max_tokens=600,
        system=ANSWER_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"Now: {now_local:%A, %Y-%m-%d %H:%M}\n\nSaved items:\n{context}\n\nQuestion: {question}",
            }
        ],
    )
    return msg.content[0].text.strip()
