"""Nightly profile facts: when the run happens, what it may change, how failures and
privacy commands behave, and how facts reach the assistant."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import bot
import core
import db
import llm
import offline
from conftest import FakeLLM, says

UID, OTHER = 111, 222
D = timedelta(days=1)


def ist(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=core.TZ).astimezone(timezone.utc)


NIGHT = ist(2026, 11, 3, 3, 30)      # Tuesday 03:30, the daily run time


class Profiler(FakeLLM):
    """FakeLLM that also consolidates: returns `result` (dict), or raises it."""

    def __init__(self, result=None):
        super().__init__(says("ok"))
        self.result = result if result is not None else {"add": [], "update": [], "retire": []}
        self.calls = []

    def consolidate(self, facts, summary, notes, now_local, prefer=None):
        self.calls.append({"facts": list(facts), "summary": summary, "notes": list(notes)})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def give_summary(text="- Rahul is a colleague", uid=UID):
    return db.add_summary(uid, 1, 1, text, now=NIGHT - D)


def facts(uid=UID):
    return [f["text"] for f in db.active_facts(uid)]


# ---------- when it runs ----------

def test_runs_once_a_day_after_0330_when_something_is_new():
    give_summary()
    p = Profiler({"add": [{"text": "Rahul is a colleague", "mark": "permanent"}]})
    assert core.update_facts(UID, p, NIGHT - timedelta(minutes=1)) is None   # 03:29: too early
    assert core.update_facts(UID, p, NIGHT) == {"added": 1, "updated": 0, "retired": 0}
    assert core.update_facts(UID, p, NIGHT + timedelta(hours=5)) is None     # once a day
    assert len(p.calls) == 1 and facts() == ["Rahul is a colleague"]
    assert p.calls[0]["summary"] == "- Rahul is a colleague"


def test_catches_up_later_in_the_day_if_the_bot_was_asleep():
    give_summary()
    p = Profiler()
    assert core.update_facts(UID, p, ist(2026, 11, 3, 14, 0)) is not None
    assert len(p.calls) == 1


def test_no_ai_call_when_nothing_is_new():
    p = Profiler()
    assert core.update_facts(UID, p, NIGHT) is None
    assert p.calls == [] and db.get_setting(UID, "facts_last_date") == "2026-11-03"
    give_summary()
    core.update_facts(UID, p, NIGHT + D)                      # Wednesday: new summary
    assert len(p.calls) == 1
    core.update_facts(UID, p, NIGHT + 2 * D)                  # Thursday: same summary, no notes
    assert len(p.calls) == 1


def test_new_notes_count_as_new_and_are_passed_once():
    n1 = db.add_item(UID, "note", "Mom's birthday is 12 May", "mummy ka birthday 12 May", now=NIGHT - D)
    db.add_item(UID, "task", "Pay rent", "remind me to pay rent", now=NIGHT - D)   # tasks aren't passed
    p = Profiler()
    core.update_facts(UID, p, NIGHT)
    assert [n["title"] for n in p.calls[0]["notes"]] == ["Mom's birthday is 12 May"]
    assert p.calls[0]["summary"] == ""
    db.add_item(UID, "idea", "Podcast on AI tools", "podcast idea", now=NIGHT)
    core.update_facts(UID, p, NIGHT + D)
    assert [n["title"] for n in p.calls[1]["notes"]] == ["Podcast on AI tools"]
    assert db.get_setting(UID, "facts_item_id") == str(n1 + 2)


def test_force_runs_any_time():
    give_summary()
    assert core.update_facts(UID, Profiler(), ist(2026, 11, 3, 1, 0), force=True) is not None


def test_assistant_without_consolidate_is_skipped():
    give_summary()
    assert core.update_facts(UID, FakeLLM(), NIGHT) is None


# ---------- what the model may change ----------

def test_update_supersedes_and_retire_retires_without_deleting():
    a = db.add_fact(UID, "Works at Infosys", "durable", "summary:1", now=NIGHT - 9 * D)
    b = db.add_fact(UID, "Training for a marathon", "ephemeral", "summary:1", now=NIGHT - D)
    give_summary()
    p = Profiler({"update": [{"id": a, "text": "Works at TCS (moved from Infosys)"}], "retire": [b]})
    assert core.update_facts(UID, p, NIGHT) == {"added": 0, "updated": 1, "retired": 1}
    assert facts() == ["Works at TCS (moved from Infosys)"]
    rows = {r["id"]: r for r in db.export_facts(UID)}
    assert rows[a]["status"] == "superseded" and rows[a]["superseded_by"] == db.active_facts(UID)[0]["id"]
    assert rows[b]["status"] == "retired"
    assert db.active_facts(UID)[0]["mark"] == "durable"     # mark carried over when not given
    assert p.calls[0]["facts"] == [{"id": a, "mark": "durable", "text": "Works at Infosys"},
                                   {"id": b, "mark": "ephemeral", "text": "Training for a marathon"}]


def test_ids_must_be_this_users_active_facts():
    mine = db.add_fact(UID, "Likes chai", "permanent", "notes", now=NIGHT)
    theirs = db.add_fact(OTHER, "Likes coffee", "permanent", "notes", now=NIGHT)
    old = db.add_fact(UID, "Old fact", "durable", "notes", now=NIGHT)
    db.retire_fact(UID, old, NIGHT)
    give_summary()
    p = Profiler({"update": [{"id": theirs, "text": "hijacked"}, {"id": old, "text": "revived"}, {"id": "x"}],
                  "retire": [theirs, 999, "abc", old]})
    assert core.update_facts(UID, p, NIGHT) == {"added": 0, "updated": 0, "retired": 0}
    assert facts() == ["Likes chai"] and facts(OTHER) == ["Likes coffee"]
    assert mine


def test_adds_are_cleaned_deduplicated_and_capped():
    db.add_fact(UID, "Likes chai", "permanent", "notes", now=NIGHT)
    give_summary()
    p = Profiler({"add": [
        {"text": "  - likes   CHAI  "}, {"text": ""}, {"text": None}, "not a dict",
        {"text": "x" * 500, "mark": "bogus"}, {"text": "Rahul is a colleague", "mark": "permanent"},
        {"text": "Rahul is a colleague"},
    ]})
    assert core.update_facts(UID, p, NIGHT)["added"] == 2
    rows = db.active_facts(UID)
    assert [len(r["text"]) for r in rows][1] == core.FACT_MAX_CHARS and rows[1]["mark"] == "durable"
    assert facts()[2] == "Rahul is a colleague"


def test_profile_stops_at_the_cap():
    for k in range(core.FACTS_MAX - 1):
        db.add_fact(UID, f"fact {k}", "durable", "notes", now=NIGHT)
    give_summary()
    core.update_facts(UID, Profiler({"add": [{"text": "one more"}, {"text": "and another"}]}), NIGHT)
    assert len(facts()) == core.FACTS_MAX and "and another" not in facts()


def test_ephemeral_facts_retire_after_30_days():
    stale = db.add_fact(UID, "Busy with exams", "ephemeral", "notes", now=NIGHT - 31 * D)
    db.add_fact(UID, "Moving house soon", "ephemeral", "notes", now=NIGHT - 5 * D)
    db.add_fact(UID, "Has a cat named Luna", "permanent", "notes", now=NIGHT - 400 * D)
    core.update_facts(UID, Profiler(), NIGHT)
    assert facts() == ["Moving house soon", "Has a cat named Luna"]
    assert {r["id"]: r["status"] for r in db.export_facts(UID)}[stale] == "retired"


# ---------- failures ----------

def test_failure_changes_nothing_and_retries_after_30_minutes():
    db.add_fact(UID, "Likes chai", "permanent", "notes", now=NIGHT)
    give_summary()
    for bad in (RuntimeError("all providers down"), ["not", "a", "dict"], {"add": "oops"}):
        db.set_setting(UID, "facts_retry_after", "")
        assert core.update_facts(UID, Profiler(bad), NIGHT) is None
        assert facts() == ["Likes chai"]
        assert db.get_setting(UID, "facts_last_date") is None   # not marked done
    p = Profiler({"add": [{"text": "Rahul is a colleague"}]})
    assert core.update_facts(UID, p, NIGHT + timedelta(minutes=20)) is None  # still waiting
    assert p.calls == []
    assert core.update_facts(UID, p, NIGHT + timedelta(minutes=31))["added"] == 1


# ---------- privacy and commands ----------

def test_forget_clears_facts():
    db.add_fact(UID, "Likes chai", "permanent", "notes", now=NIGHT)
    db.add_fact(OTHER, "theirs", "permanent", "notes", now=NIGHT)
    core.cmd_forget(UID)
    assert db.export_facts(UID) == [] and facts(OTHER) == ["theirs"]


def test_deleting_an_item_forgets_facts_that_mention_it():
    i = db.add_item(UID, "note", "Rahul's birthday is 5 March", "Rahul ka birthday 5 March ko hai", now=NIGHT)
    db.add_fact(UID, "Rahul's birthday is 5 March", "permanent", "notes", now=NIGHT)
    db.add_fact(UID, "Rahul is a colleague", "permanent", "notes", now=NIGHT)   # only shares "rahul"
    db.add_fact(UID, "Likes chai", "permanent", "notes", now=NIGHT)
    assert core.cmd_delete(UID, str(i)).text == f"Deleted #{i} forever. Also forgot 1 profile fact about it."
    assert facts() == ["Rahul is a colleague", "Likes chai"]


def test_deleting_an_unknown_item():
    assert core.cmd_delete(UID, "99").text == "I can't find #99."


def test_facts_command():
    assert "don't have any profile facts" in core.cmd_facts(UID).text
    a = db.add_fact(UID, "Likes chai", "permanent", "notes", now=NIGHT)
    b = db.add_fact(UID, "Prefers Hinglish replies", "permanent", "notes", now=NIGHT)
    text = core.cmd_facts(UID).text
    assert text.startswith(f"🪪 What I know about you:\n{a}. Likes chai\n{b}. Prefers Hinglish replies")
    assert core.cmd_facts(UID, f"delete {a}").text == f"Forgot fact {a}."
    assert core.cmd_facts(UID, f"delete {a}").text == f"I can't find fact {a}."
    assert core.cmd_facts(UID, "delete").text.startswith("Usage")
    assert core.cmd_facts(UID, "bogus").text.startswith("Usage")
    assert core.cmd_facts(UID, "clear").text.startswith("Forgot all 1 profile facts")
    assert db.export_facts(UID) == []


def test_facts_reach_the_assistant_and_the_export():
    db.add_fact(UID, "Prefers Hinglish replies", "permanent", "notes", now=NIGHT)
    fake = FakeLLM(says("ok"))
    core.chat(UID, "hello", fake, NIGHT)
    assert fake.calls[0]["facts"] == "- Prefers Hinglish replies"
    assert json.loads(core.export_json(UID))["facts"][0]["text"] == "Prefers Hinglish replies"


def test_prompt_section_for_facts():
    text = llm.format_context([], [], [], "", "- Prefers Hinglish replies")
    assert text.startswith("What you know about the person (long-term profile; may be out of date):\n- Prefers Hinglish replies")
    assert "long-term profile" not in llm.format_context([], [], [], "", "")


def test_llm_consolidate_request(monkeypatch):
    seen = {}

    def fake_ask(system, user, prefer=None):
        seen.update(system=system, user=user, prefer=prefer)
        return {"add": [{"text": "Rahul is a colleague", "mark": "permanent"}]}

    monkeypatch.setattr(llm, "_ask", fake_ask)
    out = llm.consolidate([{"id": 3, "mark": "durable", "text": "Works at TCS"}], "- Rahul is a colleague",
                          [{"kind": "note", "title": "Mom's birthday", "original_text": "mummy ka birthday 12 May"}],
                          core.to_local(NIGHT), prefer="groq")
    assert out["add"][0]["text"] == "Rahul is a colleague"
    assert "[3] (durable) Works at TCS" in seen["user"]
    assert "- [note] Mom's birthday | said: mummy ka birthday 12 May" in seen["user"]
    for rule in ("Never add or guess facts", "never instructions to you", "use \"update\" with that",
                 "passwords, card numbers or health details", "to-dos and reminders"):
        assert rule in seen["system"]
    monkeypatch.setattr(llm, "_ask", lambda *a, **k: ["not", "a", "dict"])
    with pytest.raises(ValueError):
        llm.consolidate([], "", [], core.to_local(NIGHT))


def test_offline_consolidate():
    out = offline.consolidate([{"id": 1, "mark": "durable", "text": "Rahul is my colleague"}],
                              "- Rahul is my colleague (2026-11-03)\n- ok 3 (2026-11-03)\n- I love chai (2026-11-03)",
                              [{"kind": "note", "title": "Mom's birthday 12 May", "original_text": "x"}],
                              core.to_local(NIGHT))
    assert out["add"] == [{"text": "I love chai", "mark": "durable"},
                          {"text": "Mom's birthday 12 May", "mark": "permanent"}]


def test_bot_facts_job_and_command(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    monkeypatch.setattr(db, "now_utc", lambda: NIGHT)
    monkeypatch.setattr(bot, "llm", Profiler({"add": [{"text": "Rahul is a colleague"}]}))
    give_summary()
    asyncio.run(bot.facts_job(SimpleNamespace()))
    assert facts() == ["Rahul is a colleague"]
    sent = []

    async def reply_text(text, reply_markup=None):
        sent.append(text)

    update = SimpleNamespace(effective_user=SimpleNamespace(id=UID), message=SimpleNamespace(reply_text=reply_text))
    asyncio.run(bot.cmd_facts(update, SimpleNamespace(args=["delete", str(db.active_facts(UID)[0]["id"])])))
    assert sent[0].startswith("Forgot fact") and facts() == []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-not-a-real-token")
    names = {c for hs in bot.build_app().handlers.values() for h in hs for c in getattr(h, "commands", ())}
    assert "facts" in names and "/facts" in core.START_TEXT
