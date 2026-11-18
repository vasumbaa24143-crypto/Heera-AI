"""A rough, rule-based stand-in for llm.py, for `python simulate.py --offline`.

It needs no network or API key. It is far dumber than Gemini (keyword rules,
English/Hinglish only) but follows the same rules: information is noted
automatically, to-dos and reminders only when asked, answers come only from
saved items and the conversation, and it never makes up a memory.
"""
import re
from datetime import datetime, timedelta

GREETINGS = re.compile(
    r"^(hi+|hello|hey|namaste|namaskar|good (morning|afternoon|evening|night)|gm|thanks|thank you"
    r"|thx|ok(ay)?|cool|great|nice|shukriya|dhanyavaad|bye)\b[\s!.🙂]*$",
    re.I,
)
ACTION = re.compile(
    r"(remind me|yaad dila|don'?t let me forget|^to-?do\b|\bto-?do:|add (a )?task|^task\b"
    r"|add to (my )?list|track this)",
    re.I,
)
NOTE = re.compile(r"(make a note|note (this|kar|karo|kar lo)|^note\b|save this|likh lo)", re.I)
NEEDS_DOING = re.compile(r"\b(need to|have to|must|karna hai|jana hai|bhejna hai|dena hai)\b", re.I)
HIGH = re.compile(r"\b(urgent|important|asap|zaroori|jaldi|top priority)\b", re.I)
LOW = re.compile(r"\b(low priority|whenever|no rush|koi jaldi nahi)\b", re.I)
YES = re.compile(r"^(yes|yeah|yep|haan|ha|ok|okay|sure|add it|kar do)\b", re.I)
OFFER = "Add it as a to-do?"
REMIND = re.compile(r"(remind me|yaad dila|don'?t let me forget)", re.I)
TIME_WORDS = re.compile(r"\b(tomorrow|kal|today|aaj|tonight|\d{1,2}\s*(baje|am|pm))\b", re.I)
ASK_WHEN = "Kab yaad dilaun? (e.g. “kal 10 baje”, or “no time”)"
DONE = re.compile(r"(done|ho gaya|kar diya|finished|completed).*#(\d+)|#(\d+).*(done|ho gaya)", re.I)
QUESTION_START = (
    "what", "which", "when", "where", "who", "did", "do i", "have i", "show", "list",
    "kya", "kab", "kaun", "kahan", "kitne",
)
STOPWORDS = {
    "what", "was", "that", "the", "a", "an", "is", "i", "my", "me", "to", "of", "did", "do",
    "kya", "tha", "thi", "wala", "wali", "hai", "ka", "ki", "ke", "about", "which", "when",
    "have", "we", "discussed", "said", "there", "on", "for", "and",
}


def _due(t: str, now_local: datetime):
    if re.search(r"\b(tomorrow|kal)\b", t):
        day = now_local + timedelta(days=1)
    elif re.search(r"\b(today|aaj|tonight)\b", t):
        day = now_local
    else:
        return None
    m = re.search(r"\b(\d{1,2})\s*(baje|am|pm)\b", t)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(2) == "pm" else 0)
        if m.group(2) == "baje" and hour < 8:
            hour += 12  # "5 baje" usually means 17:00
    else:
        hour = 18 if re.search(r"evening|shaam|tonight", t) else 10
    return day.replace(hour=hour, minute=0).strftime("%Y-%m-%dT%H:%M")


def _action_kind(t: str) -> str:
    return "promise" if re.search(r"promise|promised|vaada|wada", t) else "task"


def _priority(t: str) -> str:
    return "low" if LOW.search(t) else "high" if HIGH.search(t) else "normal"


def _title(text: str) -> str:
    t = re.sub(ACTION, "", text, count=1)
    t = re.sub(NOTE, "", t, count=1)
    t = re.sub(r",?\s*\b(urgent|asap|low priority|no rush)\b", "", t, flags=re.I)
    t = re.sub(r"^(to|ki|ke|ko)\s+", "", t.strip(" :,-."), flags=re.I)
    return (t or text)[:80]


def _words(s: str) -> set:
    return {w for w in re.findall(r"\w+", s.lower()) if w not in STOPWORDS and len(w) > 2}


def _agenda(t: str, items: list):
    if re.search(r"\b(today|aaj|plate)\b", t):
        want, label = {"overdue", "today"}, "today"
    elif re.search(r"\b(week|hafte|hafta)\b", t):
        want, label = {"overdue", "today", "tomorrow", "this week"}, "this week"
    elif re.search(r"\b(month|mahine)\b", t):
        want, label = {"overdue", "today", "tomorrow", "this week", "this month"}, "this month"
    elif re.search(r"\b(overall|everything|sab|all|need to do|pending)\b", t):
        want, label = None, "overall"
    else:
        return None
    order = ["overdue", "today", "tomorrow", "this week", "this month", "later", "no date"]
    picked = [
        i for i in items
        if i["status"] == "open" and (
            want is None or i["bucket"] in want
            or (label == "today" and i["bucket"] == "no date" and i["priority"] == "high")
        )
    ]
    if not picked:
        return f"Nothing on your plate for {label}. 🎉"
    rank = {"high": 0, "normal": 1, "low": 2}
    picked.sort(key=lambda i: (order.index(i["bucket"]), rank.get(i["priority"], 1), i["due_local"] or ""))
    return f"Here's {label}:\n" + "\n".join(
        f"• #{i['id']} {'🔴 ' if i['priority'] == 'high' else ''}{i['title']}"
        f" ({i['bucket']}{', ' + i['due_local'] if i['due_local'] else ''})"
        for i in picked
    )


