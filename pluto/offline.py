"""A rough, rule-based stand-in for llm.py, for `python simulate.py --offline`.

It needs no network or API key. It is much dumber than Gemini (keyword rules,
English/Hinglish only) but follows the same rules: recall answers come only
from saved items, and it never makes up a memory.
"""
import re
from datetime import datetime, timedelta

QUESTION_START = (
    "what", "which", "when", "where", "who", "did", "do i", "have i", "show",
    "kya", "kab", "kaun", "kahan", "kitne",
)
STOPWORDS = {
    "what", "was", "that", "the", "a", "an", "is", "i", "my", "me", "to", "of", "did", "do",
    "kya", "tha", "thi", "wala", "wali", "hai", "ka", "ki", "ke", "about", "which", "when",
}


def _is_question(t: str) -> bool:
    return t.endswith("?") or t.startswith(QUESTION_START) or " kya tha" in t or " kya thi" in t


def _kind(t: str) -> str:
    if "idea" in t:
        return "idea"
    if "promise" in t or "promised" in t or "vaada" in t or "wada" in t:
        return "promise"
    if re.search(r"\b(need to|have to|must|todo|remind me|karna hai|bhejna hai|call)\b", t):
        return "task"
    return "note"


def _due(t: str, now_local: datetime):
    if "tomorrow" in t or re.search(r"\bkal\b", t):
        day = now_local + timedelta(days=1)
    elif "today" in t or re.search(r"\baaj\b", t):
        day = now_local
    else:
        return None
    hour = 18 if re.search(r"evening|shaam|tonight", t) else 10
    return day.replace(hour=hour, minute=0).strftime("%Y-%m-%dT%H:%M")


def extract(text: str, now_local: datetime) -> dict:
    t = text.strip().lower()
    if _is_question(t):
        return {"is_question": True, "items": []}
    return {
        "is_question": False,
        "items": [{"kind": _kind(t), "title": text.strip()[:80], "due_local": _due(t, now_local)}],
    }


def _words(s: str) -> set:
    return {w for w in re.findall(r"\w+", s.lower()) if w not in STOPWORDS and len(w) > 2}


def answer(question: str, items: list, now_local: datetime) -> str:
    q = _words(question)
    scored = []
    for r in items:
        score = len(q & _words(r["title"] + " " + r["original_text"]))
        if score:
            scored.append((score, r["id"], r))
    if not scored:
        return "I don't have that saved."
    scored.sort(key=lambda s: (-s[0], -s[1]))
    return "\n".join(f"#{r['id']} {r['title']} ({r['status']})" for _, _, r in scored[:3])
