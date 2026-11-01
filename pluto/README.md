# Pluto (codename) — phase 1

A Telegram bot that remembers what you tell it and nudges you until it is done.

**Loop:** you type or speak → Gemini labels it (idea / promise / task / note) → saved in SQLite → you ask about it later → nudges when due or when an idea goes stale.

## Setup (about 10 minutes)

You need Python 3.11 or newer.

1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Message **@userinfobot** → copy your numeric user id.
3. Get a Gemini API key (aistudio.google.com/apikey) and, for voice, a Sarvam key (dashboard.sarvam.ai).
4. Run:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in the values
python bot.py
```

5. Open your bot in Telegram, send `/start`, then say something like:
   - "kal shaam ko Rahul ko deck bhejna hai"
   - "YouTube pe tech news segment start karne ka idea hai"
   - then ask: "YouTube wala idea kya tha?"

## Try it without Telegram

```bash
python simulate.py --offline   # no keys, no network: simple built-in labeller
python simulate.py             # same, but labels with Gemini (needs GEMINI_API_KEY)
```

It runs the same flow as the bot (`core.py`) with its own `simulate.db`. Buttons appear as
`/tap done:3`; `/advance 3d` moves the clock and `/nudges` shows what would be sent.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests use a fake labeller and fake Telegram objects, so they need no keys or network.

## Commands

`/list` open items · `/done 3` · `/snooze 3` (+1 day) · `/delete 3` (forever) · `/export` (JSON download)

## How nudges work

- Checked every 30 minutes, never between 22:00 and 08:00 (`QUIET_START/END` in `core.py`).
- Overdue items: every 6 hours, up to 5 times. Ideas with no deadline: after 3 days, then every 3 days, up to 3 times.
- Max 5 nudges per 24 hours. Overdue items go first, and items nudged least go first, so nothing is starved.
- Done / Snooze / Drop buttons on every nudge.

## Rules built in

See `CLAUDE.md`. In short: your original message is stored untouched; if labelling fails it is still
saved as a plain note; recall answers only from saved items; only `ALLOWED_USER_ID` can use the bot;
keys live only in `.env`.

## Deploy (later)

`Procfile` has `worker: python bot.py` for Railway. Add the `.env` values as Railway variables,
and mount a volume for the database (set `PLUTO_DB` to a path on the volume, e.g. `/data/pluto.db`).
The bot must be running to nudge.

## Known limits

- Recall sends your recent 300 items to the model; add embeddings only when this gets slow.
- Check `stt.py` against Sarvam's current docs; the model name and field names are env-configurable.
- The Gemini model defaults to `gemini-2.5-flash`; change `PLUTO_MODEL` if Google retires it.
