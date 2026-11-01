"""The assistant: reads one message in context and decides what to say and do.

Providers are tried in order until one gives a usable answer, so a rate limit,
outage or bad output from one model doesn't stop Pluto thinking:

    Gemini (GEMINI_API_KEY) -> Groq (GROQ_API_KEY) -> Z AI (ZAI_API_KEY)

Providers without a key are skipped. Set PLUTO_PROVIDERS (e.g. "groq,gemini")
to change the order or leave some out; in chat, /model picks which one goes
first for that user. core.py needs respond(), and choices()/label() for /model.
"""
import json
import logging
import os
from datetime import datetime

import httpx

log = logging.getLogger("pluto")

MODEL = os.getenv("PLUTO_MODEL", "gemini-2.5-flash")  # Gemini model
GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
ZAI_URL = os.getenv("ZAI_URL", "https://api.z.ai/api/paas/v4/chat/completions")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-4.6")
DEFAULT_ORDER = "gemini,groq,zai"
TIMEOUT = 45  # seconds per provider
_client = None


class AllProvidersFailed(RuntimeError):
    pass


def _key(name):
    if name == "zai":
        return os.getenv("ZAI_API_KEY") or os.getenv("Z_AI_API_KEY")
    return os.getenv({"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}[name])


def providers(prefer=None) -> list:
    """Provider names to try, in order: those listed in PLUTO_PROVIDERS that have a key.
    `prefer` (chosen with /model) moves one provider to the front; the rest stay as backup."""
    order = [p.strip().lower() for p in os.getenv("PLUTO_PROVIDERS", DEFAULT_ORDER).split(",")]
    names = [p for p in dict.fromkeys(order) if p in ("gemini", "groq", "zai") and _key(p)]
    if prefer in names:
        names.remove(prefer)
        names.insert(0, prefer)
    return names


def label(name) -> str:
    return {"gemini": f"Gemini ({MODEL})", "groq": f"Groq ({GROQ_MODEL})",
            "zai": f"Z AI ({ZAI_MODEL})"}[name]


def choices() -> list:
    """(name, label) for every provider that has a key, for the /model menu."""
    return [(n, label(n)) for n in providers()]


def describe(prefer=None) -> str:
    return " → ".join(label(p) for p in providers(prefer)) or "no provider configured"


def _c():
    global _client
    if _client is None:
        from google import genai

        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _client = genai.Client(api_key=key)
    return _client


def _gemini(system: str, user: str) -> str:
    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0.4,
        max_output_tokens=8192,  # room for the model's thinking plus the JSON
        response_mime_type="application/json",
    )
    resp = _c().models.generate_content(model=MODEL, contents=user, config=config)
    text = resp.text
    if not text:
        raise ValueError("empty response from model")
    return text


def _post(url, headers, payload):
    """One HTTP call; separated out so tests can replace it."""
    r = httpx.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _openai_style(url: str, key: str, model: str, system: str, user: str) -> str:
    """Groq and Z AI both speak the OpenAI chat-completions format."""
    data = _post(
        url,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.4,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        },
    )
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
    if not text:
        raise ValueError("empty response from model")
    return text


def _call(name: str, system: str, user: str) -> str:
    if name == "gemini":
        return _gemini(system, user)
    if name == "groq":
        return _openai_style(GROQ_URL, _key("groq"), GROQ_MODEL, system, user)
    return _openai_style(ZAI_URL, _key("zai"), ZAI_MODEL, system, user)


def _ask(system: str, user: str, prefer=None) -> dict:
    """Try each provider until one returns valid JSON."""
    names = providers(prefer)
    if not names:
        raise AllProvidersFailed("no AI provider key set (GEMINI_API_KEY, GROQ_API_KEY or ZAI_API_KEY)")
    errors = []
    for name in names:
        try:
            data = _json_from(_call(name, system, user))
        except Exception as e:  # rate limit, outage, timeout, bad JSON: try the next one
            # Log only the error type and status, never request headers (they hold the key).
            status = getattr(getattr(e, "response", None), "status_code", None)
            log.warning("%s failed (%s%s); trying next provider",
                        name, type(e).__name__, f" {status}" if status else "")
            errors.append(f"{name}: {type(e).__name__}")
            continue
        if name != names[0]:
            log.info("answered by fallback provider %s", name)
        return data
    raise AllProvidersFailed("all providers failed: " + ", ".join(errors))


