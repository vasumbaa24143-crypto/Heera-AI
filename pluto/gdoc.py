"""Keep a Google Doc "notebook" of the user's items up to date (one-way: Pluto -> Doc).

The whole doc body is rewritten on each update, so use a doc made just for this.
Two ways to connect (see the README, "Google Doc notebook"):

1. Apps Script link (easiest): paste apps_script/DocCode.gs into the doc's
   Extensions -> Apps Script, deploy it as a web app, and set in .env:
       GOOGLE_DOC_APPS_SCRIPT_URL=https://script.google.com/macros/s/.../exec
       GOOGLE_DOC_APPS_SCRIPT_SECRET=<the same long random word as in the script>

2. Service account (the same key file as the Sheet dashboard), with the Google Docs API
   enabled and the doc shared with the service account's email as Editor:
       GOOGLE_DOC_ID=<the doc's id, or its full URL>
       GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json

If both are set, the service account is used.

Content is a list of blocks: {"style": "title" | "heading" | "text" | "bullet", "text": str}.
"""
import json
import os
import re

import httpx

API = "https://docs.googleapis.com/v1/documents"
SCOPES = ["https://www.googleapis.com/auth/documents"]
TIMEOUT = 30
SCRIPT_TIMEOUT = 90
MIN_SECRET = 16
STYLES = ("title", "heading", "text", "bullet")
_session = None


class DocError(RuntimeError):
    pass


# ---------- configuration ----------

def doc_id() -> str:
    """GOOGLE_DOC_ID may be the bare id or the doc's URL."""
    raw = (os.getenv("GOOGLE_DOC_ID") or "").strip()
    m = re.search(r"/document/d/([A-Za-z0-9_-]+)", raw)
    return m.group(1) if m else raw


def key_file() -> str:
    return os.path.expanduser((os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip())


def script_url() -> str:
    return (os.getenv("GOOGLE_DOC_APPS_SCRIPT_URL") or "").strip()


def script_secret() -> str:
    return (os.getenv("GOOGLE_DOC_APPS_SCRIPT_SECRET") or "").strip()


def mode() -> str:
    """"service_account", "apps_script", or "" (not set up)."""
    if doc_id():
        return "service_account"
    if script_url():
        return "apps_script"
    return ""


def url() -> str:
    return f"https://docs.google.com/document/d/{doc_id()}/edit" if doc_id() else ""


def problem() -> str:
    """Why syncing can't work, in plain words ("" when the setup looks fine)."""
    m = mode()
    if not m:
        return "not set up"
    if m == "apps_script":
        if not re.match(r"^https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec$", script_url()):
            return "GOOGLE_DOC_APPS_SCRIPT_URL should be the web app link ending in /exec"
        if len(script_secret()) < MIN_SECRET:
            return f"GOOGLE_DOC_APPS_SCRIPT_SECRET must be set (at least {MIN_SECRET} characters)"
        return ""
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

def push(blocks: list) -> str:
    """Replace the doc's contents with `blocks`. Returns the doc's URL ("" if unknown)."""
    if problem():
        raise DocError(problem())
    if mode() == "apps_script":
        return _push_apps_script(blocks)
    _push_api(blocks)
    return url()


def _post_script(payload):
    """One POST to the web app; separated out so tests can replace it."""
    r = httpx.post(script_url(), json=payload, timeout=SCRIPT_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        raise DocError("the Apps Script link didn't answer with JSON: is it deployed as a "
                       "web app with access for 'Anyone'?") from None


def _push_apps_script(blocks) -> str:
    try:
        out = _post_script({"secret": script_secret(), "blocks": blocks})
    except httpx.HTTPError as e:
        raise DocError(f"couldn't reach the Apps Script link ({type(e).__name__})") from None
    if not isinstance(out, dict) or not out.get("ok"):
        err = out.get("error") if isinstance(out, dict) else None
        raise DocError(f"Apps Script: {err or 'unexpected answer'}")
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
    r = _http().request(method, f"{API}/{doc_id()}{path}", timeout=TIMEOUT, **kw)
    if r.status_code >= 400:
        try:
            detail = r.json().get("error", {}).get("message", "")
        except ValueError:
            detail = ""
        raise DocError(f"Google Docs {r.status_code}: {detail or r.reason}")
    return r.json() if r.content else {}


def _units(s: str) -> int:
    """Length as Google Docs counts it (UTF-16 code units; an emoji is 2)."""
    return len(s.encode("utf-16-le")) // 2


_NAMED = {"title": "TITLE", "heading": "HEADING_1"}


def build_requests(blocks, end_index) -> list:
    """Docs API requests that replace a body ending at `end_index` with `blocks`."""
    lines, styled, pos = [], [], 1
    for b in blocks:
        text = " ".join(str(b.get("text", "")).split())      # one paragraph per block
        if b.get("style") == "bullet":
            text = "• " + text
        line = text + "\n"
        if b.get("style") in _NAMED:
            styled.append((pos, pos + _units(line), _NAMED[b["style"]]))
        lines.append(line)
        pos += _units(line)
    text = "".join(lines)
    reqs = []
    if end_index > 2:  # the body's final newline can't be deleted
        reqs.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index - 1}}})
    if not text:
        return reqs
    reqs.append({"insertText": {"location": {"index": 1}, "text": text}})

    def style(start, end, named):
        return {"updateParagraphStyle": {"range": {"startIndex": start, "endIndex": end},
                                         "paragraphStyle": {"namedStyleType": named},
                                         "fields": "namedStyleType"}}

    reqs.append(style(1, pos, "NORMAL_TEXT"))  # whatever style the old first paragraph had
    reqs += [style(s, e, named) for s, e, named in styled]
    return reqs


def _push_api(blocks) -> None:
    meta = _call("GET", "", params={"fields": "body.content(endIndex)"})
    content = meta.get("body", {}).get("content", [])
    end = content[-1].get("endIndex", 2) if content else 2
    reqs = build_requests(blocks, end)
    if reqs:
        _call("POST", ":batchUpdate", json={"requests": reqs})
