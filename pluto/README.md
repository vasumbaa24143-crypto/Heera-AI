# Pluto (codename)

A personal assistant on Telegram that remembers what you tell it and nudges you until it is done.

You chat with it like a smart assistant, by text or voice note, in English, Hindi or Hinglish:

- **Information is noted automatically.** "Rahul ka birthday 5 March ko hai" → 📝 noted.
- **To-dos and reminders are added when you ask.** "remind me to call Rahul kal 5 baje", "todo: renew passport, urgent". Pluto asks "kab?" if a reminder has no time. If you just mention something you need to do, it offers to add it.
- **Priority comes from your words.** "urgent", "zaroori", "jaldi" → 🔴 high; "no rush" → low.
- **Ask about your plans.** "What's on my plate today?", "is hafte kya hai?", "this month", "overall what do I need to do?", "what have we discussed about the YouTube idea?" It answers only from what you saved and said.
- **Tell it when things are done.** "Rahul ko deck bhej diya" → marks that item done.
- **Nudges** when something is due, or when an idea goes untouched.
- **Google Sheets (optional).** Send a link to a sheet and ask about it, and Pluto reads it. It can also keep a copy of your items in a sheet of your own. See "Google Sheets" below.

## Setup (about 10 minutes)

You need Python 3.11 or newer.

