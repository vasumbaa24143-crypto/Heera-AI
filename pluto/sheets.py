"""Keep a Google Sheet dashboard of the user's items (one-way: Pluto -> Sheet).

Two ways to connect (see the README, "Google Sheet dashboard"):

1. Apps Script link (easiest, no Google Cloud): paste apps_script/Code.gs into the sheet's
   Extensions -> Apps Script, deploy it as a web app, and set in .env:
       GOOGLE_APPS_SCRIPT_URL=https://script.google.com/macros/s/.../exec
       GOOGLE_APPS_SCRIPT_SECRET=<the same long random word as in the script>

2. Service account: a Google Cloud service account with the Sheets API enabled, its JSON
   key file outside this folder, and the sheet shared with its email as Editor:
       GOOGLE_SHEETS_ID=<the sheet's id, or its full URL>
       GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json

If both are set, the service account is used. Either way, text from chat can never become
a formula: the API path writes RAW values, and the Apps Script path prefixes risky text
with an apostrophe (Sheets' standard "this is text" marker).
"""
import json
import logging
import os
import re

import httpx

log = logging.getLogger("pluto")

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TIMEOUT = 30
SCRIPT_TIMEOUT = 90
MIN_SECRET = 16
_session = None


class SheetError(RuntimeError):
    pass


# ---------- configuration ----------

def sheet_id() -> str:
    """GOOGLE_SHEETS_ID may be the bare id or the sheet's URL."""
    raw = (os.getenv("GOOGLE_SHEETS_ID") or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", raw)
    return m.group(1) if m else raw


def key_file() -> str:
    return os.path.expanduser((os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip())


def script_url() -> str:
    return (os.getenv("GOOGLE_APPS_SCRIPT_URL") or "").strip()


def script_secret() -> str:
    return (os.getenv("GOOGLE_APPS_SCRIPT_SECRET") or "").strip()


def mode() -> str:
    """"service_account", "apps_script", or "" (not set up)."""
    if sheet_id() and key_file():
        return "service_account"
    if script_url():
        return "apps_script"
    if sheet_id() or key_file():
        return "service_account"  # half set up: problem() explains what's missing
    return ""


def enabled() -> bool:
    return mode() != "" and problem() == ""


def url() -> str:
    """The sheet's address, if we know it (the Apps Script path learns it on first sync)."""
    return f"https://docs.google.com/spreadsheets/d/{sheet_id()}/edit" if sheet_id() else ""


def problem() -> str:
    """Why syncing can't work, in plain words ("" when the setup looks fine)."""
    m = mode()
    if not m:
        return "not set up"
    if m == "apps_script":
        if not re.match(r"^https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec$", script_url()):
            return "GOOGLE_APPS_SCRIPT_URL should be the web app link ending in /exec"
        if len(script_secret()) < MIN_SECRET:
            return f"GOOGLE_APPS_SCRIPT_SECRET must be set (at least {MIN_SECRET} characters)"
        return ""
    if not sheet_id():
        return "GOOGLE_SHEETS_ID is missing in .env"
    if not key_file():
        return "GOOGLE_SERVICE_ACCOUNT_FILE is missing in .env"
    if not os.path.isfile(key_file()):
        return "the service account key file named in GOOGLE_SERVICE_ACCOUNT_FILE doesn't exist"
    return ""


def service_account_email() -> str:
    if mode() != "service_account":
        return ""
    try:
        with open(key_file(), encoding="utf-8") as f:
            return json.load(f).get("client_email", "")
    except (OSError, ValueError):
        return ""


# ---------- writing ----------

def push(tables: dict) -> str:
    """Write {tab: rows} to the sheet. Returns the sheet's URL ("" if unknown)."""
    if problem():
        raise SheetError(problem())
    if mode() == "apps_script":
        return _push_apps_script(tables)
    _push_api(tables)
    return url()


_RISKY = ("=", "+", "-", "@")


def as_text(value):
    """Make a cell safe for Apps Script's setValues(), which treats "=..." as a formula:
    risky strings get Sheets' leading apostrophe, which marks the cell as plain text."""
    if isinstance(value, str) and value.startswith(_RISKY):
        return "'" + value
    return value


def _post_script(payload):
    """One POST to the web app; separated out so tests can replace it. Apps Script answers
    with a redirect to the result, which httpx follows."""
    r = httpx.post(script_url(), json=payload, timeout=SCRIPT_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        raise SheetError("the Apps Script link didn't answer with JSON: is it deployed as a "
                         "web app with access for 'Anyone'?") from None


def _push_apps_script(tables) -> str:
    safe = {t: [[as_text(v) for v in row] for row in rows] for t, rows in tables.items()}
    try:
        out = _post_script({"secret": script_secret(), "tables": safe})
    except httpx.HTTPError as e:
        raise SheetError(f"couldn't reach the Apps Script link ({type(e).__name__})") from None
    if not isinstance(out, dict) or not out.get("ok"):
        err = out.get("error") if isinstance(out, dict) else None
        raise SheetError(f"Apps Script: {err or 'unexpected answer'}")
    return out.get("url") or ""


def _http():
    """An authorised requests session (cached). Separated out so tests can replace it."""
    global _session
    if _session is None:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(key_file(), scopes=SCOPES)
        _session = AuthorizedSession(creds)
    return _session


def _call(method, path, **kw):
    r = _http().request(method, f"{API}/{sheet_id()}{path}", timeout=TIMEOUT, **kw)
    if r.status_code >= 400:
        # Google's error text explains things like "caller does not have permission";
        # it never contains our key, which lives only in the signed token.
        try:
            detail = r.json().get("error", {}).get("message", "")
        except ValueError:
            detail = ""
        raise SheetError(f"Google Sheets {r.status_code}: {detail or r.reason}")
    return r.json() if r.content else {}


def _quote(tab):
    return "'" + tab.replace("'", "''") + "'"


def _push_api(tables) -> None:
    meta = _call("GET", "", params={"fields": "sheets.properties(sheetId,title)"})
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}

    missing = [t for t in tables if t not in existing]
    if missing:
        out = _call("POST", ":batchUpdate", json={"requests": [
            {"addSheet": {"properties": {"title": t, "gridProperties": {"frozenRowCount": 1}}}}
            for t in missing]})
        for reply in out.get("replies", []):
            props = reply.get("addSheet", {}).get("properties", {})
            existing[props.get("title")] = props.get("sheetId")
        bold = [{"repeatCell": {
            "range": {"sheetId": existing[t], "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold"}} for t in missing if existing.get(t) is not None]
        if bold:
            _call("POST", ":batchUpdate", json={"requests": bold})

    _call("POST", "/values:batchClear", json={"ranges": [_quote(t) for t in tables]})
    _call("POST", "/values:batchUpdate", json={
        "valueInputOption": "RAW",
        "data": [{"range": f"{_quote(t)}!A1", "values": rows} for t, rows in tables.items()],
    })
