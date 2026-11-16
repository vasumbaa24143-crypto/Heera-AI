"""Google Doc notebook: what goes in it, when it syncs, and the Google calls (fake HTTP)."""
import asyncio
import json
import pathlib
from datetime import timedelta
from types import SimpleNamespace

import pytest

import bot
import core
import db
import gdoc
import sheets
from conftest import NOW

UID, OTHER = 111, 222
H, D = timedelta(hours=1), timedelta(days=1)
VARS = ("GOOGLE_DOC_ID", "GOOGLE_DOC_APPS_SCRIPT_URL", "GOOGLE_DOC_APPS_SCRIPT_SECRET",
        "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_SHEETS_ID", "GOOGLE_APPS_SCRIPT_URL",
        "GOOGLE_APPS_SCRIPT_SECRET")
SCRIPT = "https://script.google.com/macros/s/AKfycbx_DocDeployment-9/exec"


@pytest.fixture(autouse=True)
def no_google_env(monkeypatch):
    for k in VARS:
        monkeypatch.delenv(k, raising=False)


def add(title, kind="task", due=None, priority="normal", uid=UID):
    return db.add_item(uid, kind, title, title, db.to_str(due) if due else None,
                       now=NOW - D, priority=priority)


def texts(blocks, style=None):
    return [b["text"] for b in blocks if style is None or b["style"] == style]


# ---------- content ----------

def test_notebook_sections():
    late = add("Renew passport", due=NOW - D, priority="high")
    soon = add("Call bank", due=NOW + 3 * H)
    later = add("Book tickets", due=NOW + 5 * D)
    idea = add("YouTube tech news", kind="idea")
    fin = add("Send deck")
    db.set_status(UID, fin, "done", now=NOW - H)
    db.add_fact(UID, "Rahul is a colleague", "permanent", "notes", now=NOW)
    db.add_summary(UID, 1, 1, "- Planning a Goa trip", now=NOW)
    add("someone else's", uid=OTHER)

    blocks = core.doc_blocks(UID, NOW)
    assert blocks[0] == {"style": "title", "text": "Pluto notebook"}
    assert blocks[1]["text"].startswith("Last updated: Mon 02 Nov 2026, 12:00 (Asia/Kolkata)")
    assert texts(blocks, "heading") == ["Today", "Coming up and undated", "Notes and ideas",
                                        "What I know about you", "From our older chats",
                                        "Recently finished"]
    bullets = texts(blocks, "bullet")
    assert bullets[0] == f"#{late} 🔴 Renew passport — due 2026-11-01 12:00 (overdue)"
    assert bullets[1] == f"#{soon} Call bank — due 2026-11-02 15:00"
    assert bullets[2].startswith(f"#{later} Book tickets")
    assert f"#{idea} 💡 YouTube tech news (2026-11-01)" in bullets
    assert "Rahul is a colleague" in bullets and "Planning a Goa trip" in bullets
    assert f"#{fin} Send deck (done, 2026-11-02)" in bullets
    assert not any("someone else" in t for t in texts(blocks))
    assert all(b["style"] in gdoc.STYLES for b in blocks)


def test_empty_notebook_says_so_and_skips_empty_sections():
    blocks = core.doc_blocks(UID, NOW)
    assert texts(blocks, "heading") == ["Today"]
    assert "Nothing overdue or due today." in texts(blocks, "text")


# ---------- when it syncs ----------

def test_sync_only_when_something_changed_and_records_errors():
    add("Call bank")
    pushed = []
    assert core.sync_doc(UID, pushed.append, now=NOW) == "synced"
    assert core.sync_doc(UID, pushed.append, now=NOW + timedelta(minutes=5)) == "unchanged"
    add("New thing")
    assert core.sync_doc(UID, pushed.append, now=NOW + timedelta(minutes=10)) == "synced"
    assert core.sync_doc(UID, pushed.append, now=NOW + timedelta(minutes=11), force=True) == "synced"
    assert len(pushed) == 3

    def broken(blocks):
        raise gdoc.DocError("Google Docs 403: The caller does not have permission")

    with pytest.raises(gdoc.DocError):
        core.sync_doc(UID, broken, now=NOW, force=True)
    assert "403" in db.get_setting(UID, "doc_error")
    text = core.cmd_doc(UID, "", "https://docs.google.com/document/d/X/edit", "bot@p.iam.gserviceaccount.com").text
    assert "Share the doc with bot@p.iam.gserviceaccount.com as Editor" in text


