# Pluto (codename)

A personal assistant on Telegram that remembers what you tell it and nudges you until it is done.

You chat with it like a smart assistant, by text or voice note, in English, Hindi or Hinglish:

- **Information is noted automatically.** "Rahul ka birthday 5 March ko hai" → 📝 noted.
- **To-dos and reminders are added when you ask.** "remind me to call Rahul kal 5 baje", "todo: renew passport, urgent". Pluto asks "kab?" if a reminder has no time. If you just mention something you need to do, it offers to add it.
- **Priority comes from your words.** "urgent", "zaroori", "jaldi" → 🔴 high; "no rush" → low.
- **Ask about your plans.** "What's on my plate today?", "is hafte kya hai?", "this month", "overall what do I need to do?", "what have we discussed about the YouTube idea?" It answers only from what you saved and said.
- **Tell it when things are done.** "Rahul ko deck bhej diya" → marks that item done.
- **Reminders on time**, follow-up nudges if something stays open, and a **morning digest**.

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

`/list` open items · `/done 3` · `/snooze 3` (+1 day) · `/delete 3` (forever) · `/summary` (what Pluto remembers from older chats) · `/sheet` (Google Sheet dashboard; `/sheet now` updates it) · `/forget` (clear chat history and its summary) · `/model` (choose which AI thinks first) · `/digest` (morning digest: `now` to preview, `on`/`off`, or a time like `07:45`) · `/export` (JSON download of items and chat)

## Morning digest

One message each morning (08:30 by default, in `PLUTO_TZ`):

```
☀️ Good morning! Here's your day (Tue 03 Nov):

⚠️ Overdue (2)
• #14 🔴 Renew passport (was due Sun 01 Nov, 10:00)
• #9 Send deck to Rahul (was due Mon 02 Nov, 18:00)

📅 Today (1)
• #21 Call bank — 15:00

💡 From a while back
• #4 YouTube tech news segment (you told me 12 days ago). Still interested?
```

- Built straight from your saved items, with no AI call, so it costs nothing and can't invent anything.
- **Skipped when there's nothing to say.** Never more than one a day, and nothing until you've saved something.
- Up to 5 items per section ("…and N more — /list"). It has ✅ buttons for the items and a 🗄 Drop button for the idea.
- One old idea a day: at least a week old and not shown in the last week, taking turns.
- If the bot was off at digest time, it catches up within 4 hours; later than that, it skips the day.
- Items due later today still get their own reminder at their time. Overdue items shown in the digest don't get a repeat nudge right after it.
- With the digest on, old ideas come up only in the digest. With `/digest off`, they get their own nudges as before.

## Google Sheet dashboard (optional)

Pluto can keep a Google Sheet up to date with four tabs:
- **Dashboard:** overdue, due today, due this week, urgent, open to-dos, ideas, notes, and finished in the last 7 days.
- **To-dos:** every open to-do and reminder, most urgent first, with priority, due date and your own words.
- **Done:** finished and dropped items, with when.
- **Notes & ideas:** everything else you saved.

It checks every 5 minutes and writes only when something changed. It's **one-way**: edits you make in the
sheet are overwritten, so mark things done in Telegram. Text is written as plain values, so nothing you typed
can turn into a spreadsheet formula. Your items will also be stored in Google once you turn this on.

**Setup (about 10 minutes, once):**

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project (any name).
2. **APIs & Services → Library** → search **Google Sheets API** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account**. Name it `pluto`, then **Done**. No roles are needed.
4. Open the new service account → **Keys → Add key → Create new key → JSON**. A `.json` file downloads. Keep it **outside** the Pluto folder; it's a password:
   ```bash
   mkdir -p ~/.config/pluto && mv ~/Downloads/*.json ~/.config/pluto/service-account.json && chmod 600 ~/.config/pluto/service-account.json
   ```
   (If `~/Downloads` has other `.json` files, move just the new one by name.)
