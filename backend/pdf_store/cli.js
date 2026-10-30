#!/usr/bin/env node
'use strict';

const path = require('node:path');
const store = require('./pdf-store');

const HELP = `Spinco PDF store (Node.js) - sorts and de-duplicates uploaded PDFs with the software's own rules.

  node cli.js ingest <sourceFolder> [--out <archiveFolder>] [--kind auto|expense|trip] [--dry-run] [--json]
  node cli.js list   [--out <archiveFolder>] [--kind expense|trip|unsorted]
  node cli.js verify [--out <archiveFolder>]

Default archive folder: backend/pdf_archive. The source folder is only READ; archived files are byte-for-byte copies.
Pointing <sourceFolder> at the storage root (it contains expense_reports/ and trip_reports/) keeps the upload kind.`;

function parse(argv) {
  const opts = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dry-run') opts.dryRun = true;
    else if (a === '--json') opts.json = true;
    else if (a === '--out' || a === '--kind') opts[a.slice(2)] = argv[++i];
    else opts._.push(a);
  }
  return opts;
}

function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const o = parse(rest);
  const outDir = o.out || path.join(__dirname, '..', 'pdf_archive');
  if (cmd === 'ingest' && o._[0]) {
    const r = store.ingest(o._[0], { outDir, kind: o.kind, dryRun: o.dryRun });
    if (o.json) return console.log(JSON.stringify(r, null, 2));
    for (const x of r.results) {
      const detail = x.outcome === 'stored' ? `${x.kind} (${x.via}) -> ${x.stored}` : x.outcome === 'duplicate' ? `duplicate of ${x.duplicateOf}` : x.outcome === 'unchanged' ? `already archived as ${x.stored}` : x.outcome === 'rejected' ? x.reason : `${x.from} -> ${x.kind}`;
      console.log(`${x.outcome.padEnd(12)} ${path.basename(x.file)}  ${detail}`);
    }
    console.log(`\n${r.dryRun ? '[dry run - nothing written] ' : ''}stored ${r.summary.stored}, duplicates ${r.summary.duplicates}, already archived ${r.summary.unchanged}, reclassified ${r.summary.reclassified}, rejected ${r.summary.rejected}`);
    console.log(`archive: ${r.outDir}`);
  } else if (cmd === 'list') {
    for (const d of store.list(outDir, { kind: o.kind })) {
      console.log(`${d.archivedAt}  ${d.kind.padEnd(14)} ${(d.documentNumber || '-').padEnd(12)} ${String(d.pageCount).padStart(3)}p  ${d.storedPath}  (+${d.duplicates.length} duplicate)`);
    }
  } else if (cmd === 'verify') {
    const r = store.verify(outDir);
    console.log(`checked ${r.checked}, problems ${r.problems.length}`);
    for (const p of r.problems) console.log(`  ${p.problem}: ${p.storedPath}`);
    process.exitCode = r.problems.length ? 1 : 0;
  } else {
    console.log(HELP);
    process.exitCode = cmd ? 2 : 0;
  }
}

try {
  main();
} catch (err) {
  console.error(`error: ${err.message}`);
  process.exitCode = 1;
}
