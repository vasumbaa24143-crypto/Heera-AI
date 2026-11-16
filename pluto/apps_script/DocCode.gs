/**
 * Pluto notebook: receives your items from Pluto and rewrites this Google Doc.
 * Use a doc made just for this: everything in it is replaced on each update.
 *
 * Setup (see Pluto's README, "Google Doc notebook"):
 *  1. In your Google Doc: Extensions -> Apps Script. Replace everything with this file.
 *  2. Change SECRET below to a long random word (at least 16 characters) and save.
 *     Put the same word in Pluto's .env as GOOGLE_DOC_APPS_SCRIPT_SECRET.
 *  3. Deploy -> New deployment -> type "Web app".
 *     Execute as: Me.  Who has access: Anyone.  -> Deploy, and allow access when asked.
 *  4. Copy the Web app URL (ends in /exec) into .env as GOOGLE_DOC_APPS_SCRIPT_URL.
 *
 * "Anyone" only means the link works without a Google login; every request must carry
 * the SECRET, or it's refused.
 */
const SECRET = 'CHANGE-ME-to-a-long-random-word';

const MAX_BLOCKS = 5000;

function doPost(e) {
  try {
    if (SECRET.indexOf('CHANGE-ME') === 0 || SECRET.length < 16) {
      return reply_({ ok: false, error: 'set SECRET in the script first' });
    }
    const data = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (data.secret !== SECRET) {
      return reply_({ ok: false, error: 'wrong secret' });
    }
    const blocks = data.blocks;
    if (!Array.isArray(blocks) || blocks.length > MAX_BLOCKS) {
      return reply_({ ok: false, error: 'blocks must be a list' });
    }
    const doc = DocumentApp.getActiveDocument();
    const body = doc.getBody();
    body.clear();
    blocks.forEach(function (b) {
      const text = String((b && b.text) || '');
      const style = b && b.style;
      if (style === 'bullet') {
        body.appendListItem(text).setGlyphType(DocumentApp.GlyphType.BULLET);
      } else if (style === 'title') {
        body.appendParagraph(text).setHeading(DocumentApp.ParagraphHeading.TITLE);
      } else if (style === 'heading') {
        body.appendParagraph(text).setHeading(DocumentApp.ParagraphHeading.HEADING1);
      } else {
        body.appendParagraph(text).setHeading(DocumentApp.ParagraphHeading.NORMAL);
      }
    });
    // body.clear() leaves one empty paragraph at the top; remove it if there's other content.
    if (body.getNumChildren() > 1 && body.getChild(0).asText().getText() === '') {
      body.removeChild(body.getChild(0));
    }
    return reply_({ ok: true, url: doc.getUrl() });
  } catch (err) {
    return reply_({ ok: false, error: String(err && err.message || err) });
  }
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
