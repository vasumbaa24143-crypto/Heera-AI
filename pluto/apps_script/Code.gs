/**
 * Pluto dashboard: receives your items from Pluto and writes them into this sheet.
 *
 * Setup (see Pluto's README, "Google Sheet dashboard"):
 *  1. In your Google Sheet: Extensions -> Apps Script. Replace everything with this file.
 *  2. Change SECRET below to a long random word (at least 16 characters) and save.
 *     Put the same word in Pluto's .env as GOOGLE_APPS_SCRIPT_SECRET.
 *  3. Deploy -> New deployment -> type "Web app".
 *     Execute as: Me.  Who has access: Anyone.  -> Deploy, and allow access when asked.
 *  4. Copy the Web app URL (ends in /exec) into .env as GOOGLE_APPS_SCRIPT_URL.
 *
 * "Anyone" only means the link works without a Google login; every request must carry
 * the SECRET, or it's refused. The script only ever touches Pluto's four tabs.
 */
const SECRET = 'CHANGE-ME-to-a-long-random-word';

const TABS = ['Dashboard', 'To-dos', 'Done', 'Notes & ideas'];
const MAX_ROWS = 20000;

function doPost(e) {
  try {
    if (SECRET.indexOf('CHANGE-ME') === 0 || SECRET.length < 16) {
      return reply_({ ok: false, error: 'set SECRET in the script first' });
    }
    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (body.secret !== SECRET) {
      return reply_({ ok: false, error: 'wrong secret' });
    }
    const tables = body.tables || {};
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    Object.keys(tables).forEach(function (name) {
      if (TABS.indexOf(name) === -1) return;            // only Pluto's own tabs
      const rows = tables[name];
      if (!Array.isArray(rows) || rows.length > MAX_ROWS) return;
      writeTab_(ss, name, rows);
    });
    return reply_({ ok: true, url: ss.getUrl() });
  } catch (err) {
    return reply_({ ok: false, error: String(err && err.message || err) });
  }
}

function writeTab_(ss, name, rows) {
  const sheet = ss.getSheetByName(name) || ss.insertSheet(name);
  sheet.clearContents();
  if (!rows.length) return;
  const width = rows.reduce(function (w, r) { return Math.max(w, Array.isArray(r) ? r.length : 0); }, 1);
  const values = rows.map(function (r) {
    const row = (Array.isArray(r) ? r : []).map(asText_);
    while (row.length < width) row.push('');
    return row;
  });
  sheet.getRange(1, 1, values.length, width).setValues(values);
  sheet.setFrozenRows(1);
  sheet.getRange(1, 1, 1, width).setFontWeight('bold');
}

// setValues() turns "=..." into a formula; a leading apostrophe keeps it as plain text.
function asText_(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'number' || typeof v === 'boolean') return v;
  const s = String(v);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
