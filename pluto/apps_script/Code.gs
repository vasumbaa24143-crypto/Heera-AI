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
 * the SECRET, or it's refused. The script only ever touches Pluto's five tabs.
 *
 * Updating to a newer copy of this script: paste it over the old one (keep your SECRET),
 * save, then Deploy -> Manage deployments -> edit (pencil) -> Version: New version -> Deploy.
 * The /exec link stays the same.
 */
const SECRET = 'CHANGE-ME-to-a-long-random-word';

const TABS = ['Dashboard', 'To-dos', 'Done', 'Notes & ideas', 'Error log'];
const MAX_ROWS = 20000;
const MAX_COLS = 30;

// How each kind of row looks. Pluto sends a role per row (core.SHEET_ROLES); unknown roles
// are ignored. Only colours and fonts come from here, never anything Pluto sends.
const ROLES = {
  title:     { size: 18, bold: true, color: '#111827', height: 42, overflow: true },
  subtitle:  { size: 9, color: '#6b7280', overflow: true },
  lead:      { size: 12, bold: true, color: '#1e3a8a', bg: '#eef2ff', height: 34, overflow: true },
  blank:     { height: 12 },
  kpi_label: { size: 9, bold: true, color: '#6b7280', bg: '#f8fafc', align: 'center', height: 26 },
  kpi_value: { size: 22, bold: true, color: '#111827', bg: '#f8fafc', align: 'center', height: 44 },
  section:   { size: 12, bold: true, color: '#312e81', height: 32, overflow: true, line: '#c7d2fe' },
  header:    { bold: true, color: '#374151', bg: '#e5e7eb', height: 26, line: '#9ca3af' },
  row:       {},
  alert:     { color: '#991b1b', bg: '#fdecea' },
  today:     { color: '#1e3a8a', bg: '#e8f1ff' },
  urgent:    { color: '#9a3412', bg: '#fff4e5' },
  done:      { color: '#6b7280' },
  group:     { bold: true, color: '#5b21b6', bg: '#f3e8ff', height: 28, overflow: true },
  note:      { italic: true, size: 9, color: '#6b7280', overflow: true },
};

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
    const styles = (body.styles && typeof body.styles === 'object') ? body.styles : {};
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    Object.keys(tables).forEach(function (name) {
      if (TABS.indexOf(name) === -1) return;            // only Pluto's own tabs
      const rows = tables[name];
      if (!Array.isArray(rows) || rows.length > MAX_ROWS) return;
      writeTab_(ss, name, rows, styles[name]);
    });
    orderTabs_(ss);
    return reply_({ ok: true, url: ss.getUrl() });
  } catch (err) {
    return reply_({ ok: false, error: String(err && err.message || err) });
  }
}

function writeTab_(ss, name, rows, style) {
  const sheet = ss.getSheetByName(name) || ss.insertSheet(name);
  if (sheet.getFilter()) sheet.getFilter().remove();
  sheet.clear();                                          // values and old formatting
  if (!rows.length) return;
  const width = Math.min(MAX_COLS,
    rows.reduce(function (w, r) { return Math.max(w, Array.isArray(r) ? r.length : 0); }, 1));
  const values = rows.map(function (r) {
    const row = (Array.isArray(r) ? r : []).slice(0, width).map(asText_);
    while (row.length < width) row.push('');
    return row;
  });
  const range = sheet.getRange(1, 1, values.length, width);
  range.setValues(values);
  range.setVerticalAlignment('middle');
  if (!style || !Array.isArray(style.roles)) {            // plain layout (older Pluto)
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, 1, width).setFontWeight('bold');
    return;
  }
  styleTab_(sheet, range, values.length, width, style);
}

function styleTab_(sheet, range, height, width, style) {
  const W = SpreadsheetApp.WrapStrategy;
  const wrap = numbers_(style.wrap);   // other columns clip, so long text stays on one line
  const bg = [], color = [], weight = [], size = [], italic = [], align = [], strategy = [];
  const heights = [], lines = [];
  for (let i = 0; i < height; i++) {
    const role = String(style.roles[i] || 'row');
    const R = ROLES[role] || ROLES.row;
    bg.push(fill_(width, R.bg || '#ffffff'));
    color.push(fill_(width, R.color || '#1f2937'));
    weight.push(fill_(width, R.bold ? 'bold' : 'normal'));
    size.push(fill_(width, R.size || 10));
    italic.push(fill_(width, R.italic ? 'italic' : 'normal'));
    align.push(fill_(width, R.align || 'left'));
    const s = [];
    for (let c = 1; c <= width; c++) {
      s.push(R.overflow ? W.OVERFLOW : wrap.indexOf(c) !== -1 ? W.WRAP : W.CLIP);
    }
    strategy.push(s);
    if (R.height) heights.push([i + 1, R.height]);
    if (R.line) lines.push([i + 1, R.line]);
  }
  range.setBackgrounds(bg).setFontColors(color).setFontWeights(weight).setFontSizes(size)
    .setFontStyles(italic).setHorizontalAlignments(align).setWrapStrategies(strategy);
  numbers_(style.widths).forEach(function (w, c) {
    if (c < width) sheet.setColumnWidth(c + 1, Math.max(30, Math.min(800, w)));
  });
  sheet.autoResizeRows(1, height);
  heights.forEach(function (h) { sheet.setRowHeight(h[0], h[1]); });
  lines.forEach(function (l) {
    sheet.getRange(l[0], 1, 1, width)
      .setBorder(null, null, true, null, null, null, l[1], SpreadsheetApp.BorderStyle.SOLID);
  });
  sheet.setFrozenRows(Math.max(0, Math.min(5, Number(style.freeze) || 0)));
  sheet.setHiddenGridlines(style.gridlines === false);
  if (typeof style.tab_color === 'string' && /^#[0-9a-fA-F]{6}$/.test(style.tab_color)) {
    sheet.setTabColor(style.tab_color);
  }
  if (style.filter === true && height > 1) sheet.getRange(1, 1, height, width).createFilter();
}

// Keep Pluto's tabs first, in order (Dashboard on the left). Other tabs are left alone.
function orderTabs_(ss) {
  TABS.forEach(function (name, i) {
    const sheet = ss.getSheetByName(name);
    if (!sheet || sheet.getIndex() === i + 1) return;
    ss.setActiveSheet(sheet);
    ss.moveActiveSheet(i + 1);
  });
}

function fill_(n, v) {
  const out = [];
  for (let i = 0; i < n; i++) out.push(v);
  return out;
}

function numbers_(list) {
  return Array.isArray(list) ? list.map(Number).filter(function (n) { return isFinite(n); }) : [];
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