def test_deleting_an_item_changes_the_notebook():
    i = add("Secret plan")
    pushed = []
    core.sync_doc(UID, pushed.append, now=NOW)
    core.cmd_delete(UID, str(i))
    assert core.sync_doc(UID, pushed.append, now=NOW) == "synced"
    assert not any("Secret plan" in t for t in texts(pushed[-1]))


# ---------- setup checks ----------

def test_setup_checks(tmp_path, monkeypatch):
    assert gdoc.problem() == "not set up" and gdoc.mode() == ""
    monkeypatch.setenv("GOOGLE_DOC_APPS_SCRIPT_URL", SCRIPT)
    assert "GOOGLE_DOC_APPS_SCRIPT_SECRET must be set" in gdoc.problem()
    monkeypatch.setenv("GOOGLE_DOC_APPS_SCRIPT_SECRET", "a-long-random-secret-word")
    assert gdoc.problem() == "" and gdoc.mode() == "apps_script"
    monkeypatch.setenv("GOOGLE_DOC_APPS_SCRIPT_URL", "https://evil.example.com/exec")
    assert "ending in /exec" in gdoc.problem()

    monkeypatch.setenv("GOOGLE_DOC_ID", "https://docs.google.com/document/d/doc_ID-123/edit?tab=t.0")
    assert gdoc.mode() == "service_account" and gdoc.doc_id() == "doc_ID-123"
    assert gdoc.problem() == "GOOGLE_SERVICE_ACCOUNT_FILE is missing in .env"
    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"client_email": "pluto@p.iam.gserviceaccount.com"}))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(key))
    assert gdoc.problem() == "" and gdoc.service_account_email() == "pluto@p.iam.gserviceaccount.com"
    assert gdoc.url() == "https://docs.google.com/document/d/doc_ID-123/edit"


def test_a_key_file_for_the_doc_alone_doesnt_upset_the_sheet(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "sa.json"))
    assert sheets.problem() == "not set up"


# ---------- Apps Script path ----------

def test_apps_script_push(monkeypatch):
    monkeypatch.setenv("GOOGLE_DOC_APPS_SCRIPT_URL", SCRIPT)
    monkeypatch.setenv("GOOGLE_DOC_APPS_SCRIPT_SECRET", "a-long-random-secret-word")
    calls = []
    monkeypatch.setattr(gdoc, "_post_script", lambda p: calls.append(p) or
                        {"ok": True, "url": "https://docs.google.com/document/d/FROMSCRIPT/edit"})
    add("Call bank")
    assert core.sync_doc(UID, gdoc.push, now=NOW) == "synced"
    assert calls[0]["secret"] == "a-long-random-secret-word"
    assert calls[0]["blocks"][0] == {"style": "title", "text": "Pluto notebook"}
    assert core.cmd_doc(UID, gdoc.problem(), gdoc.url()).text.startswith(
        "📄 Your notebook: https://docs.google.com/document/d/FROMSCRIPT/edit")

    monkeypatch.setattr(gdoc, "_post_script", lambda p: {"ok": False, "error": "wrong secret"})
    with pytest.raises(gdoc.DocError, match="Apps Script: wrong secret"):
        gdoc.push([])


def test_apps_script_file_has_the_safety_checks():
    gs = (pathlib.Path(gdoc.__file__).parent / "apps_script" / "DocCode.gs").read_text()
    assert "data.secret !== SECRET" in gs
    assert "SECRET.indexOf('CHANGE-ME') === 0" in gs
    assert "ok: true, url: doc.getUrl()" in gs


# ---------- Docs API path ----------

