"""Pluto - Telegram bot. Capture -> label -> remember -> recall -> nudge.

This file only translates between Telegram and core.py, which holds the flow.
"""
import asyncio
import io
import logging
import os

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
NUDGE_EVERY_SECONDS = 1800


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
    except Exception:
        log.exception("transcription failed")
        await update.message.reply_text(
            "I could not understand that voice note. Please type it or try again."
        )
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


def check_config() -> None:
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "ALLOWED_USER_ID") if not os.getenv(k)]
    if not llm.providers():
        missing.append("an AI key (GEMINI_API_KEY, GROQ_API_KEY or ZAI_API_KEY)")
    if missing:
        raise SystemExit(
            f"Missing in .env: {', '.join(missing)}. Copy .env.example to .env and fill it in."
        )
    if not ALLOWED.isdigit():
        raise SystemExit("ALLOWED_USER_ID must be your numeric Telegram id (ask @userinfobot).")
    if len(llm.providers()) == 1:
        log.warning("Only one AI provider is set; add another key so Pluto has a fallback.")
    if not os.getenv("SARVAM_API_KEY"):
        log.warning("SARVAM_API_KEY is not set: voice notes will not work, text will.")


def build_app() -> Application:
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("snooze", cmd_snooze))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(nudge_job, interval=NUDGE_EVERY_SECONDS, first=60)
    return app


def main() -> None:
    check_config()
    db.init()
    app = build_app()
    log.info("Pluto is running (AI: %s)", llm.describe())
    app.run_polling()


if __name__ == "__main__":
    main()
