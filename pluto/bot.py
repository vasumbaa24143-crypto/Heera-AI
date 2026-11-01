"""Pluto - Telegram bot. Capture -> label -> remember -> recall -> nudge."""
import asyncio
import io
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db  # noqa: E402
import llm  # noqa: E402
import stt  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("pluto")

TZ = ZoneInfo(os.getenv("PLUTO_TZ", "Asia/Kolkata"))
ALLOWED = os.getenv("ALLOWED_USER_ID")  # only this Telegram user may use the bot
QUIET_START, QUIET_END = 22, 8  # no nudges between 22:00 and 08:00 local

KIND_ICON = {"idea": "💡", "promise": "🤝", "task": "✅", "note": "📝"}


def now_local() -> datetime:
    return datetime.now(TZ)


def allowed(update: Update) -> bool:
    u = update.effective_user
    return bool(u) and (not ALLOWED or str(u.id) == ALLOWED)


def fmt_due(due_utc: str | None) -> str:
    if not due_utc:
        return ""
    return db.from_str(due_utc).astimezone(TZ).strftime("%a %d %b, %H:%M")


def item_line(r) -> str:
    due = f" — due {fmt_due(r['due_at'])}" if r["due_at"] else ""
    return f"{KIND_ICON.get(r['kind'], '•')} #{r['id']} {r['title']}{due}"


def buttons(item_id: int, with_archive=False) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton("✅ Done", callback_data=f"done:{item_id}"),
        InlineKeyboardButton("😴 +1 day", callback_data=f"snz:{item_id}"),
    ]
    if with_archive:
        row.append(InlineKeyboardButton("🗄 Drop", callback_data=f"arc:{item_id}"))
    return InlineKeyboardMarkup([row])


# ---------- core flow ----------

async def process(update: Update, text: str) -> None:
    uid = update.effective_user.id
    msg = update.message
    try:
        result = await asyncio.to_thread(llm.extract, text, now_local())
    except Exception:
        # Never lose a thought: if labelling fails, save it raw as a note.
        log.exception("extract failed; saving raw note")
        item_id = db.add_item(uid, "note", text[:80], text)
        await msg.reply_text(f"Saved as a plain note (#{item_id}). I could not label it just now.")
        return

    if result["is_question"]:
        pool = db.recall_pool(uid)
        try:
            ans = await asyncio.to_thread(llm.answer, text, pool, now_local())
        except Exception:
            log.exception("answer failed")
            ans = "I could not search just now. Your items are safe — try /list."
        await msg.reply_text(ans)
        return

    items = result["items"] or [{"kind": "note", "title": text[:80], "due_local": None}]
    for it in items:
        due_utc = None
        if it.get("due_local"):
            try:
                dt = datetime.fromisoformat(it["due_local"]).replace(tzinfo=TZ)
                due_utc = db.to_str(dt)
            except ValueError:
                due_utc = None
        kind = it.get("kind") if it.get("kind") in KIND_ICON else "note"
        title = (it.get("title") or text[:80])[:120]
        item_id = db.add_item(uid, kind, title, text, due_utc)
        row = db.get_item(uid, item_id)
        await msg.reply_text(
            f"Saved. {item_line(row)}",
            reply_markup=buttons(item_id) if kind in ("task", "promise") or due_utc else None,
        )


# ---------- handlers ----------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text(
        "Pluto here. Just tell me anything — type it or send a voice note.\n\n"
        "I will remember it, and nudge you if it needs follow-up.\n"
        "Ask me later: “what was that YouTube idea?”\n\n"
        "/list  open items\n/done <id>  mark done\n/snooze <id>  +1 day\n"
        "/delete <id>  remove forever\n/export  download everything"
    )


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await process(update, update.message.text)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    v = update.message.voice or update.message.audio
    try:
        f = await v.get_file()
        buf = io.BytesIO()
        await f.download_to_memory(buf)
        text = await stt.transcribe(buf.getvalue())
    except Exception:
        log.exception("transcription failed")
        await update.message.reply_text("I could not understand that voice note. Please type it or try again.")
        return
    if not text:
        await update.message.reply_text("I heard silence. Try again?")
        return
    await update.message.reply_text(f"🎙 Heard: {text}")
    await process(update, text)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    rows = db.list_open(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Nothing open. Tell me something to remember.")
        return
    await update.message.reply_text("\n".join(item_line(r) for r in rows))


def _arg_id(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        return int(ctx.args[0].lstrip("#"))
    except (IndexError, ValueError):
        return None


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    i = _arg_id(ctx)
    ok = i is not None and db.set_status(update.effective_user.id, i, "done")
    await update.message.reply_text(f"Done #{i} ✅" if ok else "Usage: /done <id>")


async def cmd_snooze(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    i = _arg_id(ctx)
    ok = i is not None and db.snooze(update.effective_user.id, i, 24)
    await update.message.reply_text(f"Snoozed #{i} for a day 😴" if ok else "Usage: /snooze <id>")


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    i = _arg_id(ctx)
    ok = i is not None and db.set_status(update.effective_user.id, i, "deleted")
    await update.message.reply_text(f"Deleted #{i}" if ok else "Usage: /delete <id>")


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    data = json.dumps(db.export_all(update.effective_user.id), ensure_ascii=False, indent=2)
    await update.message.reply_document(io.BytesIO(data.encode()), filename="pluto-export.json")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not allowed(update):
        return
    action, _, raw = q.data.partition(":")
    uid, item_id = update.effective_user.id, int(raw)
    if action == "done":
        ok, text = db.set_status(uid, item_id, "done"), f"Done #{item_id} ✅"
    elif action == "arc":
        ok, text = db.set_status(uid, item_id, "archived"), f"Dropped #{item_id} 🗄"
    elif action == "snz":
        ok, text = db.snooze(uid, item_id, 24), f"Snoozed #{item_id} for a day 😴"
    else:
        return
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text(text if ok else "That item is no longer there.")


# ---------- nudges ----------

async def nudge_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    hour = now_local().hour
    if hour >= QUIET_START or hour < QUIET_END:
        return
    for r, reason in db.pick_nudges():
        if reason == "due":
            text = f"⏰ Due: {r['title']} (#{r['id']})"
            kb = buttons(r["id"])
        else:
            days = (db.now_utc() - db.from_str(r["created_at"])).days
            text = f"💡 You told me this {days} days ago: “{r['title']}” (#{r['id']}). Still want it?"
            kb = buttons(r["id"], with_archive=True)
        try:
            await ctx.bot.send_message(r["user_id"], text, reply_markup=kb)
            db.mark_nudged(r["id"], r["user_id"])
        except Exception:
            log.exception("nudge failed for item %s", r["id"])


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (get it from @BotFather).")
    db.init()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("snooze", cmd_snooze))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(nudge_job, interval=1800, first=60)
    log.info("Pluto is running")
    app.run_polling()


if __name__ == "__main__":
    main()
