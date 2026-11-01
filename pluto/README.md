# Pluto (codename) — first prototype

A Telegram bot that remembers what you tell it and nudges you until it is done.

**Loop:** you type or speak → Claude labels it (idea / promise / task / note) → saved in SQLite → you ask about it later → nudges when due or when an idea goes stale.

## Setup (about 10 minutes)

1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Message **@userinfobot** → copy your numeric user id.
3. Get an Anthropic API key (console.anthropic.com) and, for voice, a Sarvam key (dashboard.sarvam.ai).
4. Run:

```bash
cd pluto
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in the four values
python bot.py
```

5. Open your bot in Telegram, send `/start`, then say something like:
   - "kal shaam ko Rahul ko deck bhejna hai"
   - "YouTube pe tech news segment start karne ka idea hai"
   - then ask: "YouTube wala idea kya tha?"

## Commands

`/list` open items · `/done 3` · `/snooze 3` (+1 day) · `/delete 3` · `/export` (JSON download)

## How nudges work

- Checked every 30 minutes, never between 22:00 and 08:00 (`QUIET_START/END` in `bot.py`).
- Overdue tasks: every 6 hours, up to 5 times. Ideas with no deadline: after 3 days, then every 3 days, up to 3 times.
- Max 5 nudges per 24 hours. Done / Snooze / Drop buttons on every nudge.

## Rules built in

- Your original message is stored untouched in `original_text`.
- If labelling fails, the message is still saved as a plain note.
- Recall answers only from saved items; if it is not saved, it says so.
- Only `ALLOWED_USER_ID` can use the bot. `/delete` and `/export` are one command away.

## Known limits (prototype)

- Recall sends your recent 300 items to the model; add embeddings only when this gets slow.
- The bot must be running to nudge. For 24/7, deploy to Railway or Render (start command `python bot.py`, add the env vars, and mount a volume for `pluto.db`).
- Check `stt.py` against Sarvam's current docs; the model name and field names are env-configurable.