def choices() -> list:
    return []  # the offline stand-in has no models to choose from


def summarise(previous: str, messages: list, now_local: datetime, prefer=None) -> list:
    """Rough stand-in for llm.summarise: keeps the person's substantive messages as lines."""
    lines = [ln for ln in (previous or "").splitlines() if ln.strip()]
    for m in messages:
        text = m["text"].strip()
        if m["role"] == "user" and len(_words(text)) >= 2 and not GREETINGS.match(text.lower()):
            lines.append(f"- {text[:100]} ({m['when'][4:14]})")
    return lines[-15:]


def consolidate(facts: list, summary: str, notes: list, now_local: datetime, prefer=None) -> dict:
    """Rough stand-in for llm.consolidate: summary lines and notes that mention a person or
    a preference become facts, if not already there."""
    have = {f["text"].lower() for f in facts}
    cues = re.compile(r"\b(is my|my (wife|husband|mom|mother|dad|father|friend|boss|colleague|brother|sister)"
                      r"|colleague|friend|birthday|prefer|like|love|hate|always|never|live|work)\b", re.I)
    lines = [ln.lstrip("-• ").rsplit(" (", 1)[0] for ln in (summary or "").splitlines()]
    lines += [n["title"] for n in notes]
    add = []
    for ln in lines:
        if ln and cues.search(ln) and ln.lower() not in have:
            have.add(ln.lower())
            add.append({"text": ln[:200], "mark": "permanent" if "birthday" in ln.lower() else "durable"})
    return {"add": add[:10], "update": [], "retire": []}


def respond(message: str, history: list, items: list, now_local: datetime, prefer=None,
            related=(), summary="", facts="") -> dict:
    text = message.strip()
    t = text.lower()
    last_pluto = next((m["text"] for m in reversed(history) if m["role"] == "pluto"), "")
    prev_user = next((m["text"] for m in reversed(history) if m["role"] == "user"), "")

    # Answer to "Kab yaad dilaun?"
    if last_pluto.startswith(ASK_WHEN.split("?")[0]) and prev_user:
        no_time = re.search(r"\b(no time|koi time nahi|anytime)\b", t)
        due = None if no_time else _due(t, now_local) or _due("kal " + t, now_local)
        return {
            "reply": "Pakka, I'll remind you." if due else "Okay, saved without a time.",
            "save": [{"kind": "task", "title": _title(prev_user), "due_local": due,
                      "priority": _priority(prev_user.lower()), "uses_previous_message": True}],
            "done": [],
        }

    # "yes" to "Add it as a to-do?"
    if last_pluto.endswith(OFFER) and prev_user and YES.match(t):
        p = prev_user.lower()
        return {
            "reply": "Done 👍",
            "save": [{"kind": _action_kind(p), "title": _title(prev_user), "due_local": _due(p, now_local),
                      "priority": _priority(p), "uses_previous_message": True}],
            "done": [],
        }

    m = DONE.search(t)
    if m:
        return {"reply": "Nice, that's off your list.", "save": [], "done": [int(m.group(2) or m.group(3))]}

    if ACTION.search(t):
        if REMIND.search(t) and not TIME_WORDS.search(t):
            return {"reply": ASK_WHEN, "save": [], "done": []}
        item = {"kind": _action_kind(t), "title": _title(text), "due_local": _due(t, now_local),
                "priority": _priority(t), "uses_previous_message": False}
        return {"reply": "Got it 👍", "save": [item], "done": []}

    if NOTE.search(t):
        item = {"kind": "idea" if "idea" in t else "note", "title": _title(text), "due_local": None,
                "priority": "normal", "uses_previous_message": False}
        return {"reply": "", "save": [item], "done": []}

    if GREETINGS.match(t):
        hour = now_local.hour
        part = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
        if re.match(r"(thanks|thank you|thx|shukriya|dhanyavaad)", t):
            return {"reply": "Anytime! 🙂", "save": [], "done": []}
        return {"reply": f"Good {part}! What can I do for you?", "save": [], "done": []}

    agenda = _agenda(t, items)
    if agenda:
        return {"reply": agenda, "save": [], "done": []}

    if t.endswith("?") or t.startswith(QUESTION_START):
        q = _words(text)
        hits = [i for i in items if q & _words(i["title"] + " " + i["original_text"])]
        said = [m for m in [*related, *history] if m["role"] == "user" and q & _words(m["text"])]
        if not hits and not said:
            return {"reply": "I don't have anything about that saved.", "save": [], "done": []}
        lines = [f"#{i['id']} {i['title']} ({i['bucket']})" for i in hits[-5:]]
        lines += [f"{m['when']}: “{m['text']}”" for m in said[-3:] if m["text"] != text]
        return {"reply": "Here's what I have:\n" + "\n".join(lines), "save": [], "done": []}

    if NEEDS_DOING.search(t):
        return {"reply": OFFER, "save": [], "done": []}

    # Anything else with some substance is information: note it.
    if len(_words(text)) >= 2:
        item = {"kind": "idea" if "idea" in t else "note", "title": text[:80], "due_local": None,
                "priority": "normal", "uses_previous_message": False}
        return {"reply": "", "save": [item], "done": []}
    return {"reply": "Hmm?", "save": [], "done": []}
