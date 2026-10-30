// The review pack's INSTRUCTIONS.txt must describe the review process that is actually supported: review.html -> "Export JSON" -> apply_verification.mjs.
// There is NO importer for review_sheet.csv, so the instructions must not offer it as a way to submit answers. The pack is generated from a synthetic mirror
// (tests/helpers/syntheticMirror.mjs); the real pack is never generated or read.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { makeMirror, assertIsolated, buildPack, REAL_FUEL } from "./helpers/syntheticMirror.mjs";

const m = makeMirror();
assertIsolated(m.root);
const gen = buildPack(m);
const instructions = fs.existsSync(path.join(m.packDir, "INSTRUCTIONS.txt")) ? fs.readFileSync(path.join(m.packDir, "INSTRUCTIONS.txt"), "utf8") : "";
const lines = instructions.split(/\r?\n/);

test("pack generation for the instruction tests ran in the synthetic mirror", () => {
  assert.equal(gen.status, 0, gen.all);
  assert.ok(path.resolve(m.packDir).startsWith(path.resolve(m.root) + path.sep));
  assert.ok(instructions.length > 200);
});

test("the instructions do not promise a CSV / spreadsheet submission workflow", () => {
  assert.doesNotMatch(instructions, /prefer a spreadsheet/i);
  assert.doesNotMatch(instructions, /fill\s+review_sheet\.csv/i);
  assert.doesNotMatch(instructions, /(send|submit|return|e-?mail|upload)\b[^.\n]*(\.csv|spreadsheet|review_sheet)/i);
  assert.doesNotMatch(instructions, /(\.csv|spreadsheet|review_sheet)[^.\n]*\b(instead|alternatively|also accepted|is accepted|can be used to submit)/i);
  // every line that mentions the sheet must say, in so many words, that it is NOT a submission route
  const mentions = lines.filter((l) => /csv|spreadsheet|review_sheet/i.test(l));
  assert.ok(mentions.length >= 1, "the sheet is still generated, so the instructions should say what it is");
  for (const l of mentions) assert.match(l, /\bNO importer\b[\s\S]*\bNOT\b|\bNOT\b[\s\S]*\bonly the JSON\b/i, `must state it is not accepted: ${l}`);
});

test("the instructions describe the supported process: review.html, Export JSON, and applying both reviewers' files together", () => {
  assert.match(instructions, /review\.html/);
  assert.match(instructions, /Export JSON/);
  assert.match(instructions, /only the JSON file exported by review\.html is accepted/i);
  assert.match(instructions, /applied together in ONE run/i, "independent agreement exists only when both exports are applied in the same invocation (a later separate review is only logged)");
  assert.match(instructions, /NOT marked verified/);
});

test("claim guard: no script or module other than the pack generator reads review_sheet.csv or parses CSV (if an importer is ever added, the instructions and this test must be revisited)", () => {
  for (const dir of ["scripts", "src"]) {
    for (const f of fs.readdirSync(path.join(REAL_FUEL, dir)).filter((x) => x.endsWith(".mjs"))) {
      if (dir === "scripts" && f === "build_review_pack.mjs") continue;
      assert.doesNotMatch(fs.readFileSync(path.join(REAL_FUEL, dir, f), "utf8"), /review_sheet|\.csv\b|csv-parse/i, `${dir}/${f} references the CSV sheet`);
    }
  }
});

test("the sheet is still generated as a worksheet and the README describes it as non-submittable", () => {
  assert.ok(fs.existsSync(path.join(m.packDir, "review_sheet.csv")));
  const readme = fs.readFileSync(path.join(REAL_FUEL, "verification", "README.md"), "utf8");
  assert.match(readme, /review_sheet\.csv[^\n]*only a paper worksheet[^\n]*NO importer/i);
  assert.doesNotMatch(readme, /Prefer a spreadsheet/i);
});
