# Pluto — notes for Claude

Pluto is a Telegram personal assistant with a memory. The user chats with it like a
smart assistant (text or voice, in English, Hindi or Hinglish). It quietly notes useful
information, adds to-dos and reminders when asked, answers "what's on my plate
today / this week / this month / overall" and "what have we discussed about X", and
nudges until things are done. Read README.md for setup and behaviour.

## Product rules (do not break these)

1. **Never overwrite the original message.** `items.original_text` and `messages.text`
   are written once, on insert, and never updated. Titles, kinds, priorities and due
   dates are derived data; the user's own words are the source of truth. If a message
   is split into several items, each keeps the whole message; if an item came from a
   follow-up ("remind me to call Rahul" → "kal 5 baje"), it keeps both messages.
2. **Never invent a memory.** Answers come only from the user's saved items and their
   conversation with Pluto. If nothing matches, say so plainly. The assistant must never
   invent a deadline or a priority the user did not give or clearly imply. In code, a
   reply that cites an item id the user does not have is withheld (`core.chat`).
3. **Ask before any outside action.** Pluto only ever messages its own user. It must not
   contact other people, post, buy, or call outside services beyond labelling (Gemini)
   and transcription (Sarvam) without the user saying yes first. The same goes for
   anyone working on this repo: ask before pushing, deploying, creating repos, or
   sending anything to an external service.
4. **Keys only in `.env`.** API keys and tokens live in `.env` (local) or the host's
   environment variables (Railway). Never hard-code them, log them, put them in tests,
   or commit `.env`. `.env.example` lists the names with empty values.
5. **If labelling fails, save it as a plain note.** Any error or malformed output from
   the assistant means: store the message as a `note` with the original text and tell
   the user (they can /delete it). A thought is never dropped.
6. **Information is saved automatically; actions only when asked.** Facts, details and
   ideas the user shares are noted without asking. Tasks, promises and reminders are
   created only when the user asks ("remind me", "todo", "add task", …), so their
   category and priority are right. If the user only mentions something they need to
   do, Pluto offers ("Add it as a to-do?") instead of adding it. Small talk and
   questions are never saved. Priority (high/normal/low) comes only from the user's
   own words ("urgent", "zaroori", "no rush").

Also built in: only `ALLOWED_USER_ID` may use the bot (fail closed if unset); the
assistant can mark only the user's own open items done; `/delete` removes an item,
its nudge history and the chat messages it came from, for good; `/forget` clears the
chat history; `/export` returns items and conversation; no nudges 22:00–08:00 local;
at most 5 nudges per user per 24 hours.

## Layout

- `core.py` — the whole flow (`chat`, commands, buttons, time buckets, nudge
  selection). Transport-agnostic and synchronous. Put new behaviour here. The model
  proposes (`reply`, `save`, `done`); core checks and applies.
- `bot.py` — Telegram adapter only. `simulate.py` — terminal adapter, same flow.
- `db.py` — SQLite. Every time-dependent function takes an optional `now`.
- `llm.py` — Gemini (`respond`), including the assistant's system prompt.
  `offline.py` — rule-based stand-in for `simulate.py --offline`. Any object with
  `respond(message, history, items, now_local)` works as the assistant.
- `stt.py` — Sarvam speech-to-text.
- `tests/` — pytest with a fake assistant and fake Telegram objects; no network needed.

## Working on it

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                         # must pass before every commit
python simulate.py --offline   # try the flow in the terminal, no keys needed
```

- Every rule above has a test; add one when you add behaviour.
- Tests must not touch the network or real keys. Use `FakeLLM`/`says` and the fakes in
  `tests/test_bot.py`.
- Python 3.11+ (google-genai requires it).
