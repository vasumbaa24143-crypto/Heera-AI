# What Pluto should borrow from QwenPaw and nanobot

Study date: 1 Nov 2026. Sources read (shallow clones, outside this repo, not committed):

| Project | Commit | Licence |
|---|---|---|
| [agentscope-ai/QwenPaw](https://github.com/agentscope-ai/QwenPaw) | `c26d31c` (30 Oct 2026) | Apache 2.0 |
| [HKUDS/nanobot](https://github.com/HKUDS/nanobot) | `8b37976` (31 Oct 2026) | MIT |

**Licence approach.** Nothing in this plan copies code. We re-implement ideas in Pluto's own
style (small SQLite functions in `db.py`, a flow in `core.py`, adapters in `bot.py`). Short prompt
ideas below are paraphrased, not pasted. If code is ever copied, keep the original notice next to
it: the MIT copyright line ("Copyright (c) 2025-present Xubin Ren and the nanobot contributors")
for nanobot, or the Apache 2.0 licence plus a "modified from QwenPaw" note for QwenPaw.

Both projects are large general agent platforms (nanobot ~357k lines incl. UI, QwenPaw ~3,000
Python files). Pluto is a one-person memory assistant. The goal is to take their best *ideas* for
memory and proactive messages, not their machinery.

---

## 1. QwenPaw: three-layer memory

QwenPaw names three memory systems (`website/public/docs/context.en.md`, "The Three Memory
Systems"): **working**, **episodic** (full history) and **semantic** (the ReMe Markdown knowledge
base). The first two come from the "scroll" context manager; the third comes from ReMe.

### 1a. Working memory: the live prompt plus a summary

- `src/qwenpaw/agents/context/scroll/manager.py`: `ScrollContextManager`. Every new turn is
  written to SQLite **before** anything is dropped ("durable first"). When the live prompt goes
  over a trigger ratio (80% by default), finished middle turns are *evicted* from the prompt. The
  active turn and a recent tail are protected.
- `scroll/continuation_summary.py`: evicted turns are replaced by a short Markdown summary with
  fixed sections: *Active Task, Current State, Constraints, Decisions, Open Work*. It is prefixed by
  `SUMMARY_PREFIX` ("…background for task continuity. It is not a user message…"). The model is
  asked for plain Markdown, never JSON; the parse happens locally and **fails closed**. A bad
  update keeps the previous summary. `build_update_prompt()` updates the summary incrementally.
  `redact_secrets()` strips tokens before they are stored.
- `scroll/eviction_index.py`: each summary line carries a `seq` range, so the agent can call
  `recall_history(op="expand", lo, hi)` (`scroll/recall_tool.py`) to read the exact original rows.

### 1b. Full history: every turn, word for word, searchable

- `scroll/history.py`: table `conversation_history(seq, session_id, agent_id, kind, role, content,
  …, created_at)` in `history.db`. `_init_fts()` adds an **external-content FTS5** index:
  `CREATE VIRTUAL TABLE conversation_history_fts USING fts5(content,
  content='conversation_history', content_rowid='seq', tokenize='porter unicode61')`. The index
  doesn't duplicate the text. If SQLite has no FTS5, it logs once and search falls back to `LIKE`.
- `scroll/memoryspace.py`: `MemorySpace.search(query, k=10, created_from=…, created_to=…)`. It
  runs under a time/row budget, using `set_progress_handler` to stop slow queries.
  `fts_match_query()` turns plain text into a safe FTS5 query by quoting each word. That stops
  "C++" or "foo-bar" from raising syntax errors, and keeps implicit AND. CJK queries and
  punctuation-only queries use `LIKE` instead.

### 1c. Semantic memory: the ReMe Markdown knowledge base

- Files, not a hidden DB (`website/public/docs/memory.en.md`): `MEMORY.md` is small core memory
  that only the user and the main agent edit; automation never overwrites it. `memory/YYYY-MM-DD/{name}.md` holds one note per
  session, with YAML frontmatter `name`/`description`/`session_id`/`source_conversation`.
  `digest/{personal,procedure,wiki}/` holds consolidated knowledge with `[[wikilinks]]`.
- **Storing (Auto-Memory):** `src/qwenpaw/agents/middlewares.py`, `MemoryMiddleware`, counts
  user turns and calls `_flush_auto_memory()` every `auto_memory_interval` turns (default **5**,
  `ReMeLightMemoryManager.get_auto_memory_interval()` in
  `agents/memory/reme_light_memory_manager.py`). It extracts only durable things: preferences,
  decisions, progress, blockers. A run that finds nothing writes nothing. Previously recalled
  memory is stripped first, so it isn't re-saved as if the user had said it again.
- **Summarising (Auto-Dream):** a scheduled job reads recently changed daily notes, classifies
  each memory as `personal`, `procedure` or `wiki`, and picks exactly one action per memory:
  `CREATE`, `CORROBORATE`, `REFINE` or `CORRECT`. It links sources back to the daily notes.
- **Retrieving for a question:** `ReMeLightMemoryManager.memory_search()`. It ranks with BM25 and
  optionally embeddings, fuses the two with RRF, and attaches the hit's in/out links. It returns
  **excerpts with path and line numbers**, and the agent opens the full file only if the excerpt
  isn't enough ("progressive" retrieval).

### Mapping onto Pluto

| QwenPaw | Pluto today | What to do |
|---|---|---|
| Working memory: tail plus summary | `core.chat` sends the last `HISTORY_TURNS = 100` messages (`db.recent_messages`) | Send the last ~20 messages verbatim plus a **rolling summary** stored in a new `summaries` table (`db.py`). `llm.py` gets a `summarise()` prompt that returns plain text, parsed and checked in code and failing closed. |
| Full history in SQLite with FTS5 | `messages` table, no search; `db.context_items` sends up to 300 open + 50 closed items | Add FTS5 indexes over `messages.text` and `items.title/original_text` (`db.py`). Add `db.search(uid, query, k)` with a `fts_match_query`-style sanitiser and a `LIKE` fallback. |
| Excerpts with an address, expanded on demand | Item ids `#12` are already the "address" | Search hits go into the prompt as `#id` items or `[msg 2026-10-03]` lines, so the existing "never cite an unknown id" guard in `core.chat` keeps working. |
| Auto-Memory every N turns, nothing saved if nothing durable | Facts are saved per message as `note` items | Keep per-message notes (they're the user's own words). Add a nightly consolidation of *derived* facts (see nanobot Dream below); it's simpler than per-5-turns. |
| `MEMORY.md` never overwritten by automation | Rule 1: `original_text`/`messages.text` are never rewritten | Same principle. Summaries and facts are separate derived rows that cite their sources. |

`bot.py` needs no change for search. It only gains a scheduled job for the consolidation (section 3).

---

## 2. QwenPaw: Heartbeat

- **Trigger:** `src/qwenpaw/app/crons/manager.py`. When `heartbeat.enabled` is true (it's **off by
  default**), it adds an APScheduler job with id `HEARTBEAT_JOB_ID = "_heartbeat"` and
  `misfire_grace_time=HEARTBEAT_MISFIRE_GRACE_SECONDS` (60 s, so a missed run isn't replayed hours
  later). `_build_heartbeat_trigger(every)` makes a `CronTrigger` for a 5-field cron
  (`"0 9 * * *"`) or an `IntervalTrigger` for `"30m"`/`"1h"`.
  `src/qwenpaw/app/crons/heartbeat.py` parses these with `parse_heartbeat_every()`,
  `parse_heartbeat_cron()` and `is_cron_expression()`.
- **What goes in:** `run_heartbeat_once()` reads `HEARTBEAT.md` from the agent workspace (file
  name from `QWENPAW_HEARTBEAT_FILE`). It sends the whole file as one user message in the session
  `"main"`, with `request_context={"source": "heartbeat"}`. The agent can use its tools and memory.
- **Where it goes:** `target` is `main` (run silently), `last` (stream the reply to the channel
  and session in `state/last_dispatch.json`, i.e. wherever you last chatted) or `inbox`.
- **How it avoids spam:**
  1. disabled until switched on;
  2. `_in_active_hours()` skips runs outside a daily window like 08:00–22:00, in the user's timezone;
  3. it skips when `HEARTBEAT.md` is missing or empty;
  4. it caps each run with `timeoutSeconds` (default 300);
  5. the 60 s misfire grace stops a backlog of runs after downtime.

  It does **not** check whether the reply is worth sending. With `target=last`, whatever the
  agent says is delivered. nanobot's heartbeat adds that check (section 4).

### Mapping onto Pluto

| QwenPaw | Pluto |
|---|---|
| APScheduler `CronTrigger` at a fixed time | `bot.py` already runs APScheduler through `python-telegram-bot[job-queue]`: `app.job_queue.run_daily(digest_job, time=…)`. No new dependency. |
| `_in_active_hours()` | `core.in_quiet_hours()` already does this (22:00–08:00). |
| Skip when there's nothing to ask | The digest is built **from data** in `db.py`. If the query returns nothing, send nothing. That's stronger than QwenPaw's "empty file" check. |
| Free-form `HEARTBEAT.md` prompt to an LLM | Skip for Phase 1. A fixed template is cheaper and can't invent anything (rule 2). |
| Misfire grace | Send at most one digest per local day, and only if the bot is up before a cutoff (see section 5). |

---

## 3. nanobot: "Dream" long-term memory

nanobot has two stages (`docs/memory.md`):

**Stage 1, Consolidator** (`nanobot/agent/memory.py`, `class Consolidator`):
`summarize_transcript()` and `compact_idle_session()` turn old conversation into a compact
checkpoint, appended to `memory/history.jsonl`, one JSON line per entry
(`MemoryStore.append_history()`). It runs when:
- the session would exceed the model's input budget (`estimate_session_prompt_tokens()`,
  `_input_token_budget()`);
- the chat has been **idle** for `session_ttl_minutes`, default **15** (`nanobot/config/schema.py`,
  alias `idleCompactAfterMinutes`, checked every `idle_compact_check_interval_seconds = 60`);
- the user sends `/compact`.

Its prompt is `nanobot/templates/agent/consolidator_archive.md`. In short:
- keep a working-state handoff (objective, status, blockers, next action);
- keep any other fact only if it passes **SNIP**: Signal (saves the user repeating it), Novel,
  Important (losing it causes rework), Persistent (useful for 2+ weeks);
- tag each fact `[permanent]`, `[durable]`, `[ephemeral]` or `[correction]`;
- output one `- [mark] fact` per line, or `(nothing)`.

**Stage 2, Dream** (`MemoryStore.build_dream_prompt(max_entries=20)`):
- **When:** a scheduled job named `"dream"`. `DreamConfig` in `nanobot/config/schema.py` has
  `enabled=True` and `interval_h=2`, built as an `every` schedule by `build_schedule()`. There's a
  legacy `cron` override, plus a manual `/dream` command (`nanobot/command/builtin.py`).
- **Skips if nothing is new:** `build_dream_prompt()` reads only history entries after
  `.dream_cursor` (`get_last_dream_cursor()`) and returns `None` when there are none. The runner
  logs "Dream: nothing to process" and exits.
- **Runs:** `nanobot/cli/gateway_runtime.py`, `on_cron_job()`, the `job.name == "dream"` branch.
  It runs `agent.process_direct()` in an **ephemeral** session with a restricted tool set
  (`MemoryStore.build_dream_tools()`: file read/edit on the memory files). The cursor only
  advances if the run completed (`MemoryStore.dream_run_completed()`). Changes are committed to a
  small git store (`_commit_dream_changes`), then `compact_history()` prunes processed entries.
- **Prompt** (`nanobot/templates/agent/dream.md`), paraphrased:
  - route each fact to one canonical file: `SOUL.md` (bot behaviour), `USER.md` (the person's
    traits and preferences), `memory/MEMORY.md` (goals and decisions), and `skills/` for workflows
    seen at least twice;
  - write **atomic facts** ("has a cat named Luna"), not descriptions ("discussed pets");
  - apply the tags: `[correction]` replaces the old fact in place, `[permanent]` is kept
    regardless of age, `[ephemeral]` is dropped after it stops being useful (about 30 days);
  - remove superseded facts and filler;
  - **"when the retained memory is already current, leave it unchanged"**.

### Mapping onto Pluto

| nanobot | Pluto |
|---|---|
| `history.jsonl` checkpoints | `summaries(id, user_id, from_msg_id, to_msg_id, text, created_at)` in `db.py`. Each summary covers a message-id range, like QwenPaw's `seq` spans. |
| Consolidator prompt (SNIP + tags + `(nothing)`) | `llm.summarise(messages, previous_summary)` returns `- [mark] fact` lines. `core` parses them, ignores bad lines, and keeps the old summary on failure (rule 5 spirit). |
| `USER.md`/`MEMORY.md` edited by Dream | `facts(id, user_id, text, mark, source, created_at, superseded_by)` in SQLite. Dream-style runs add rows or set `superseded_by`; they **never** edit `items.original_text` or `messages.text`. |
| `.dream_cursor`, skip when nothing new | A `settings` row `dream_cursor` (the `settings` table already exists for `/model`). Skip the LLM call when no new summaries exist. |
| Scheduled every 2 h | `bot.py`: `job_queue.run_daily(dream_job, time=03:30 local)`. Once a night is enough for one person. |
| Ephemeral session, restricted tools | Pluto has no tools yet. `llm.consolidate()` returns JSON actions (`add`, `supersede`) that `core` validates against existing fact ids, the same way `core.chat` validates `done` ids today. |

---

## 4. nanobot: how cron and scheduled tasks reach Telegram

1. **Creating a job:** the model calls the `cron` tool (`nanobot/agent/tools/cron.py`, `CronTool`)
   with `action="add"` and exactly one schedule: `at` (an ISO time), `every_seconds`, or
   `cron_expr` + `tz`. The job is bound to the current chat (session key, channel, chat_id) via
   `_request_route()`.
2. **Waking up:** `nanobot/cron/service.py`, `CronService`. `_compute_next_run()` works out the
   next time; `_arm_timer()` sleeps until the earliest `next_run_at_ms` (capped at `max_sleep_ms
   = 300000`, 5 min); `_on_timer()` → `_run_timer()` → `_execute_job()`. Jobs persist to
   `<workspace>/cron/jobs.json`. One-shot `at` jobs can `delete_after_run`. `_resume_next_run()`
   keeps `every` jobs on their rhythm after a restart. Note: for `at` jobs, `_compute_next_run()`
   returns `None` once the time has passed, so **a one-off reminder missed during downtime is
   skipped**. Pluto's poll-every-minute design fires it late instead, which is better for
   reminders. Keep Pluto's approach.
3. **Running it as a chat turn:** `nanobot/cron/bound_runner.py`, `run_bound_cron_job()`. It
   renders `templates/agent/cron_reminder.md` ("The scheduled time has arrived… speak directly to
   the user in their language… don't add 'Done' or 'Reminded'"), then calls
   `agent.submit_cron_turn(InboundMessage(channel, chat_id, content=prompt,
   session_key_override=…))`. It records queued/ok/error run records.
4. **Delivering:** the agent's reply becomes an `OutboundMessage`. `nanobot/bus/queue.py`
   (`consume_outbound()`) passes it to `nanobot/channels/manager.py`
   (`_dispatch_outbound_loop()`), which calls `nanobot/channels/telegram/runtime.py`,
   `TelegramChannel.send()`. That splits long text into chunks that fit Telegram's 4096-character
   limit and sends it with `bot.send_message`.
5. **Heartbeat variant:** `gateway_runtime.on_cron_job()`, `job.name == "heartbeat"`. It runs
   only if `HEARTBEAT.md` has an "Active tasks" section with content
   (`_heartbeat_has_active_tasks()`). After the run, a second small LLM call
   (`nanobot/utils/evaluator.py`, `evaluate_response(..., default_notify=False)`, prompt
   `templates/agent/evaluator.md`) decides whether to notify. It suppresses "routine status check
   with nothing new" and **stays silent if the evaluator fails** ("fail closed").

### Mapping onto Pluto

| nanobot | Pluto today | What to do |
|---|---|---|
| `CronService` timer to the next due job | `bot.nudge_job` every 60 s → `core.due_nudges()` → `db.pick_nudges()` | Keep it. Polling one small SQLite table each minute is simple and survives restarts without a jobs file. |
| `at` / `every` / `cron_expr` schedules | Only one-off `items.due_at` | Later: recurring reminders with a `repeat` column on `items` (`daily`, `weekly:MON`, `monthly:15`). When a recurring item is marked done, `core` creates the next occurrence. No cron parser needed. |
| `cron_reminder.md`: the reply comes from an LLM turn | Fixed text `⏰ Reminder: {title}` | Keep fixed text: it's instant, free, and can't invent anything. |
| Outbound bus → channel manager → `TelegramChannel.send` | `bot.nudge_job` → `ctx.bot.send_message` | Pluto's `core.Reply` + `bot.send`/`markup` already play the role of `OutboundMessage` + `TelegramChannel`. No bus needed for one channel. |
| Evaluator gate, fail closed | — | For the digest, use a **deterministic** gate ("nothing due, nothing overdue, no idea → don't send"). An LLM gate is only needed if the digest is ever written by an LLM. |

---

## 5. Design: daily digest

> **Status: built** (`core.due_digests`, `core.build_digest`, `core.cmd_digest`, `db.digest_rows`,
> `bot.digest_job`). One change from this design: with the digest on, stale ideas no longer get
> separate nudges, because the digest already brings up one old idea a day.

**Goal:** one morning message with what matters today, and silence when there's nothing to say.

### What's in it

```
☀️ Good morning! Here's today (Tue 3 Nov):

⚠️ Overdue (2)
• #14 🔴 Renew passport (was due Sun 1 Nov)
• #9 Send deck to Rahul (was due Mon 2 Nov, 18:00)

📅 Today (1)
• #21 Call bank — 15:00

💡 From a while back
• #4 YouTube tech news segment (you told me 12 days ago). Still interested?
```

- **Overdue:** open items with `due_at` before now, oldest due first. High priority first within
  the same day. Show at most 5, then "…and N more, see /list".
- **Today:** open items due between now and local midnight, by time. Max 5.
- **One old idea:** one open `idea` that is ≥ 7 days old and hasn't been in a digest or nudge in
  the last 7 days. Rotate by `last_nudged_at`, oldest first. Its message gets `[Keep] [Drop]`
  buttons (existing `snz:`/`arc:` callbacks).
- Titles are shown exactly as stored. No LLM call, so nothing can be invented and it costs nothing.

### When and how often

- Default **08:30 local** (`PLUTO_TZ`). `/digest` shows the setting; `/digest off`, `/digest on`
  and `/digest 07:45` change it. It's stored in the existing `settings` table
  (`digest_time`, `digest_enabled`, `digest_last_date`).
- `bot.py`: `app.job_queue.run_daily(digest_job, time=datetime.time(8, 30, tzinfo=core.TZ))`.
  Telegram's job queue is APScheduler, already installed. For a changed time, use a minute job
  that checks whether the local time has passed `digest_time` and `digest_last_date != today`.
  This also catches up after the bot was down.
- **Catch-up rule** (like QwenPaw's misfire grace): if the bot starts after 08:30 but before
  **12:00**, send today's digest once; after noon, skip it, since a stale morning digest is noise.

### Skip rules (anti-spam)

1. Nothing overdue, nothing today, no eligible idea → **send nothing**, but still set
   `digest_last_date = today` so we don't keep re-checking.
2. Never more than one digest per local day (`digest_last_date`).
3. Off until the user has saved at least one item. New users don't get empty "good morning"s.
4. Items in the digest count as nudged (`db.mark_nudged`), so the 6-hourly repeat nudge for an
   overdue item doesn't fire again an hour later. A to-do due **later today** still gets its own
   on-time reminder; the digest doesn't replace it.
5. The digest itself doesn't count against the daily nudge cap. It's one message the user opted
   into.

### Code shape

| File | Change |
|---|---|
| `db.py` | `digest_rows(uid, now) -> (overdue, today, idea)`, three small SELECTs reusing `status='open'`, `due_at` and `last_nudged_at`. |
| `core.py` | `build_digest(uid, now) -> Reply | None` (None means skip), and `cmd_digest(uid, arg)`. |
| `bot.py` | `digest_job()`: for the allowed user, `core.build_digest`, send it, then `db.set_setting(..., "digest_last_date", ...)`. Register `/digest`. |
| `simulate.py` | `/digest` to preview today's digest at the simulated time. |
| `llm.py` | No change. |
| Tests | Skip when empty; one per day; catch-up before 12:00 and none after; idea rotation; caps of 5 + "N more"; overdue items marked as nudged; other users' items never included. |

---

## 6. Design: memory upgrade (SQLite only)

**When it's needed:** each message currently sends up to 300 open + 50 closed items and the last
100 messages. That's fine for months of use. It becomes slow and costly when there are more than
~300 items, or when "what did we discuss about X" refers to chats older than the last 100 messages.

**Constraint:** SQLite only, no embeddings, no vector DB, no new packages. FTS5 is built into the
`sqlite3` module that ships with Python: it works here on SQLite 3.53, with `bm25()` and
`snippet()`. Python.org and Homebrew builds on macOS include FTS5 too.

### Three layers, Pluto-sized

| Layer | Stored in | Sent with each message |
|---|---|---|
| **Working** | `messages` (exists) + new `summaries` | Last **20** messages verbatim, plus the newest rolling summary (≤ 1,500 chars) |
| **Searchable history** | `messages_fts`, `items_fts` (new FTS5 indexes) | Top **10** search hits for the current message (items as `#id`, messages as dated quotes) |
| **Profile facts** | new `facts` table | All current facts (cap 60), e.g. "Rahul is a colleague", "prefers Hinglish replies" |

Also always sent, as today: **all open items** (capped at 150, sorted by urgency) with their
time buckets, because agenda questions ("what's on my plate") must see everything open.

### Tables (in `db.init()`, created idempotently)

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, original_text, content='items', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, content='messages', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');
-- plus AFTER INSERT / AFTER DELETE triggers on items and messages to keep the indexes in step,
-- and a one-time INSERT INTO items_fts(items_fts) VALUES('rebuild') for existing rows.

CREATE TABLE IF NOT EXISTS summaries(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
  from_msg_id INTEGER NOT NULL, to_msg_id INTEGER NOT NULL,
  text TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
  text TEXT NOT NULL, mark TEXT NOT NULL,          -- permanent | durable | ephemeral
  source TEXT NOT NULL,                            -- e.g. "summary:12" or "item:40"
  created_at TEXT NOT NULL, superseded_by INTEGER); -- never deleted by automation
```

- **No Porter stemming:** unlike QwenPaw's `porter unicode61`, Porter is English-only and would
  mangle Hinglish words; `unicode61` alone is safer.
- External-content tables (QwenPaw's approach) index the text without storing it twice. `items`
  and `messages` stay the source of truth (rule 1).
- If `CREATE VIRTUAL TABLE … fts5` raises "no such module", set `db.HAS_FTS = False` and
  `db.search()` uses `LIKE '%word%'` on each keyword instead (QwenPaw's fallback).

### Search (`db.search(uid, query, k=10)`)

1. Turn the user's message into a safe FTS query, like QwenPaw's `fts_match_query()`: take word
   tokens, drop English and Hinglish stop-words ("kya", "tha", "the", "about"), quote each token,
   and join with `OR`. Ranking by `bm25()` puts documents matching more words first.
2. Query `items_fts` and `messages_fts` for this `user_id` only, ordered by `bm25()`, limited to `k`.
3. Return items as their normal views (so `#id` citations and the existing unknown-id guard keep
   working). Return messages as `[Sun 2026-10-04] You: "…snippet…"`.

`core.chat` then calls `llm.respond(message, history, items, now, hits=…, summary=…, facts=…)`.
`llm.format_context()` gains three short sections. The prompt rule stays: answer only from these.

### Rolling summary (nanobot's Consolidator, simplified)

- **When:** more than 40 messages exist after the last summary's `to_msg_id`, or the chat has been
  idle 30 min with at least 10 unsummarised messages. That's checked by the existing 60 s job in
  `bot.py`, so no new scheduler is needed.
- **How:** `llm.summarise(previous_summary, new_messages)` uses a short prompt based on the SNIP
  idea: keep open threads, decisions, people and preferences; drop small talk; write plain lines;
  return `(nothing)` if nothing is worth keeping. `core` stores it with the message-id range it
  covers. If the call fails or returns junk, keep the previous summary and try later (fail closed).

### Profile facts (nanobot's Dream, simplified)

- **When:** nightly at 03:30 local (`job_queue.run_daily`), only if new summaries exist since
  `settings.dream_cursor`. Otherwise no LLM call.
- **How:** `llm.consolidate(current_facts, new_summaries)` returns JSON
  `{"add": [{"text","mark"}], "supersede": [{"id", "text"}]}`. `core` checks that every `id`
  belongs to the user and applies the changes. Old facts get `superseded_by`; they are never
  deleted by automation.
- Facts are **derived**. They cite `source`, and a reply that relies on one can be traced back.

### Privacy and the existing commands

- `/delete <id>` also removes the item from `items_fts` (trigger), and removes facts whose only
  source is that item.
- `/forget` also clears `summaries` and `messages_fts` rows, and the facts sourced from summaries.
- `/export` adds `summaries` and `facts`.

### Cost check

The prompt goes from ≤ 350 items + 100 messages to ≤ 150 open items + 10 hits + 20 messages + 1
summary + 60 facts. That's a fixed upper bound however long Pluto is used.

---

## 7. What to skip in Phase 1 (too big for one person)

| Skip | Why |
|---|---|
| Embeddings, vector search, RRF fusion, rerankers (QwenPaw `reme_embedding.py`, `reme_reranker.py`) | Needs a model/API, an index to rebuild, and tuning. FTS5 + summaries + facts cover one person's notes. |
| Markdown knowledge base, `[[wikilinks]]` graph, per-day note files, git-versioned memory (ReMe, nanobot `GitStore`) | Great for research workspaces. Pluto's SQLite rows + `/export` are enough, and simpler to keep private. |
| Free-form `HEARTBEAT.md` LLM check-ins and QwenPaw's "proactive" goal guessing (`agents/memory/proactive/`) | Unpredictable and costly. The deterministic digest gives the useful part. |
| An LLM "should we notify" evaluator | Only needed for LLM-written proactive messages, which we're not doing. |
| Full agent loop with tools, sub-agents, skills, MCP, shell/file tools, sandboxing | Large surface and security work. Revisit after the digest and memory upgrade if the one-JSON-per-message design really limits us. |
| Message bus + multi-channel manager, web UI, console, TUI | One channel (Telegram) plus `simulate.py` doesn't need a bus. `core.py` is already transport-agnostic. |
| Cron expressions typed by the user | Most people say "every Monday" or "daily". A small `repeat` column covers that without a parser. |
| QwenPaw's "Auto Resource" (papers, news feeds) | Out of scope for a personal memory assistant. |
| nanobot's LLM-written reminder text (`cron_reminder.md`) | A fixed "⏰ Reminder: …" is instant, free, and can't invent anything. |

## 8. Suggested order

1. **Daily digest** (section 5). Smallest change, no LLM, uses the existing job queue, and fixes
   "Pluto doesn't reach out".
2. **FTS5 search** over items and messages, replacing "send the last 300/100".
3. **Rolling summaries**, then **nightly facts**.
4. **Recurring reminders** (`repeat` column), the most-requested scheduling gap, still with no parser.
