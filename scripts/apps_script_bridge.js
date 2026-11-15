/**
 * JAI Google Apps Script Webhook Bridge
 * 
 * Free serverless cloud execution for JAI.
 * Runs in Google Cloud with 0 local RAM or CPU overhead.
 * 
 * How to Deploy (1-Minute Setup):
 * 1. Open https://script.google.com and click "New Project".
 * 2. Paste this entire file into Code.gs.
 * 3. Set a secret token in Project Settings -> Script Properties:
 *    Key: JAI_WEBHOOK_SECRET, Value: (your chosen password/token)
 * 4. Click "Deploy" -> "New deployment" -> Select type "Web app".
 *    - Execute as: "Me"
 *    - Who has access: "Anyone"
 * 5. Copy the Web App URL and set in JAI's .env:
 *    APPS_SCRIPT_WEBHOOK_URL="https://script.google.com/macros/s/..."
 *    APPS_SCRIPT_SECRET="(your chosen secret)"
 */

function doGet(e) {
  return ContentService.createTextOutput(JSON.stringify({
    status: "ok",
    service: "JAI Google Apps Script Bridge",
    time: new Date().toISOString()
  })).setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  try {
    var raw = e.postData ? e.postData.contents : "{}";
    var data = JSON.parse(raw);
    
    // Optional secret verification
    var scriptProps = PropertiesService.getScriptProperties();
    var expectedSecret = scriptProps.getProperty("JAI_WEBHOOK_SECRET");
    if (expectedSecret && data.secret !== expectedSecret) {
      return ContentService.createTextOutput(JSON.stringify({
        status: "error",
        error: "Unauthorized: Invalid or missing secret token."
      })).setMimeType(ContentService.MimeType.JSON);
    }

    var action = data.action || "ping";

    if (action === "ping") {
      return ContentService.createTextOutput(JSON.stringify({
        status: "success",
        message: "Pong from Google Apps Script cloud!"
      })).setMimeType(ContentService.MimeType.JSON);
    }

    // Action 1: Create Styled Google Doc
    if (action === "create_doc") {
      var title = data.title || "Untitled JAI Document";
      var doc = DocumentApp.create(title);
      var body = doc.getBody();

      var blocks = data.blocks || [];
      if (blocks.length === 0 && data.text) {
        body.appendParagraph(data.text);
      }

      for (var i = 0; i < blocks.length; i++) {
        var b = blocks[i];
        var type = (b.type || "paragraph").toLowerCase();
        var content = b.content || "";

        if (type === "title") {
          var p = body.appendParagraph(content);
          p.setHeading(DocumentApp.ParagraphHeading.TITLE);
        } else if (type === "h1" || type === "heading1") {
          var p = body.appendParagraph(content);
          p.setHeading(DocumentApp.ParagraphHeading.HEADING1);
        } else if (type === "h2" || type === "heading2") {
          var p = body.appendParagraph(content);
          p.setHeading(DocumentApp.ParagraphHeading.HEADING2);
        } else if (type === "bullet") {
          body.appendListItem(content);
        } else if (type === "code") {
          var p = body.appendParagraph(content);
          p.setFontFamily("Consolas");
          p.setFontSize(10);
        } else {
          body.appendParagraph(content);
        }
      }

      doc.saveAndClose();

      return ContentService.createTextOutput(JSON.stringify({
        status: "success",
        doc_id: doc.getId(),
        url: doc.getUrl(),
        title: title
      })).setMimeType(ContentService.MimeType.JSON);
    }

    // Action 2: Append rows to Google Sheet
    if (action === "append_sheet") {
      var sheetName = data.sheet_name || "JAI_Data";
      var rows = data.rows || [];
      var spreadsheetId = data.spreadsheet_id;
      var ss;

      if (spreadsheetId) {
        ss = SpreadsheetApp.openById(spreadsheetId);
      } else {
        var files = DriveApp.getFilesByName(sheetName);
        if (files.hasNext()) {
          ss = SpreadsheetApp.open(files.next());
        } else {
          ss = SpreadsheetApp.create(sheetName);
        }
      }

      var sheet = ss.getActiveSheet();
      for (var r = 0; r < rows.length; r++) {
        sheet.appendRow(rows[r]);
      }

      return ContentService.createTextOutput(JSON.stringify({
        status: "success",
        spreadsheet_id: ss.getId(),
        url: ss.getUrl(),
        rows_added: rows.length
      })).setMimeType(ContentService.MimeType.JSON);
    }

    return ContentService.createTextOutput(JSON.stringify({
      status: "error",
      error: "Unknown action: " + action
    })).setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({
      status: "error",
      error: err.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  }
}
