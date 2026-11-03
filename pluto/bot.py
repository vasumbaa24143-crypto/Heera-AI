"""Pluto - Telegram bot. Capture -> label -> remember -> recall -> nudge.

This file only translates between Telegram and core.py, which holds the flow.
"""
import asyncio
import io
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
from telegram.error import BadRequest  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import core  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import stt  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the bot token
log = logging.getLogger("pluto")

ALLOWED = os.getenv("ALLOWED_USER_ID", "").strip()  # only this Telegram user may use the bot
NUDGE_EVERY_SECONDS = 60  # reminders arrive within a minute of their time


def allowed(update: Update) -> bool:
    """Fail closed: with no ALLOWED_USER_ID set, nobody is allowed."""
    u = update.effective_user
    return bool(u) and bool(ALLOWED) and str(u.id) == ALLOWED


def button_rows(buttons) -> list:
    """Reply.buttons is one row [(label, data), ...] or several rows [[...], [...]]."""
    if not buttons:
        return []
    return buttons if isinstance(buttons[0], list) else [buttons]


def markup(reply: core.Reply):
    rows = button_rows(reply.buttons)
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


async def send(message, replies) -> None:
    for r in replies:
        await message.reply_text(r.text, reply_markup=markup(r))


# ---------- handlers ----------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text(core.START_TEXT)


async def handle_text(update: Update, text: str) -> None:
    uid = update.effective_user.id
    replies = await asyncio.to_thread(core.chat, uid, text, llm)
    await send(update.message, replies)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await handle_text(update, update.message.text)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    v = update.message.voice or update.message.audio
    try:
        f = await v.get_file()
        buf = io.BytesIO()
        await f.download_to_memory(buf)
        text = await stt.transcribe(
            buf.getvalue(),
            filename=getattr(v, "file_name", None) or "voice.ogg",
            mime=getattr(v, "mime_type", None) or "audio/ogg",
        )
    except stt.TranscriptionFailed as e:
        log.warning("transcription failed: %s", e)
        if not stt.providers():
            msg = (f"I can't listen to voice notes yet: add one of {stt.KEY_NAMES} to .env "
                   "and restart me. Meanwhile, please type it.")
        else:
            msg = "I couldn't make out that voice note just now. Please try again or type it."
        await update.message.reply_text(msg)
        return
    except Exception:
        log.exception("could not download the voice note")
        await update.message.reply_text("I couldn't download that voice note. Please try again.")
        return
    if not text:
        await update.message.reply_text("I heard silence. Try again?")
        return
    await update.message.reply_text(f"🎙 Heard: {text}")
    await handle_text(update, text)


def _arg(ctx):
    return ctx.args[0] if ctx.args else None


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await send(update.message, [core.cmd_list(update.effective_user.id)])


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await send(update.message, [core.cmd_done(update.effective_user.id, _arg(ctx))])


