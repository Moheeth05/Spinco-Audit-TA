// Registers ONE new authentic receipt image, with enforced provenance, as PENDING a human label. Usage:
//   node scripts/register_intake.mjs <image.png|jpg> --provenance <provenance.json> --split development|heldout_untouched [--fuel-dir <dir>]
// What it enforces:
//   * provenance must name the source, the owner, the permission basis and the permitted usage (store / modify / evaluate / redistribute),
//     and the person who authorised use; a receipt that may not even be stored/evaluated is refused;
//   * only authentic issued receipts (not templates/generator output, not synthetic) are accepted here;
//   * exact duplicates (SHA-256) of anything already in the corpus or the intake register are refused;
//   * the layout FAMILY id is fixed at intake and a family goes wholly to ONE side: if the family is already registered under the other
//     split the registration is refused (no near-identical template may sit in both development and held-out);
//   * the receipt gets NO label here. It stays `pending_human_label` and is not scored until scripts/apply_verification.mjs verifies it.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const argv = process.argv.slice(2);
const pos = [], opt = {};
for (let i = 0; i < argv.length; i++) argv[i].startsWith("--") ? (opt[argv[i].slice(2)] = argv[++i]) : pos.push(argv[i]);
const FUEL = path.resolve(opt["fuel-dir"] || path.join(path.dirname(fileURLToPath(import.meta.url)), ".."));
const fail = (m) => { console.error("REFUSED: " + m); process.exit(1); };
const [imagePath] = pos;
if (!imagePath || !opt.provenance || !opt.split) fail("usage: register_intake.mjs <image> --provenance <json> --split development|heldout_untouched");
if (!["development", "heldout_untouched"].includes(opt.split)) fail("--split must be development or heldout_untouched");
if (!fs.existsSync(imagePath)) fail(`no such image: ${imagePath}`);

const prov = JSON.parse(fs.readFileSync(opt.provenance, "utf8"));
const req = ["source_description", "source_type", "owner", "permission_basis", "authorized_by", "retrieval_date", "brand_family", "receipt_family", "personal_data_handling"];
for (const k of req) if (!prov[k] || !String(prov[k]).trim()) fail(`provenance.${k} is required`);
if (prov.source_type !== "authentic_issued_receipt") fail(`source_type must be 'authentic_issued_receipt' (got '${prov.source_type}'): generator output, templates and synthetic receipts are not accepted into the authentic corpus`);
const u = prov.permitted_usage || {};
for (const k of ["store", "evaluate"]) if (u[k] !== true) fail(`permitted_usage.${k} must be true (a receipt that may not be stored/evaluated cannot be used)`);
for (const k of ["modify", "redistribute"]) if (typeof u[k] !== "boolean") fail(`permitted_usage.${k} must be stated true or false`);
const BRANDS = ["Nayara", "Jio-bp", "Shell", "IndianOil", "BPCL", "HPCL", "independent", "other"];
if (!BRANDS.includes(prov.brand_family)) fail(`brand_family must be one of ${BRANDS.join(", ")}`);
if (prov.contains_confidential_company_data === true && u.redistribute === true) fail("a receipt with confidential company data cannot be marked redistributable");

const bytes = fs.readFileSync(imagePath);
const sha = crypto.createHash("sha256").update(bytes).digest("hex");
const regFile = path.join(FUEL, "intake", "intake_register.json");
const reg = fs.existsSync(regFile) ? JSON.parse(fs.readFileSync(regFile, "utf8")) : { note: "Authentic receipts awaiting human labels. No label is held here.", receipts: [] };
if (reg.receipts.some((r) => r.sha256 === sha)) fail("exact duplicate of an already registered intake receipt");
const idx = path.join(FUEL, "corpus", "ocr-index.json");
if (fs.existsSync(idx) && Object.values(JSON.parse(fs.readFileSync(idx, "utf8")).images || {}).some((i) => i.sha256 === sha)) fail("exact duplicate of a receipt already in the corpus");
const sameFamily = reg.receipts.filter((r) => r.receipt_family === prov.receipt_family);
if (sameFamily.some((r) => r.split !== opt.split)) fail(`family '${prov.receipt_family}' is already registered under split '${sameFamily[0].split}': a family goes wholly to one side`);

const ext = path.extname(imagePath).toLowerCase() || ".png";
const n = reg.receipts.length + 1;
const id = `intake_${prov.brand_family.replace(/[^A-Za-z]/g, "").toLowerCase()}_${String(n).padStart(3, "0")}`;
const destDir = path.join(FUEL, "corpus", "authentic_intake", "pending");
fs.mkdirSync(destDir, { recursive: true });
fs.writeFileSync(path.join(destDir, `${id}${ext}`), bytes);
reg.receipts.push({
  receipt_id: id, image_path: path.relative(FUEL, path.join(destDir, `${id}${ext}`)).replace(/\\/g, "/"), sha256: sha, registered_at: new Date().toISOString(),
  split: opt.split, receipt_family: prov.receipt_family, brand_family: prov.brand_family, label_status: "pending_human_label", provenance: prov,
});
fs.mkdirSync(path.dirname(regFile), { recursive: true });
fs.writeFileSync(regFile, JSON.stringify(reg, null, 1));
console.log(JSON.stringify({ registered: id, split: opt.split, family: prov.receipt_family, brand: prov.brand_family, label_status: "pending_human_label" }));