def test_build_requests_replaces_the_body_and_styles_headings():
    blocks = [{"style": "title", "text": "Pluto notebook"},
              {"style": "text", "text": "multi\nline   text"},
              {"style": "heading", "text": "Today 🔴"},
              {"style": "bullet", "text": "#1 Call bank"}]
    reqs = gdoc.build_requests(blocks, end_index=40)
    assert reqs[0] == {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 39}}}
    text = reqs[1]["insertText"]["text"]
    assert reqs[1]["insertText"]["location"] == {"index": 1}
    assert text == "Pluto notebook\nmulti line text\nToday 🔴\n• #1 Call bank\n"
    styles = [(r["updateParagraphStyle"]["range"]["startIndex"], r["updateParagraphStyle"]["range"]["endIndex"],
               r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]) for r in reqs[2:]]
    units = len(text.encode("utf-16-le")) // 2
    assert styles[0] == (1, 1 + units, "NORMAL_TEXT")
    assert styles[1] == (1, 16, "TITLE")
    # "Today 🔴\n" is 9 UTF-16 units (the emoji counts as 2), starting after 15 + 16 units.
    assert styles[2] == (32, 41, "HEADING_1")


def test_build_requests_on_an_empty_doc_skips_the_delete():
    reqs = gdoc.build_requests([{"style": "text", "text": "hi"}], end_index=2)
    assert "insertText" in reqs[0]
    assert gdoc.build_requests([], end_index=2) == []


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.reason = status, body or {}, "Forbidden"
        self.content = json.dumps(self._body).encode()

    def json(self):
        return self._body


def test_api_push_reads_the_length_then_updates(tmp_path, monkeypatch):
    key = tmp_path / "sa.json"
    key.write_text("{}")
    monkeypatch.setenv("GOOGLE_DOC_ID", "doc123")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(key))
    calls = []

    class Sess:
        def request(self, method, url, timeout=None, **kw):
            calls.append((method, url, kw))
            return FakeResp(body={"body": {"content": [{"endIndex": 1}, {"endIndex": 25}]}})

    monkeypatch.setattr(gdoc, "_http", lambda: Sess())
    assert gdoc.push([{"style": "text", "text": "hello"}]) == "https://docs.google.com/document/d/doc123/edit"
    assert calls[0][:2] == ("GET", f"{gdoc.API}/doc123")
    assert calls[1][:2] == ("POST", f"{gdoc.API}/doc123:batchUpdate")
    assert calls[1][2]["json"]["requests"][0]["deleteContentRange"]["range"]["endIndex"] == 24

    class Denied:
        def request(self, *a, **k):
            return FakeResp(403, {"error": {"message": "The caller does not have permission"}})

    monkeypatch.setattr(gdoc, "_http", lambda: Denied())
    with pytest.raises(gdoc.DocError, match="Google Docs 403: The caller does not have permission"):
        gdoc.push([])


# ---------- Telegram side ----------

def test_doc_command_and_job(monkeypatch):
    assert "No Google Doc notebook yet" in core.cmd_doc(UID, "not set up", "").text
    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    monkeypatch.setattr(db, "now_utc", lambda: NOW)
    pushed = []
    monkeypatch.setattr(gdoc, "push", pushed.append)
    asyncio.run(bot.doc_job(SimpleNamespace()))            # not set up: nothing happens
    assert pushed == []
    monkeypatch.setenv("GOOGLE_DOC_APPS_SCRIPT_URL", SCRIPT)
    monkeypatch.setenv("GOOGLE_DOC_APPS_SCRIPT_SECRET", "a-long-random-secret-word")
    add("Call bank")
    asyncio.run(bot.doc_job(SimpleNamespace()))
    asyncio.run(bot.doc_job(SimpleNamespace()))            # unchanged: no second write
    assert len(pushed) == 1
    sent = []

    async def reply_text(text, **kw):
        sent.append(text)

    update = SimpleNamespace(effective_user=SimpleNamespace(id=UID), message=SimpleNamespace(reply_text=reply_text))
    asyncio.run(bot.cmd_doc(update, SimpleNamespace(args=["now"])))
    assert len(pushed) == 2 and sent[0].startswith("📄 Your notebook:")
    assert "Google Doc: ✓ via Apps Script" in bot.key_report()
    assert "/doc" in core.START_TEXT
