// Wraps the marked-generated HTML body in a print-ready A4 template.
// Segoe UI carries the Arabic glyphs; Chromium's bidi engine handles RTL runs inline.
const fs = require('fs');
const path = require('path');

const dir = __dirname;
const body = fs.readFileSync(path.join(dir, 'blueprint-body.html'), 'utf8');

const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Kooora.com — Site Architecture Blueprint</title>
<style>
  @page { size: A4; margin: 18mm 15mm 20mm 15mm; }
  * { box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', Tahoma, 'Arial Unicode MS', sans-serif;
    font-size: 10.5pt; line-height: 1.5; color: #1a1a2e; margin: 0;
  }
  h1 { font-size: 20pt; color: #0d3b66; border-bottom: 3px solid #0d3b66;
       padding-bottom: 6px; margin-top: 28px; page-break-before: always; }
  h1:first-of-type { page-break-before: avoid; margin-top: 0; }
  h2 { font-size: 14.5pt; color: #14507a; margin-top: 22px; page-break-after: avoid; }
  h3 { font-size: 12pt; color: #1d6a96; margin-top: 16px; page-break-after: avoid; }
  h4 { font-size: 11pt; color: #333; page-break-after: avoid; }
  table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 8.5pt;
          page-break-inside: auto; }
  th { background: #0d3b66; color: #fff; padding: 5px 7px; text-align: left; }
  td { border: 1px solid #c5d3e0; padding: 4px 7px; vertical-align: top;
       overflow-wrap: break-word; word-break: break-word; }
  tr { page-break-inside: avoid; }
  tr:nth-child(even) td { background: #f2f6fa; }
  code { font-family: Consolas, 'Segoe UI', monospace; font-size: 8.5pt;
         background: #eef2f6; padding: 1px 4px; border-radius: 3px;
         overflow-wrap: break-word; }
  pre { background: #f6f8fa; border: 1px solid #d8e0e8; border-radius: 5px;
        padding: 10px; font-size: 8pt; line-height: 1.35;
        white-space: pre-wrap; overflow-wrap: break-word; }
  pre code { background: none; padding: 0; font-size: inherit; }
  blockquote { border-left: 4px solid #0d3b66; margin-left: 0; padding: 2px 14px;
               color: #444; background: #f6f8fa; }
  ul, ol { padding-left: 22px; }
  li { margin: 2px 0; }
  a { color: #14507a; text-decoration: none; }
  hr { border: none; border-top: 1px solid #c5d3e0; margin: 18px 0; }
</style>
</head>
<body>
${body}
</body>
</html>`;

fs.writeFileSync(path.join(dir, 'KOOORA-SITE-BLUEPRINT.html'), html, 'utf8');
console.log('Wrapped HTML written:', fs.statSync(path.join(dir, 'KOOORA-SITE-BLUEPRINT.html')).size, 'bytes');
