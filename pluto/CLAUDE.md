# Pluto — notes for Claude

Pluto is a Telegram memory assistant: the user tells it things (text or voice, in
English, Hindi or Hinglish), it labels and saves them, answers questions about them
later, and nudges until things are done. Read README.md for setup and behaviour.

## Product rules (do not break these)

1. **Never overwrite the original message.** `items.original_text` is written once, on
   insert, and never updated. Titles, kinds and due dates are derived data; the user's
   own words are the source of truth. If a message is split into several items, each
   item keeps the whole original message.
2. **Never invent a memory.** Recall answers come only from the user's saved items. If
   nothing matches, say so plainly. If nothing is saved, do not call the model at all.
   The labeller must never invent a deadline the user did not give or clearly imply.
3. **Ask before any outside action.** Pluto only ever messages its own user. It must not
   contact other people, post, buy, or call outside services beyond labelling (Gemini)
   and transcription (Sarvam) without the user saying yes first. The same goes for
   anyone working on this repo: ask before pushing, deploying, creating repos, or
   sending anything to an external service.
4. **Keys only in `.env`.** API keys and tokens live in `.env` (local) or the host's
   environment variables (Railway). Never hard-code them, log them, put them in tests,
   or commit `.env`. `.env.example` lists the names with empty values.
5. **If labelling fails, save it as a plain note.** Any error or malformed output from
   the labeller means: store the message as a `note` with the original text, tell the
   user it was saved unlabelled. A thought is never dropped.

Also built in: only `ALLOWED_USER_ID` may use the bot (fail closed if unset); `/delete`
removes an item and its nudge history for good; `/export` returns everything; no
nudges 22:00–08:00 local; at most 5 nudges per user per 24 hours.

## Layout

- `core.py` — the whole flow (capture, recall, commands, buttons, nudge selection).
  Transport-agnostic and synchronous. Put new behaviour here.
- `bot.py` — Telegram adapter only. `simulate.py` — terminal adapter, same flow.
- `db.py` — SQLite. Every time-dependent function takes an optional `now`.
- `llm.py` — Gemini (`extract`, `answer`). `offline.py` — rule-based stand-in for
  `simulate.py --offline`. Any object with `extract`/`answer` works as the labeller.
- `stt.py` — Sarvam speech-to-text.
- `tests/` — pytest with a fake labeller and fake Telegram objects; no network needed.

## Working on it

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                         # must pass before every commit
python simulate.py --offline   # try the flow in the terminal, no keys needed
```

- Every rule above has a test; add one when you add behaviour.
- Tests must not touch the network or real keys. Use `FakeLLM` and the fakes in
  `tests/test_bot.py`.
- Python 3.11+ (google-genai requires it).
