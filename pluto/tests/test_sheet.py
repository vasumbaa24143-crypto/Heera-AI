"""Google Sheet dashboard: what goes in each tab, when it syncs, and the Google calls
(with a fake HTTP session; no network)."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

import bot
import core
import db
import sheets
from conftest import NOW

UID, OTHER = 111, 222
H, D = timedelta(hours=1), timedelta(days=1)
GOOGLE_VARS = ("GOOGLE_SHEETS_ID", "GOOGLE_SERVICE_ACCOUNT_FILE",
               "GOOGLE_APPS_SCRIPT_URL", "GOOGLE_APPS_SCRIPT_SECRET")


@pytest.fixture(autouse=True)
def no_google_env(monkeypatch):
    for k in GOOGLE_VARS:
        monkeypatch.delenv(k, raising=False)


def add(title, kind="task", due=None, priority="normal", text=None, uid=UID, created=NOW - D):
    return db.add_item(uid, kind, title, text or title, db.to_str(due) if due else None,
                       now=created, priority=priority)


# ---------- what's in the tabs ----------

def test_tabs_and_rows():
    late = add("Renew passport", due=NOW - D, priority="high")
    soon = add("Call bank", due=NOW + 3 * H)
    undated = add("Clean garage")
    remind_note = add("Check results", kind="note", due=NOW + 2 * D)   # a dated note is a reminder
    idea = add("YouTube tech news", kind="idea")
    note = add("Wifi password in the drawer", kind="note")
    fin = add("Send deck", due=NOW - 2 * D)
    db.set_status(UID, fin, "done", now=NOW - H)
    add("someone else's", uid=OTHER)

    t = core.sheet_tables(UID, NOW)
    assert list(t) == list(core.SHEET_TABS)
    todo_ids = [r[0] for r in t["To-dos"][1:]]
    assert todo_ids == [late, soon, remind_note, undated]     # overdue, today, later, no date
    assert t["To-dos"][0] == ["ID", "To-do", "Type", "Priority", "Due", "When", "Added", "Your words"]
    assert t["To-dos"][1] == [late, "Renew passport", "task", "high", "2026-11-01 12:00", "overdue",
                              "2026-11-01 12:00", "Renew passport"]
    assert [r[0] for r in t["Notes & ideas"][1:]] == [note, idea]
    assert t["Done"][1][:5] == [fin, "Send deck", "task", "done", "2026-11-02 11:00"]

    dash = dict((r[0], r[1]) for r in t["Dashboard"] if r[0])
    assert dash["⚠️ Overdue"] == 1 and dash["📅 Due today"] == 1
    assert dash["🔴 Urgent (high priority)"] == 1 and dash["✅ Open to-dos"] == 4
    assert dash["💡 Open ideas"] == 1 and dash["📝 Notes"] == 1
    assert dash["🎉 Finished in the last 7 days"] == 1
    assert dash["Last updated"] == "2026-11-02 12:00 (Asia/Kolkata)"


def test_finished_count_uses_when_it_was_closed():
    old = add("old one")
    db.set_status(UID, old, "done", now=NOW - 10 * D)
    dropped = add("dropped", kind="idea")
    db.set_status(UID, dropped, "archived", now=NOW)
    dash = dict((r[0], r[1]) for r in core.sheet_tables(UID, NOW)["Dashboard"] if r[0])
    assert dash["🎉 Finished in the last 7 days"] == 0
    assert db.get_item(UID, old)["closed_at"] == db.to_str(NOW - 10 * D)
    db.set_status(UID, old, "open")
    assert db.get_item(UID, old)["closed_at"] is None


def test_typed_formulas_stay_plain_text():
    add("=HYPERLINK(\"http://evil\",\"click\")", text="=IMPORTXML(\"http://evil\",\"//a\")")
    row = core.sheet_tables(UID, NOW)["To-dos"][1]
    assert row[1].startswith("=") and isinstance(row[1], str)   # kept as text; written RAW below


# ---------- when it syncs ----------

def test_sync_only_when_something_changed():
    add("Call bank", due=NOW + 3 * H)
    pushed = []
    assert core.sync_sheet(UID, pushed.append, now=NOW) == "synced"
    assert core.sync_sheet(UID, pushed.append, now=NOW + timedelta(minutes=5)) == "unchanged"
    add("New thing")
    assert core.sync_sheet(UID, pushed.append, now=NOW + timedelta(minutes=10)) == "synced"
    assert core.sync_sheet(UID, pushed.append, now=NOW + timedelta(minutes=15), force=True) == "synced"
    assert len(pushed) == 3
    assert db.get_setting(UID, "sheet_synced_at") == db.to_str(NOW + timedelta(minutes=15))


def test_time_moving_on_counts_as_a_change():
    add("Call bank", due=NOW + 3 * H)
    pushed = []
    core.sync_sheet(UID, pushed.append, now=NOW)
    core.sync_sheet(UID, pushed.append, now=NOW + 4 * H)       # "today" became "overdue"
    assert len(pushed) == 2


def test_failure_is_recorded_and_retried():
    add("Call bank")

    def broken(tables):
        raise sheets.SheetError("Google Sheets 403: The caller does not have permission")

    with pytest.raises(sheets.SheetError):
        core.sync_sheet(UID, broken, now=NOW)
    assert db.get_setting(UID, "sheet_hash") is None
    assert "403" in db.get_setting(UID, "sheet_error")
    assert core.sync_sheet(UID, lambda t: None, now=NOW) == "synced"
    assert db.get_setting(UID, "sheet_error") == ""


# ---------- the Google calls ----------

class FakeResp:
    def __init__(self, status=200, body=None, reason="OK"):
        self.status_code, self._body, self.reason = status, body or {}, reason
        self.content = json.dumps(self._body).encode()

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, existing=("Sheet1",), fail=None):
        self.calls, self.existing, self.fail = [], list(existing), fail
        self.next_id = 100

    def request(self, method, url, timeout=None, **kw):
        self.calls.append(SimpleNamespace(method=method, url=url, **kw))
        if self.fail:
            return self.fail
        if method == "GET":
            return FakeResp(body={"sheets": [{"properties": {"title": t, "sheetId": i}}
                                             for i, t in enumerate(self.existing)]})
        if url.endswith(":batchUpdate") and "addSheet" in json.dumps(kw.get("json")):
            replies = []
            for r in kw["json"]["requests"]:
                self.next_id += 1
                replies.append({"addSheet": {"properties": {"title": r["addSheet"]["properties"]["title"],
                                                            "sheetId": self.next_id}}})
            return FakeResp(body={"replies": replies})
        return FakeResp(body={})


@pytest.fixture
def configured(tmp_path, monkeypatch):
    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"client_email": "pluto-bot@my-proj.iam.gserviceaccount.com"}))
    monkeypatch.setenv("GOOGLE_SHEETS_ID", "https://docs.google.com/spreadsheets/d/abc123_-XYZ/edit#gid=0")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(key))
    return key


def test_push_creates_missing_tabs_then_rewrites_with_raw_values(configured, monkeypatch):
    sess = FakeSession(existing=("Sheet1", "Dashboard"))
    monkeypatch.setattr(sheets, "_http", lambda: sess)
    add("=HYPERLINK(\"http://evil\")")
    sheets.push(core.sheet_tables(UID, NOW))

    assert all(c.url.startswith(f"{sheets.API}/abc123_-XYZ") for c in sess.calls)
    add_req = sess.calls[1].json["requests"]
    assert [r["addSheet"]["properties"]["title"] for r in add_req] == ["To-dos", "Done", "Notes & ideas"]
    assert all(r["addSheet"]["properties"]["gridProperties"]["frozenRowCount"] == 1 for r in add_req)
    assert sess.calls[2].json["requests"][0]["repeatCell"]["cell"]["userEnteredFormat"]["textFormat"]["bold"]
    assert sess.calls[3].url.endswith("/values:batchClear")
    assert sess.calls[3].json["ranges"] == ["'Dashboard'", "'To-dos'", "'Done'", "'Notes & ideas'"]
    write = sess.calls[4].json
    assert sess.calls[4].url.endswith("/values:batchUpdate")
    assert write["valueInputOption"] == "RAW"                  # never parsed as formulas
    assert write["data"][3]["range"] == "'Notes & ideas'!A1"
    assert write["data"][1]["values"][1][1] == "=HYPERLINK(\"http://evil\")"


def test_push_skips_creating_tabs_that_exist(configured, monkeypatch):
    sess = FakeSession(existing=core.SHEET_TABS)
    monkeypatch.setattr(sheets, "_http", lambda: sess)
    sheets.push(core.sheet_tables(UID, NOW))
    assert [c.url.rsplit("/", 1)[-1] for c in sess.calls] == ["abc123_-XYZ", "values:batchClear", "values:batchUpdate"]


def test_google_errors_are_readable(configured, monkeypatch):
    sess = FakeSession(fail=FakeResp(403, {"error": {"message": "The caller does not have permission"}}))
    monkeypatch.setattr(sheets, "_http", lambda: sess)
    with pytest.raises(sheets.SheetError, match="403: The caller does not have permission"):
        sheets.push(core.sheet_tables(UID, NOW))


def test_setup_checks(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_SHEETS_ID", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    assert sheets.problem() == "not set up" and not sheets.enabled() and sheets.url() == ""
    monkeypatch.setenv("GOOGLE_SHEETS_ID", "abc123")
    assert sheets.problem() == "GOOGLE_SERVICE_ACCOUNT_FILE is missing in .env"
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "nope.json"))
    assert "doesn't exist" in sheets.problem()
    with pytest.raises(sheets.SheetError):
        sheets.push({})
    assert sheets.url() == "https://docs.google.com/spreadsheets/d/abc123/edit"


def test_sheet_id_from_url_and_robot_email(configured):
    assert sheets.sheet_id() == "abc123_-XYZ"
    assert sheets.problem() == ""
    assert sheets.service_account_email() == "pluto-bot@my-proj.iam.gserviceaccount.com"


# ---------- Telegram side ----------

def test_sheet_command_messages(configured):
    assert "No Google Sheet dashboard yet" in core.cmd_sheet(UID, "not set up", "").text
    assert "can't sync: GOOGLE_SHEETS_ID is missing" in core.cmd_sheet(UID, "GOOGLE_SHEETS_ID is missing in .env", "").text
    ok = core.cmd_sheet(UID, "", sheets.url()).text
    assert ok.startswith("📊 Your dashboard: https://docs.google.com/spreadsheets/d/abc123_-XYZ/edit")
    assert "Last updated: not yet" in ok
    db.set_setting(UID, "sheet_error", "Google Sheets 403: The caller does not have permission")
    text = core.cmd_sheet(UID, "", sheets.url(), sheets.service_account_email()).text
    assert "Share the sheet with pluto-bot@my-proj.iam.gserviceaccount.com as Editor" in text


def test_sheet_job_does_nothing_when_not_set_up(monkeypatch):
    monkeypatch.delenv("GOOGLE_SHEETS_ID", raising=False)
    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    monkeypatch.setattr(sheets, "push", lambda t: (_ for _ in ()).throw(AssertionError("called")))
    asyncio.run(bot.sheet_job(SimpleNamespace()))


def test_sheet_job_syncs_the_allowed_user(configured, monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    monkeypatch.setattr(db, "now_utc", lambda: NOW)
    pushed = []
    monkeypatch.setattr(sheets, "push", pushed.append)
    add("Call bank")
    asyncio.run(bot.sheet_job(SimpleNamespace()))
    asyncio.run(bot.sheet_job(SimpleNamespace()))              # nothing changed: no second write
    assert len(pushed) == 1 and pushed[0]["To-dos"][1][1] == "Call bank"


def test_sheet_now_command_forces_a_sync(configured, monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    pushed, sent = [], []
    monkeypatch.setattr(sheets, "push", pushed.append)

    async def reply_text(text, **kw):
        sent.append(text)

    update = SimpleNamespace(effective_user=SimpleNamespace(id=UID),
                             message=SimpleNamespace(reply_text=reply_text))
    asyncio.run(bot.cmd_sheet(update, SimpleNamespace(args=["now"])))
    asyncio.run(bot.cmd_sheet(update, SimpleNamespace(args=["now"])))
    assert len(pushed) == 2 and sent[-1].startswith("📊 Your dashboard:")


def test_key_check_shows_the_sheet(configured):
    assert "Google Sheet: ✓ via service account" in bot.key_report()


# ---------- Apps Script link ----------

SCRIPT = "https://script.google.com/macros/s/AKfycbx_TestDeployment-123/exec"


@pytest.fixture
def script(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_URL", SCRIPT)
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_SECRET", "a-long-random-secret-word")
    calls = []

    def fake_post(payload):
        calls.append(payload)
        return {"ok": True, "url": "https://docs.google.com/spreadsheets/d/FROMSCRIPT/edit"}

    monkeypatch.setattr(sheets, "_post_script", fake_post)
    return calls


def test_apps_script_is_chosen_and_validated(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_URL", SCRIPT)
    assert sheets.mode() == "apps_script"
    assert "GOOGLE_APPS_SCRIPT_SECRET must be set" in sheets.problem()
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_SECRET", "short")
    assert "at least 16" in sheets.problem()
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_SECRET", "a-long-random-secret-word")
    assert sheets.problem() == "" and sheets.enabled()
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_URL", "https://evil.example.com/exec")
    assert "ending in /exec" in sheets.problem()
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_URL", SCRIPT + "/../dev")
    assert sheets.problem() != ""


def test_service_account_wins_when_both_are_set(configured, monkeypatch):
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_URL", SCRIPT)
    assert sheets.mode() == "service_account"
    assert sheets.service_account_email().startswith("pluto-bot@")


def test_apps_script_push_sends_secret_and_safe_text(script):
    add("=HYPERLINK(\"http://evil\")", text="+cmd|calc")
    add("normal text")
    link = sheets.push(core.sheet_tables(UID, NOW))
    assert link == "https://docs.google.com/spreadsheets/d/FROMSCRIPT/edit"
    [payload] = script
    assert payload["secret"] == "a-long-random-secret-word"
    assert list(payload["tables"]) == list(core.SHEET_TABS)
    rows = payload["tables"]["To-dos"]
    risky = next(r for r in rows if "HYPERLINK" in str(r[1]))
    assert risky[1] == "'=HYPERLINK(\"http://evil\")" and risky[7] == "'+cmd|calc"
    assert any(r[1] == "normal text" for r in rows)
    assert isinstance(rows[1][0], int)                       # numbers stay numbers


def test_as_text():
    assert [sheets.as_text(v) for v in ["=1+1", "+1", "-x", "@me", "ok", "'=already", 5, None]] == \
        ["'=1+1", "'+1", "'-x", "'@me", "ok", "'=already", 5, None]


def test_apps_script_errors_are_readable(script, monkeypatch):
    monkeypatch.setattr(sheets, "_post_script", lambda p: {"ok": False, "error": "wrong secret"})
    with pytest.raises(sheets.SheetError, match="Apps Script: wrong secret"):
        sheets.push(core.sheet_tables(UID, NOW))

    def unreachable(p):
        raise sheets.httpx.ConnectError("no route")

    monkeypatch.setattr(sheets, "_post_script", unreachable)
    with pytest.raises(sheets.SheetError, match="couldn't reach the Apps Script link"):
        sheets.push(core.sheet_tables(UID, NOW))


def test_link_learned_from_the_script_is_shown(script):
    add("Call bank")
    core.sync_sheet(UID, sheets.push, now=NOW)
    assert db.get_setting(UID, "sheet_url") == "https://docs.google.com/spreadsheets/d/FROMSCRIPT/edit"
    text = core.cmd_sheet(UID, sheets.problem(), sheets.url()).text
    assert text.startswith("📊 Your dashboard: https://docs.google.com/spreadsheets/d/FROMSCRIPT/edit")


def test_not_set_up_message_points_to_apps_script():
    assert "GOOGLE_APPS_SCRIPT_URL" in core.cmd_sheet(UID, "not set up", "").text


def test_key_check_shows_apps_script(script):
    assert "Google Sheet: ✓ via Apps Script" in bot.key_report()


def test_apps_script_file_has_the_safety_checks():
    import pathlib
    gs = (pathlib.Path(sheets.__file__).parent / "apps_script" / "Code.gs").read_text()
    assert "body.secret !== SECRET" in gs                     # refuses requests without the secret
    assert "SECRET.indexOf('CHANGE-ME') === 0" in gs          # refuses to run with the placeholder
    assert "TABS.indexOf(name) === -1" in gs                  # only touches Pluto's own tabs
    assert "/^[=+\\-@]/.test(s)" in gs                        # formula-looking text stays text
    assert "setValues(values)" in gs and "ok: true, url: ss.getUrl()" in gs


# ---------- the Claude route: --dashboard-json ----------

def test_dashboard_json_prints_the_tabs(monkeypatch, capsys):
    monkeypatch.setattr(bot, "ALLOWED", str(UID))
    monkeypatch.setattr(bot.sys, "argv", ["bot.py", "--dashboard-json"])
    add("Call bank")
    add("not mine", uid=OTHER)
    bot.main()
    data = json.loads(capsys.readouterr().out)
    assert list(data) == list(core.SHEET_TABS)
    assert [r[1] for r in data["To-dos"][1:]] == ["Call bank"]


def test_dashboard_json_needs_the_allowed_user(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", "")
    with pytest.raises(SystemExit, match="ALLOWED_USER_ID"):
        bot.dashboard_json()
