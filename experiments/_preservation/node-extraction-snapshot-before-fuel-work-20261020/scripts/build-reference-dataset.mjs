// Builds datasets/manifest.json: SHA-256 hash, page count, filename, and
// dataset role for each reference PDF. Split by report (not by row) to
// avoid leakage between rows of the same document - dev PDFs are used to
// design/tune the extraction approach, held-out PDFs are touched only for
// the final evaluation after the approach is frozen.
import { createHash } from "node:crypto";
import { readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { getDocument } from "pdfjs-dist/legacy/build/pdf.mjs";

const FIXTURES_DIR = path.resolve("fixtures/reference-pdfs");

// Dev set: used to diagnose the row-merging bug and design the fix.
// Held-out: never inspected while designing the fix - evaluated once, at
// the end, against the frozen approach.
const DEV_SET = new Set(["reahul-report-ER40513.pdf", "ER-40706.pdf", "ER-39832.pdf"]);
const HELDOUT_SET = new Set(["ER-40223.pdf", "ER-40782.pdf", "vivek-bhai-report-ER41220.pdf"]);

const KNOWN_BUT_UNAVAILABLE = [
  {
    name: "dipak report.pdf",
    note: "Named in the task's reference-PDF list but not found anywhere on disk (root, uat-data-test, or any other project directory) - genuinely unavailable, not substituted or fabricated.",
  },
];

async function main() {
  const files = (await readdir(FIXTURES_DIR)).filter((f) => f.endsWith(".pdf"));
  const manifest = { generatedAt: new Date().toISOString(), reportsCovered: [], knownButUnavailable: KNOWN_BUT_UNAVAILABLE };

  for (const file of files) {
    const fullPath = path.join(FIXTURES_DIR, file);
    const data = await readFile(fullPath);
    const sha256 = createHash("sha256").update(data).digest("hex");
    const doc = await getDocument({ data: new Uint8Array(data), useSystemFonts: true }).promise;
    const pageCount = doc.numPages;
    await doc.destroy();

    const role = DEV_SET.has(file) ? "dev" : HELDOUT_SET.has(file) ? "heldout" : "unassigned";
    manifest.reportsCovered.push({ filename: file, sha256, pageCount, bytes: data.length, role });
    console.log(`${file}: ${pageCount} pages, sha256=${sha256.slice(0, 12)}..., role=${role}`);
  }

  await writeFile(path.resolve("datasets/manifest.json"), JSON.stringify(manifest, null, 2));
  console.log(`\nWrote datasets/manifest.json (${manifest.reportsCovered.length} reports, ${manifest.knownButUnavailable.length} unavailable)`);
}

main();