def _json_from(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON in model output")
    return json.loads(text[start : end + 1])


SYSTEM = """You are Pluto, the user's personal assistant on Telegram. You are warm, sharp and brief,
like a capable human assistant who knows their life. The user writes in English, Hindi or Hinglish
(Hindi in Latin script). Always reply in the same language and script they used.

Return a single JSON object. You are given: the current local time, the recent conversation, the user's saved items, and their
new message. Return JSON only:

{
  "reply": "what you say back",
  "save": [
    {"kind": "idea" | "promise" | "task" | "note",
     "title": "short title in the user's own words, max 80 chars",
     "due_local": "YYYY-MM-DDTHH:MM" or null,
     "priority": "high" | "normal" | "low",
     "uses_previous_message": true/false}
  ],
  "done": [item ids the user just told you are finished]
}

WHEN TO SAVE
- INFORMATION is saved automatically, without being asked: facts, details, numbers, names,
  addresses, decisions, plans, preferences, things that happened, and ideas the user shares
  ("Rahul's birthday is 5 March", "wifi password drawer mein hai", "YouTube tech news ka idea hai").
  Save it as kind "note", or "idea" for something to maybe explore. Don't ask first.
- ACTIONS are saved ONLY when the user asks for them: "remind me", "yaad dila dena", "todo",
  "to-do", "add task", "add to my list", "don't let me forget", "track this promise", or similar.
  Then use kind "task" (something to do) or "promise" (a commitment to someone).
  If the user just mentions something they need to do ("mujhe bank jana hai") without asking,
  do NOT save it as a task. Offer instead, in one short line: "Add it as a to-do?". If they say
  yes, save it then (uses_previous_message true).
- Greetings, small talk, thanks, opinions, venting and questions are never saved.
- One message can hold several things. Save each one separately.
- priority (tasks and promises only): "high" if the user says urgent, important, ASAP, zaroori,
  jaldi, top priority, or similar; "low" if they say low priority, whenever, no rush, koi jaldi
  nahi; otherwise "normal". Never guess a priority they didn't express. Use "normal" for notes.
- A reminder needs a time. If they ask to be reminded but gave no time, do not save yet: ask one
  short question (e.g. "Kab yaad dilaun?") and save when they answer. Then set
  uses_previous_message true, and build the title from the earlier message. If they say there is
  no particular time, save it with due_local null. A plain to-do doesn't need a time.
- due_local: resolve "kal", "tomorrow", "parso", "next Monday", "this weekend" (Saturday 10:00),
  "shaam" (18:00), "subah" (09:00) from the current local time. Never invent a time they didn't
  give or clearly imply.
- Do not say "saved" or quote item ids for new items in your reply: the app appends a confirmation
  with the real id. Just acknowledge naturally and briefly (e.g. "Pakka 👍" /
  "Got it."). If all you did was note information, a very short acknowledgement is enough.

ANSWERING QUESTIONS ABOUT THEIR STUFF
- Use ONLY the saved items and the conversation given. Each open item has a "bucket" already
  worked out for you: overdue, today, tomorrow, this week, this month, later, or no date.
  - Order by urgency: overdue first, then by due time, and high-priority items before others
    in the same bucket. Mark high-priority items with 🔴.
  - "What's on my plate today / aaj kya hai": overdue + today, plus open high-priority to-dos
    with no date.
  - "This week": overdue, today, tomorrow and this week. "This month": also this month.
  - "Overall / sab kuch / what do I need to do": every open item, grouped sensibly.
  - Items with no date: mention them briefly when asked about overall plans, or if relevant.
- "What have we discussed about X": look through the items and the conversation and summarise
  what was said, with dates if useful.
- Cite saved items as #id. Only cite ids that appear in the saved items.
- If nothing matches, say so plainly. Never invent, guess or embellish a memory, a date, or an item.
- For lists, use short lines, one item per line, most urgent first.

MARKING THINGS DONE
- If the user says they finished something ("Rahul ko deck bhej diya", "paid the rent"), put the id
  in "done" only if it clearly matches exactly one open item, and say so in your reply. If it is
  unclear which item they mean, ask instead.

Keep replies short: one to four sentences, or a compact list. No markdown headings or tables.
"""


def format_context(history: list, items: list) -> str:
    convo = "\n".join(
        f"[{m['when']}] {'User' if m['role'] == 'user' else 'Pluto'}: {m['text']}" for m in history
    ) or "(no earlier conversation)"
    lines = []
    for it in items:
        due = f" | due {it['due_local']}" if it["due_local"] else ""
        prio = f", {it['priority']} priority" if it["kind"] in ("task", "promise") else ""
        lines.append(
            f"#{it['id']} [{it['kind']}{prio}, {it['bucket']}] saved {it['saved_local']}{due}\n"
            f"  title: {it['title']}\n  original: {it['original_text']}"
        )
    saved = "\n".join(lines) or "(nothing saved yet)"
    return f"Recent conversation:\n{convo}\n\nSaved items:\n{saved}"


def respond(message: str, history: list, items: list, now_local: datetime, prefer=None) -> dict:
    data = _ask(
        SYSTEM,
        f"Current local time: {now_local:%A, %Y-%m-%d %H:%M}\n\n"
        f"{format_context(history, items)}\n\nNew message from the user:\n{message}",
        prefer=prefer,
    )
    if not isinstance(data, dict):
        raise ValueError("assistant did not return an object")
    data.setdefault("reply", "")
    data.setdefault("save", [])
    data.setdefault("done", [])
    return data