5. Create an empty Google Sheet ([sheets.new](https://sheets.new)) → **Share** → paste the service account's email (it ends in `.iam.gserviceaccount.com` and is shown on its page) → **Editor** → untick "Notify" → **Share**.
6. In `.env`:
   ```
   GOOGLE_SHEETS_ID=https://docs.google.com/spreadsheets/d/...your sheet's URL...
   GOOGLE_SERVICE_ACCOUNT_FILE=~/.config/pluto/service-account.json
   ```
7. `python bot.py --check` should show `Google Sheet: ✓ …`. Restart the bot and send `/sheet now`.

If `/sheet` says permission was denied, the sheet isn't shared with the service account's email yet (step 5).
A work Google account may block creating keys; a personal Gmail account works.

## How reminders and nudges work

- Checked every minute while the bot is running.
- **A reminder you set always arrives at the time you asked for**, even at night, and doesn't count against any limit.
- If it's still open afterwards, Pluto nudges again every 6 hours (up to 5 times). With the digest off, ideas you haven't touched get a nudge after 3 days, then every 3 days (up to 3 times).
- Those follow-up nudges wait until morning (no follow-ups 22:00–08:00, `QUIET_START/END` in `core.py`) and are capped at 5 per 24 hours.
- Done / Snooze / Drop buttons on every nudge.
- Your computer must be awake with `python bot.py` running for reminders to arrive. To get them when your Mac is asleep, deploy it (see below).

## Rules built in

See `CLAUDE.md`. In short: your original message is stored untouched; if labelling fails it is still
saved as a plain note; recall answers only from saved items; only `ALLOWED_USER_ID` can use the bot;
keys live only in `.env`.

## Deploy (later)

`Procfile` has `worker: python bot.py` for Railway. Add the `.env` values as Railway variables,
and mount a volume for the database (set `PLUTO_DB` to a path on the volume, e.g. `/data/pluto.db`).
The bot must be running to nudge.

## Known limits

- **Search:** each message sends the AI your open items (the 150 most urgent), the 20 most recently finished, and the last 30 chat messages. On top of that, it sends up to 10 older items and 10 older messages that contain the same words, found with SQLite's built-in full-text search (FTS5). So "what did we discuss about Goa?" reaches back to your very first message, while each message stays small. Search matches **words, not meaning**: "beach holiday" won't find "Goa trip", and Hindi in Devanagari won't match the same word typed in Latin letters. If your Python's SQLite lacks FTS5, Pluto falls back to a slower plain-text search.
- **Rolling summary:** once 20 or more messages have scrolled out of the last 30, Pluto asks the AI (in the background, checked every minute) to fold them into a short summary of older chats: plans, people, decisions, preferences, at most 15 lines. Every reply gets that summary as background. `/summary` shows it, `/forget` clears it, and `/delete` throws it away so it's rebuilt without the deleted words. If the AI fails, the old summary stays and it tries again 10 minutes later. Your saved items and messages are never changed by this.
- Voice notes: Groq `whisper-large-v3` → Gemini → Sarvam `saarika:v2.5` (`STT_PROVIDERS` changes the order).
- AI providers are tried in order Gemini → Groq → Z AI → NVIDIA Nemotron (`PLUTO_PROVIDERS` changes it, and `/model` in chat picks which one goes first; the rest stay as backup). Default models: `gemini-2.5-flash`, `llama-3.3-70b-versatile` (Groq), `glm-4.6` (Z AI), `nvidia/nemotron-3.5-super-vl-120b-a12b` (NVIDIA, with thinking off for quick replies; `NVIDIA_THINKING=1` turns it on); change `PLUTO_MODEL`, `GROQ_MODEL`, `ZAI_MODEL`, `NVIDIA_MODEL` if a provider retires one. Fallback models may phrase replies differently, but the code-side checks (what gets saved, never citing unknown items) apply to all of them.
