"""Chat with Pluto in the terminal: the same flow as the Telegram bot (core.py),
without Telegram.

    python simulate.py              # uses your AI keys from .env (Gemini, Groq, Z AI, NVIDIA)
    python simulate.py --offline    # simple built-in assistant, no network, no keys

It uses its own database (simulate.db) so it never touches the bot's pluto.db.
Type /help inside for commands, including /advance to move the clock forward
and /nudges to see what the bot would nudge you about.
"""
import argparse
import os
import re
import sys
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()

import core  # noqa: E402
import db  # noqa: E402

HELP = """Chat with Pluto like an assistant. Say "remind me ..." or "note: ..." to save
something; ask "what's on my plate today?" or "what have we discussed about X?".

/list              open items
/done <id>         mark done
/snooze <id>       +1 day
/delete <id>       remove forever
/model [name]      choose which AI thinks first (buttons, or e.g. /model groq)
/forget            clear the chat history (items are kept)
/export            print everything as JSON
/tap <data>        press a button, e.g. /tap done:3
/nudges            run the reminder, nudge and morning-digest checks now
/digest [now|on|off|HH:MM]  morning digest settings, or preview it
/advance <n>h|<n>d move the simulated clock forward, e.g. /advance 6h
/time              show the simulated time
/quit              exit"""


class Session:
    def __init__(self, llm, uid=1):
        self.llm, self.uid, self.offset = llm, uid, timedelta(0)

    def now(self):
        return db.now_utc() + self.offset

    def handle(self, line: str) -> list:
        """Return lines of output for one line of input."""
        line = line.strip()
        if not line:
            return []
        if not line.startswith("/"):
            return render(core.chat(self.uid, line, self.llm, self.now()))
        cmd, _, arg = line.partition(" ")
        arg = arg.strip() or None
        now = self.now()
        if cmd in ("/start", "/help"):
            return [HELP]
        if cmd == "/list":
            return render([core.cmd_list(self.uid)])
        if cmd == "/done":
            return render([core.cmd_done(self.uid, arg)])
        if cmd == "/snooze":
            return render([core.cmd_snooze(self.uid, arg, now)])
        if cmd == "/delete":
            return render([core.cmd_delete(self.uid, arg)])
        if cmd == "/model":
            return render([core.cmd_model(self.uid, self.llm, arg)])
        if cmd == "/digest":
            return render([core.cmd_digest(self.uid, arg, now)])
        if cmd == "/forget":
            return render([core.cmd_forget(self.uid)])
        if cmd == "/export":
            return [core.export_json(self.uid)]
        if cmd == "/tap":
            reply = core.button(self.uid, arg, now, llm=self.llm)
            return render([reply]) if reply else ["Unknown button."]
        if cmd == "/nudges":
            out = []
            for uid, reply, to_mark in core.due_digests([self.uid], now):
                if reply is not None:
                    out += render([reply])
                core.digest_done(uid, to_mark, now)
            for uid, item_id, reply in core.due_nudges(now):
                db.mark_nudged(item_id, uid, now)
                if uid == self.uid:
                    out += render([reply])
            if not out and core.in_quiet_hours(now):
                return ["(quiet hours: only reminders you set go out between 22:00 and 08:00)"]
            return out or ["(nothing to nudge right now)"]
        if cmd == "/advance":
            m = re.fullmatch(r"(\d+)\s*([hd])", arg or "")
            if not m:
                return ["Usage: /advance 6h  or  /advance 3d"]
            n = int(m.group(1))
            self.offset += timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)
            return [f"Clock is now {core.to_local(self.now()):%a %d %b %H:%M}"]
        if cmd == "/time":
            return [f"{core.to_local(now):%a %d %b %H:%M} ({core.TZ.key})"]
        return [f"Unknown command {cmd}. Try /help."]


def render(replies) -> list:
    out = []
    for r in replies:
        out.append(r.text)
        rows = r.buttons if r.buttons and isinstance(r.buttons[0], list) else [r.buttons]
        for row in rows:
            if row:
                out.append("   " + "  ".join(f"[{label}: /tap {data}]" for label, data in row))
    return out


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Chat with Pluto in the terminal.")
    p.add_argument("--offline", action="store_true", help="use the built-in rule-based assistant")
    p.add_argument("--db", default=os.getenv("PLUTO_SIM_DB", "simulate.db"))
    p.add_argument("--user", type=int, default=1, help="pretend Telegram user id")
    args = p.parse_args(argv)

    if args.offline:
        import offline as llm
    else:
        import llm

        if not llm.providers():
            sys.exit(f"No AI key in .env ({llm.KEY_NAMES}). "
                     "Fill one in, or run: python simulate.py --offline")

    db.DB_PATH = args.db
    db.init()
    s = Session(llm, args.user)
    mode = "offline assistant" if args.offline else f"AI: {llm.describe()}"
    print(f"Pluto simulator — {mode}, database {args.db}. /help for commands, /quit to exit.")
    while True:
        try:
            line = input("you> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("/quit", "/exit"):
            break
        for out in s.handle(line):
            print(f"pluto> {out}" if not out.startswith("   ") else out)


if __name__ == "__main__":
    main()
