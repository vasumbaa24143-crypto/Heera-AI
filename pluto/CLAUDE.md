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
2. **Never invent a memory.** Answers come only from the user's saved items, their
   conversation with Pluto and a Google Sheet they sent a link to. If nothing matches,
   say so plainly. The assistant must never
   invent a deadline or a priority the user did not give or clearly imply. In code, a
   reply that cites an item id the user does not have is withheld (`core.chat`).
3. **Ask before any outside action.** Pluto only ever messages its own user. It must not
   contact other people, post, buy, or call outside services beyond the AI providers
   (Gemini, Groq, Z AI, NVIDIA), transcription (Sarvam) and Google Sheets (only through Pluto's
   own service account, see `sheets.py`) without the user saying yes first. The same goes for
   anyone working on this repo: ask before pushing, deploying, creating repos, or
   sending anything to an external service.
4. **Keys only in `.env`.** API keys and tokens live in `.env` (local) or the host's
   environment variables (Railway). Never hard-code them, log them, put them in tests,
   or commit `.env`. `.env.example` lists the names with empty values; add any new
   key there and to `bot.KEY_REPORT` (a test checks they match).
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
chat history; `/model` picks which AI provider goes first (saved per user, others
stay as backup); `/export` returns items and conversation. A reminder's first alert
always goes out at the time the user set (checked every minute), even at night and
past the cap; repeat nudges and idea nudges wait out quiet hours (22:00–08:00) and are
capped at 5 per user per 24 hours. The morning digest (`core.due_digests`, `/digest`) is
built from data with no AI call. It's sent at most once per local day, from the user's
digest time until 4 h later, and skipped when there's nothing overdue, nothing due today
and no idea to revisit. With the digest on, stale ideas appear only there, not as
separate nudges. Digest items are marked shown with `db.touch_nudged`, which doesn't
use the daily cap.

Google Sheets (`sheets.py`) is optional and stays off until `GOOGLE_SERVICE_ACCOUNT_FILE` is set.
The assistant may ask to read a sheet (`read`), but core opens it only if the user sent that link;
what it reads is data and is never saved. What to save or mark done comes only from the first
answer, which never saw the sheet (the answer that did only supplies the words), so a hostile
sheet can't make Pluto save, finish or open anything. `PLUTO_LOG_SHEET_ID` also turns on a copy of the items in a tab called `Pluto`: the database
stays the source of truth, the tab is rewritten whenever something changed, `/delete` removes the
row (Google's version history may still hold it), and original messages stay out unless
`PLUTO_LOG_ORIGINAL=1`. The Google key file is a secret like the other keys: keep it outside the repo.

## Layout

- `core.py` — the whole flow (`chat`, commands, buttons, time buckets, nudge
  selection, morning digest). Transport-agnostic and synchronous. Put new behaviour here. The model
  proposes (`reply`, `save`, `done`, `read`); core checks and applies.
- `bot.py` — Telegram adapter only. `simulate.py` — terminal adapter, same flow.
- `db.py` — SQLite. Every time-dependent function takes an optional `now`. `db.on_change` is
  called when an item is added, finished or deleted (the bot uses it for the Sheets log). Full-text
  search: external-content FTS5 indexes `items_fts` and `messages_fts`, kept in step by
  triggers (so `/delete` and `/forget` also clear search), `unicode61` without Porter
  stemming (Hinglish), `search_terms()` drops English/Hinglish filler words, and every word is
  quoted so user text can't break FTS5 syntax. If FTS5 is missing (`db.HAS_FTS = False`),
  search falls back to `LIKE`. `core.chat` sends open items + recent closed + search hits
  (items and older messages) instead of the whole history.
- `sheets.py` — Google Sheets: read a linked sheet, and keep the item log tab. Every Google call
  goes through `_call` and `_token`, so tests replace them.
- `llm.py` — the assistant (`respond`) and its system prompt. Tries Gemini → Groq →
  Z AI → NVIDIA Nemotron until one returns valid JSON (`PLUTO_PROVIDERS` sets the order); if all fail,
  core saves the raw note (rule 5).
  `offline.py` — rule-based stand-in for `simulate.py --offline`. Any object with
  `respond(message, history, items, now_local, prefer=None, related=())` works as the assistant.
- `stt.py` — voice-to-text: Groq Whisper → Gemini → Sarvam, whichever keys exist.
- `tests/` — pytest with a fake assistant and fake Telegram objects; no network needed.

## Working on it

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                         # must pass before every commit
python simulate.py --offline   # try the flow in the terminal, no keys needed
python bot.py --check          # which keys are set in .env (never prints values)
```

- Every rule above has a test; add one when you add behaviour.
- Tests must not touch the network or real keys. Use `FakeLLM`/`says` and the fakes in
  `tests/test_bot.py`; `tests/conftest.py` clears the Google settings and blocks Google
  sign-in, and `tests/test_sheets.py` has a `FakeGoogle`.
- Python 3.11+ (google-genai requires it).