1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Message **@userinfobot** → copy your numeric user id.
3. Get at least one AI key: Gemini (aistudio.google.com/apikey), Groq (console.groq.com/keys), Z AI (z.ai) or NVIDIA Nemotron (build.nvidia.com → Get API Key). With more than one, Pluto falls back automatically when one is rate-limited or down. Voice notes work with your Groq or Gemini key (Sarvam is an optional extra).
4. Run:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in the values
python bot.py --check     # shows which keys are set (never their values)
python bot.py
```

5. Open your bot in Telegram, send `/start`, then say something like:
   - "YouTube pe tech news segment start karne ka idea hai"
   - "remind me kal shaam Rahul ko deck bhejna hai"
   - then ask: "aaj aur kal kya kya hai?" or "YouTube wala idea kya tha?"

## Try it without Telegram

```bash
python simulate.py --offline   # no keys, no network: simple built-in assistant
python simulate.py             # same, but with your AI keys from .env
```

It runs the same flow as the bot (`core.py`) with its own `simulate.db`. Buttons appear as
`/tap done:3`; `/advance 3d` moves the clock and `/nudges` shows what would be sent.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests use a fake assistant and fake Telegram objects, so they need no keys or network.

## Commands

`/list` open items · `/done 3` · `/snooze 3` (+1 day) · `/delete 3` (forever) · `/forget` (clear chat history) · `/model` (choose which AI thinks first) · `/export` (JSON download of items and chat)

## How reminders and nudges work

- Checked every minute while the bot is running.
- **A reminder you set always arrives at the time you asked for**, even at night, and doesn't count against any limit.
- If it's still open afterwards, Pluto nudges again every 6 hours (up to 5 times). Ideas you haven't touched get a nudge after 3 days, then every 3 days (up to 3 times).
- Those follow-up nudges wait until morning (no follow-ups 22:00–08:00, `QUIET_START/END` in `core.py`) and are capped at 5 per 24 hours.
- Done / Snooze / Drop buttons on every nudge.
- Your computer must be awake with `python bot.py` running for reminders to arrive. To get them when your Mac is asleep, deploy it (see below).

## Google Sheets (optional)

Pluto can read a Google Sheet you send it, and keep a copy of your items in a sheet of your own. Both go through one Google service account, so Pluto only reaches sheets you share with that account. Nothing runs until you set it up:

1. In [Google Cloud](https://console.cloud.google.com/), pick or create a project, then **APIs & Services → Library → Google Sheets API → Enable**.
2. **IAM & Admin → Service accounts → Create service account** (any name, e.g. `pluto`). Open it, then **Keys → Add key → Create new key → JSON**. A key file downloads. Keep it outside this folder (for example `~/.pluto/google-key.json`): anyone who has it can open every sheet you shared with the account.
3. In `pluto.env` set `GOOGLE_SERVICE_ACCOUNT_FILE=/full/path/to/that-file.json`. (On a host without files, such as Railway, set `GOOGLE_SERVICE_ACCOUNT_JSON` to the file's contents instead.)
4. Run `python bot.py --check`. It prints the service account's email address. In Google Sheets press **Share** on each sheet and share it with that address: **Viewer** for sheets you want Pluto to read, **Editor** for the log sheet.
5. For the item log, create a blank sheet, share it with that address as **Editor**, and put its address (or just the id between `/d/` and `/edit`) in `PLUTO_LOG_SHEET_ID`. Restart the bot.

**Reading a sheet.** Send the link and ask about it: "what's the total in this sheet? https://docs.google.com/spreadsheets/d/…". Pluto reads the first 200 rows and columns A to Z of the tab in the link (the first tab if the link names none) and answers from them only. A link you just share so Pluto remembers it ("budget sheet ka link hai …") is noted, not opened. Reading never changes the sheet. The rows go to the same AI providers as your chat, and they are not saved. Text inside a sheet is only data: what Pluto saves or marks done comes from your own message, so a sheet can't make it save, finish or open anything. `simulate.py` can read sheets too, but it never writes the log.

**The item log.** Pluto keeps a tab called `Pluto` in the log sheet with one row per item: id, saved, kind, title, due, priority, status. Within a minute of any change (a new item, done, `/delete`) it rewrites that tab from its own database, so the sheet is only ever a copy. Don't edit the `Pluto` tab (Pluto owns it and clears anything else in it): put your own notes on another tab. Your original messages are left out unless you set `PLUTO_LOG_ORIGINAL=1`, and API keys or tokens in a title are replaced with `[REDACTED_KEY]`. If Google can't be reached Pluto carries on, and tries again a few minutes later.

`/delete` removes the row on the next update, but Google keeps a version history of every sheet, so anyone who can open the sheet's history can still see what used to be there. If that matters to you, leave the log off.

## Rules built in

See `CLAUDE.md`. In short: your original message is stored untouched; if labelling fails it is still
saved as a plain note; recall answers only from saved items; only `ALLOWED_USER_ID` can use the bot;
keys live only in `.env`.

## Deploy (later)

`Procfile` has `worker: python bot.py` for Railway. Add the `.env` values as Railway variables,
and mount a volume for the database (set `PLUTO_DB` to a path on the volume, e.g. `/data/pluto.db`).
The bot must be running to nudge.

## Known limits

- Each message sends the last 100 chat messages and your open items (plus 50 recent closed ones) to the model. "What have we discussed" only reaches that far back in the chat; saved items are always included. Add search/embeddings when this gets slow or costly.
- Voice notes: Groq `whisper-large-v3` → Gemini → Sarvam `saarika:v2.5` (`STT_PROVIDERS` changes the order).
- Google Sheets: a question reads one tab (first 200 rows, columns A to Z). The item log rewrites its whole `Pluto` tab on every change, so it suits hundreds or a few thousand items.
- AI providers are tried in order Gemini → Groq → Z AI → NVIDIA Nemotron (`PLUTO_PROVIDERS` changes it, and `/model` in chat picks which one goes first; the rest stay as backup). Default models: `gemini-2.5-flash`, `llama-3.3-70b-versatile` (Groq), `glm-4.6` (Z AI), `nvidia/nemotron-3.5-super-vl-120b-a12b` (NVIDIA, with thinking off for quick replies; `NVIDIA_THINKING=1` turns it on); change `PLUTO_MODEL`, `GROQ_MODEL`, `ZAI_MODEL`, `NVIDIA_MODEL` if a provider retires one. Fallback models may phrase replies differently, but the code-side checks (what gets saved, never citing unknown items) apply to all of them.
