// Inspects raw pdfjs-dist text items for one table page to understand
// EXACTLY why the linear-join heuristic merges rows PyMuPDF keeps separate.
import { readFile } from "node:fs/promises";
import { getDocument } from "pdfjs-dist/legacy/build/pdf.mjs";

const PDF = process.argv[2] || "fixtures/reference-pdfs/reahul-report-ER40513.pdf";
const PAGE = parseInt(process.argv[3] || "2", 10);

const data = await readFile(PDF);
const doc = await getDocument({ data: new Uint8Array(data), useSystemFonts: true }).promise;
const page = await doc.getPage(PAGE);
const content = await page.getTextContent();

console.log(`Page ${PAGE}: ${content.items.length} raw text items\n`);
for (const it of content.items.slice(0, 60)) {
  const x = it.transform[4].toFixed(1);
  const y = it.transform[5].toFixed(1);
  console.log(`y=${y.padStart(8)} x=${x.padStart(7)} hasEOL=${String(it.hasEOL).padEnd(5)} str=${JSON.stringify(it.str)}`);
}
await doc.destroy();
