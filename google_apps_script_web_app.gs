function doPost(e) {
  var props = PropertiesService.getScriptProperties();
  var apiToken = props.getProperty("API_TOKEN");
  var sheetId = props.getProperty("SHEET_ID");
  var sheetName = props.getProperty("SHEET_NAME") || "Sheet1";

  var body = JSON.parse((e && e.postData && e.postData.contents) || "{}");
  if (!apiToken || body.token !== apiToken) {
    return jsonResponse({ ok: false, error: "unauthorized" });
  }

  var rows = Array.isArray(body.rows) ? body.rows : [];
  var sheet = SpreadsheetApp.openById(sheetId).getSheetByName(sheetName);
  if (!sheet) {
    return jsonResponse({ ok: false, error: "sheet not found" });
  }

  rows.forEach(function (row) {
    sheet.appendRow([
      new Date(),
      row.user_name || "",
      row.video_code || "",
      row.question_text || "",
      row.score || "",
      body.submission_id || ""
    ]);
  });

  return jsonResponse({ ok: true, written: rows.length });
}

function jsonResponse(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
