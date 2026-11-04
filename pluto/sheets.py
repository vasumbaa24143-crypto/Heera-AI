"""Keep a Google Sheet dashboard of the user's items (one-way: Pluto -> Sheet).

Setup: a Google Cloud service account with the Sheets API enabled, its JSON key file on
this computer (never in git), and a sheet shared with the service account's email as
Editor. Then in .env:

    GOOGLE_SHEETS_ID=<the sheet's id, or its full URL>
    GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json

Uses google-auth + requests, which the Gemini SDK already installs. Every cell is
written with valueInputOption=RAW, so text from chat can never become a formula.
"""
import json
import logging
import os
import re

log = logging.getLogger("pluto")

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TIMEOUT = 30
_session = None


class SheetError(RuntimeError):
    pass


def sheet_id() -> str:
    """GOOGLE_SHEETS_ID may be the bare id or the sheet's URL."""
    raw = (os.getenv("GOOGLE_SHEETS_ID") or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", raw)
    return m.group(1) if m else raw


def key_file() -> str:
    return os.path.expanduser((os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip())


def enabled() -> bool:
    return bool(sheet_id() and key_file())


def url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id()}/edit" if sheet_id() else ""


def problem() -> str:
    """Why syncing can't work, in plain words ("" when the setup looks fine)."""
    if not sheet_id() and not key_file():
        return "not set up"
    if not sheet_id():
        return "GOOGLE_SHEETS_ID is missing in .env"
    if not key_file():
        return "GOOGLE_SERVICE_ACCOUNT_FILE is missing in .env"
    if not os.path.isfile(key_file()):
        return "the service account key file named in GOOGLE_SERVICE_ACCOUNT_FILE doesn't exist"
    return ""


def service_account_email() -> str:
    try:
        with open(key_file(), encoding="utf-8") as f:
            return json.load(f).get("client_email", "")
    except (OSError, ValueError):
        return ""


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


def push(tables: dict) -> None:
    """Write {tab: rows} to the sheet: create missing tabs, then replace their contents."""
    if problem():
        raise SheetError(problem())
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
