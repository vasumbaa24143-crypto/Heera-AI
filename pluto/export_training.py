"""Export Pluto's conversation as chat-style JSONL, for evaluating or fine-tuning a model.

    python export_training.py                # -> training/conversations.jsonl
    python export_training.py --dry-run      # only print the counts, write nothing
    python export_training.py --gap-hours 12 --out /path/to/file.jsonl

It reads the `messages` table read-only, so it holds exactly what Pluto still remembers:
anything removed with /delete or /forget is gone and is not exported. Each run rewrites the
file, so export again after deleting. The file stays on this machine (`training/` and
*.jsonl are git-ignored). Your messages go in as written, except keys in a few known
formats (Google, Groq, NVIDIA, sk-..., Telegram bot tokens), which become [REDACTED_KEY];
a key in any other format is not detected, so treat the file as private. Nothing is sent
anywhere.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROLES = {"user": "user", "pluto": "assistant"}
# Recognised key formats only: Google, Groq, NVIDIA, sk-..., Telegram bot tokens.
# A key in any other format is exported as written.
SECRET = re.compile(
    r"AIza[0-9A-Za-z_-]{20,}|gsk_[0-9A-Za-z]{20,}|nvapi-[0-9A-Za-z_-]{20,}"
    r"|sk-[0-9A-Za-z_-]{20,}|\d{8,10}:[0-9A-Za-z_-]{30,}"
)


def redact(text: str) -> str:
    return SECRET.sub("[REDACTED_KEY]", text)


def read_messages(db_path) -> list:
    """Every stored message, grouped by user and oldest first. The database is opened read-only."""
    con = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT user_id, role, text, created_at FROM messages ORDER BY user_id, id"
        ).fetchall()
    finally:
        con.close()


def _when(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sessions(rows, gap_hours=6.0) -> list:
    """Group one user's messages (oldest first) into chats, split by long silences."""
    out, last = [], None
    for r in rows:
        role = ROLES.get(r["role"])
        if role is None:
            continue
        at = _when(r["created_at"])
        if last is None or at - last > timedelta(hours=gap_hours):
            out.append({"started_at": r["created_at"], "messages": []})
        last = at
        msgs, text = out[-1]["messages"], redact(r["text"])
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + text  # keep user/assistant turns alternating
        else:
            msgs.append({"role": role, "content": text})
    return out


def write(chats, out_path) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for i, chat in enumerate(chats, 1):
            f.write(json.dumps({"session": i, **chat}, ensure_ascii=False) + "\n")
    os.chmod(out, 0o600)  # in case the file already existed with looser permissions


def export(db_path, out_path=None, gap_hours=6.0) -> tuple:
    """Return (turns, sessions); write the file unless out_path is None."""
    by_user = {}
    for r in read_messages(db_path):
        by_user.setdefault(r["user_id"], []).append(r)
    chats = [s for rows in by_user.values() for s in sessions(rows, gap_hours)]
    if out_path is not None:
        write(chats, out_path)
    return sum(len(s["messages"]) for s in chats), len(chats)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Export Pluto's conversation as chat-style JSONL.")
    p.add_argument("--db", default=os.getenv("PLUTO_DB", "pluto.db"))
    p.add_argument("--out", default=str(HERE / "training" / "conversations.jsonl"))
    p.add_argument("--gap-hours", type=float, default=6.0,
                   help="a silence longer than this starts a new chat (default 6)")
    p.add_argument("--dry-run", action="store_true", help="print the counts, write nothing")
    args = p.parse_args(argv)

    try:
        turns, chats = export(args.db, None if args.dry_run else args.out, args.gap_hours)
    except sqlite3.Error as e:
        sys.exit(f"Could not read {args.db}: {e}")
    print(f"Pluto remembers {turns} turns in {chats} chats.")
    if not args.dry_run:
        print(f"Wrote {args.out} (plain text, readable only by you; re-export after any /delete).")


if __name__ == "__main__":
    main()