async def cmd_snooze(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await send(update.message, [core.cmd_snooze(update.effective_user.id, _arg(ctx))])


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await send(update.message, [core.cmd_delete(update.effective_user.id, _arg(ctx))])


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await send(update.message, [core.cmd_forget(update.effective_user.id)])


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await send(update.message, [core.cmd_model(update.effective_user.id, llm, _arg(ctx))])


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await send(update.message, [core.cmd_digest(update.effective_user.id, _arg(ctx))])


async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await send(update.message, [core.cmd_summary(update.effective_user.id)])


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    data = core.export_json(update.effective_user.id)
    await update.message.reply_document(io.BytesIO(data.encode()), filename="pluto-export.json")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()  # always stop the button's loading spinner
    if not allowed(update):
        return
    reply = core.button(update.effective_user.id, q.data, llm=llm)
    if reply is None:
        return
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass  # message too old to edit, or already edited
    await q.message.reply_text(reply.text)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error", exc_info=ctx.error)


# ---------- nudges ----------

async def nudge_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now = db.now_utc()
    for uid, item_id, reply in core.due_nudges(now):
        try:
            await ctx.bot.send_message(uid, reply.text, reply_markup=markup(reply))
        except Exception:
            log.exception("nudge failed for item %s", item_id)
            continue
        db.mark_nudged(item_id, uid, now)


def digest_users() -> list:
    """Who gets a morning digest: the one allowed user."""
    return [int(ALLOWED)] if ALLOWED.isdigit() else []


async def digest_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now = db.now_utc()
    for uid, reply, to_mark in core.due_digests(digest_users(), now):
        if reply is not None:
            try:
                await ctx.bot.send_message(uid, reply.text, reply_markup=markup(reply))
            except Exception:
                log.exception("digest failed for user %s", uid)
                continue  # not marked as sent: tried again next minute, until the grace ends
        core.digest_done(uid, to_mark, now)


async def summary_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Fold chat that has left the recent window into the rolling summary (in a worker
    thread, since it may call the AI). Usually a quick no-op."""
    for uid in digest_users():
        try:
            await asyncio.to_thread(core.update_summary, uid, llm)
        except Exception:
            log.exception("summary job failed for user %s", uid)


def check_config() -> None:
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "ALLOWED_USER_ID") if not os.getenv(k)]
    if not llm.providers():
        missing.append(f"an AI key ({llm.KEY_NAMES})")
    if missing:
        raise SystemExit(
            f"Missing in .env: {', '.join(missing)}. Copy .env.example to .env and fill it in."
        )
    if not ALLOWED.isdigit():
        raise SystemExit("ALLOWED_USER_ID must be your numeric Telegram id (ask @userinfobot).")
    if len(llm.providers()) == 1:
        log.warning("Only one AI provider is set; add another key so Pluto has a fallback.")
    if not stt.providers():
        log.warning("No speech-to-text key (%s): voice notes won't work, text will.", stt.KEY_NAMES)


def build_app() -> Application:
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("snooze", cmd_snooze))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(nudge_job, interval=NUDGE_EVERY_SECONDS, first=5)
    app.job_queue.run_repeating(digest_job, interval=NUDGE_EVERY_SECONDS, first=10)
    app.job_queue.run_repeating(summary_job, interval=NUDGE_EVERY_SECONDS, first=30)
    return app


KEY_REPORT = [
    ("TELEGRAM_BOT_TOKEN", "Telegram bot token", "required"),
    ("ALLOWED_USER_ID", "your Telegram user id", "required"),
    ("GEMINI_API_KEY", "Gemini", "AI"),
    ("GROQ_API_KEY", "Groq", "AI"),
    ("ZAI_API_KEY", "Z AI", "AI"),
    ("NVIDIA_API_KEY", "NVIDIA Nemotron", "AI"),
    ("SARVAM_API_KEY", "Sarvam (voice backup)", "optional"),
]


def key_report() -> str:
    """Which keys are set, without ever showing their values."""
    lines = ["Keys in .env (values are never shown):"]
    for name, what, kind in KEY_REPORT:
        val = os.getenv(name) or (os.getenv("Z_AI_API_KEY") if name == "ZAI_API_KEY" else None)
        mark = "✓ set    " if val else ("✗ MISSING" if kind == "required" else "– not set")
        lines.append(f"  {mark}  {name:<20} {what}" + ("  (required)" if kind == "required" else ""))
    lines.append(f"AI order: {llm.describe()}")
    voice = " → ".join(stt.providers()) or f"✗ none (add {stt.KEY_NAMES})"
    lines.append(f"Voice notes: {voice}")
    if not llm.providers():
        lines.append("✗ Set at least one AI key.")
    return "\n".join(lines)


def main() -> None:
    if "--check" in sys.argv[1:]:
        print(key_report())
        return
    check_config()
    db.init()
    app = build_app()
    log.info("Pluto is running (AI: %s)", llm.describe())
    app.run_polling()


if __name__ == "__main__":
    main()
