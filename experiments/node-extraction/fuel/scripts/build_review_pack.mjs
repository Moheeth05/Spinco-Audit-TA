// Builds an OFFLINE human-review pack for the three required fields. Usage:
//   node scripts/build_review_pack.mjs [--scope authentic|pending|all] [--show-provisional 1] [--out <dir>]
// The pack is a static folder: review.html (no network, no external scripts/fonts), manifest.js, INSTRUCTIONS.txt, and review_sheet.csv - a paper worksheet ONLY:
// nothing imports it, the only accepted submission is the JSON exported by review.html (the instructions say so).
// Images are referenced by relative path (not copied) so company receipts are not duplicated. BLIND by default: the provisional
// (assistant-read) labels are NOT shown, so the human's answer is independent and agreement can be measured honestly.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadAnnotations } from "../src/labels.mjs";
import { intakeImageIntegrityProblem } from "../src/intakeRegister.mjs";

const FUEL = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = Object.fromEntries(process.argv.slice(2).reduce((a, x, i, all) => (x.startsWith("--") ? [...a, [x.slice(2), all[i + 1]]] : a), []));
const scope = args.scope || "authentic";
const showProv = args["show-provisional"] === "1";
const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
const outDir = path.resolve(args.out || path.join(FUEL, "verification", `review-pack-${stamp}`));

// The page embeds src/reviewValidation.mjs verbatim (its `export` keywords stripped) so the export check in the browser and apply_verification.mjs enforce the SAME rules.
const rvSrc = fs.readFileSync(path.join(FUEL, "src", "reviewValidation.mjs"), "utf8");
if (/<\/script|<!--/i.test(rvSrc)) throw new Error("reviewValidation.mjs cannot be embedded in an HTML script element");
const rvNames = [...rvSrc.matchAll(/^export (?:const|function) (\w+)/gm)].map((m) => m[1]);
const rvInline = "var ReviewValidation=(function(){\n" + rvSrc.replace(/^export /gm, "") + "\nreturn {" + rvNames.join(",") + "};\n})();";
const rvSha = crypto.createHash("sha256").update(rvSrc).digest("hex");

const { records, summary, eligibility } = loadAnnotations(FUEL, { labels: "all" });
// An invalid intake register (unreadable / not an object / `receipts` missing or not a list) means eligibility of intake receipts cannot be established, and
// apply_verification.mjs would reject every review: so no pack is generated (nothing is written) - the pack never offers what the applier would reject.
if (summary.intake_register_status === "invalid") {
  console.error(`REFUSED: ${summary.intake_register_problem}; eligibility of intake receipts cannot be established, so no pack was generated. Repair or remove intake/intake_register.json.`);
  process.exit(1);
}
// A receipt is offered only if it is exactly what apply_verification.mjs would accept (same eligibility function: trusted corpus source type, or a usable intake
// register entry), is not already human-verified, and (intake) has an AVAILABLE and INTACT image (intakeImageIntegrityProblem: a regular, non-empty file inside corpus/authentic_intake/ whose SHA-256 equals the register's). Intake records come from the register ONLY - once, with their own image path.
const usableIntake = new Map(eligibility.intake.usable.map((e) => [e.receipt_id, e]));
const items = [], notOffered = [];
for (const r of records) {
  if (!eligibility.eligible.has(r.receipt_id)) continue;
  if (r.annotation_status === "human_verified") continue; // already verified
  if (usableIntake.has(r.receipt_id)) {
    if (scope === "authentic") continue;
    const imageProblem = intakeImageIntegrityProblem(FUEL, usableIntake.get(r.receipt_id)); // same availability + sha256 integrity rule apply_verification.mjs enforces (src/intakeRegister.mjs)
    if (imageProblem) { notOffered.push({ id: r.receipt_id, reason: imageProblem }); console.warn(`intake receipt ${r.receipt_id}: not offered (${imageProblem})`); continue; }
    const imageFile = path.join(FUEL, r.image_path);
    items.push({ id: r.receipt_id, image: path.relative(outDir, imageFile).replace(/\\/g, "/"), family: r.receipt_family, kind: "intake", provisional: null });
  } else {
    if (scope === "pending") continue;
    items.push({ id: r.receipt_id, image: path.relative(outDir, path.join(FUEL, "corpus", "authentic", "images", `${r.receipt_id}.png`)).replace(/\\/g, "/"), family: r.receipt_family, kind: "existing", provisional: showProv ? Object.fromEntries(["pump_name", "transaction_date", "total_amount"].map((f) => [f, { value: r.fields[f].normalized_value, raw: r.fields[f].raw_value }])) : null });
  }
}
fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(path.join(outDir, "manifest.js"), `window.REVIEW_MANIFEST = ${JSON.stringify({ pack_id: path.basename(outDir), generated: new Date().toISOString(), blind: !showProv, validator_sha256: rvSha, items }, null, 1)};\n`);

const htmlTemplate = String.raw`<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Fuel receipt label review (offline)</title>
<style>
body{font:14px system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#f3f4f6;color:#111}
header{position:sticky;top:0;background:#0f172a;color:#fff;padding:8px 12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:5}
header input[type=text]{padding:4px;width:170px}
main{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(360px,1fr);gap:12px;padding:12px}
#imgwrap{background:#fff;border:1px solid #cbd5e1;overflow:auto;height:calc(100vh - 90px)}
#imgwrap img{transform-origin:top left;max-width:none}
form{background:#fff;border:1px solid #cbd5e1;padding:12px;overflow:auto;height:calc(100vh - 90px)}
fieldset{margin:0 0 12px;border:1px solid #cbd5e1}legend{font-weight:600}
label{display:block;margin:4px 0}input[type=text],input[type=number],input[type=date],select,textarea{width:100%;box-sizing:border-box;padding:4px}
.warn{background:#fef3c7;border:1px solid #f59e0b;padding:6px;margin-bottom:8px}button{padding:6px 10px}
.small{color:#475569;font-size:12px}
.err{background:#fee2e2;border:1px solid #dc2626;color:#7f1d1d;padding:8px 12px;margin:8px 12px 0;white-space:pre-wrap}
</style></head><body>
<header>
 <b>Fuel receipt label review</b>
 <label>Reviewer ID <input type="text" id="rid" placeholder="employee id / name"></label>
 <label><input type="checkbox" id="auth"> I am authorized by the company to view these receipts</label>
 <span id="progress"></span>
 <button id="prev" type="button">&larr; Prev</button><button id="next" type="button">Next &rarr;</button>
 <button id="zoomIn" type="button">Zoom +</button><button id="zoomOut" type="button">Zoom -</button>
 <button id="export" type="button">Export JSON</button>
</header>
<div id="errors" class="err" role="alert" hidden></div>
<main>
 <div id="imgwrap"><img id="img" alt="receipt"></div>
 <form id="f" autocomplete="off">
  <div class="warn">Read ONLY what is printed on this receipt image. Do not use any other document, the Expense Report, or a claim to decide a value. If you cannot read a field, mark it <b>illegible</b> - do not guess. Everything stays in this browser until you click Export; nothing is sent anywhere.</div>
  <div class="small" id="meta"></div>
  <fieldset><legend>1. Pump / station name</legend>
   <label><input type="radio" name="p_status" value="read"> printed name (dealer / station) is legible</label>
   <label><input type="radio" name="p_status" value="logo_only"> only the oil-company logo/wordmark identifies it (no dealer name printed)</label>
   <label><input type="radio" name="p_status" value="not_printed"> no pump / brand name at all on the receipt</label>
   <label><input type="radio" name="p_status" value="illegible"> illegible</label>
   <label>Name as you would record it (dealer/station name preferred over the brand)<input type="text" id="p_value"></label>
   <label>Exactly as printed (all header lines)<input type="text" id="p_printed"></label>
   <label>Other acceptable forms (comma separated, e.g. brand + dealer)<input type="text" id="p_other"></label>
  </fieldset>
  <fieldset><legend>2. Transaction date (not the print date)</legend>
   <label><input type="radio" name="d_status" value="read"> date is legible</label>
   <label><input type="radio" name="d_status" value="illegible"> illegible / missing</label>
   <label>Date exactly as printed<input type="text" id="d_printed"></label>
   <label>Date as YYYY-MM-DD<input type="date" id="d_iso"></label>
   <label><input type="checkbox" id="d_provable"> the day/month order is provable <b>from this receipt alone</b> (a component &gt; 12, a month name, ISO, an explicit format cue, ...)</label>
   <label>How did you establish the order?
    <select id="d_evidence"><option value="">-</option><option value="component_gt_12">a component is greater than 12</option><option value="month_name_or_iso">month name / ISO format</option><option value="format_cue_on_receipt">format cue printed on the receipt</option><option value="equal_components">day equals month</option><option value="month_in_bill_number">month printed in the bill number</option><option value="other_documents">other receipts / documents (NOT receipt-intrinsic)</option><option value="none">cannot establish</option></select></label>
  </fieldset>
  <fieldset><legend>3. Total amount paid for the fuel</legend>
   <label><input type="radio" name="a_status" value="read"> amount is legible</label>
   <label><input type="radio" name="a_status" value="illegible"> illegible</label>
   <label>Amount exactly as printed<input type="text" id="a_printed"></label>
   <label>Amount (number)<input type="number" id="a_value" step="0.01"></label>
   <div class="small">The printed total of the fuel purchase. Not the rate per litre, litres, totaliser, preset value, points or balance.</div>
  </fieldset>
  <label>Notes (optional)<textarea id="notes" rows="2"></textarea></label>
 </form>
</main>
<script id="rv">/*__RV_INLINE__*/</script>
<script src="manifest.js"></script>
<script>
(function(){
 var M=window.REVIEW_MANIFEST, KEY='fuel-review-state-v1-'+M.pack_id, i=0, zoom=1;
 var S={}; try{S=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
 var $=function(id){return document.getElementById(id)};
 function val(n){var e=document.querySelector('input[name='+n+']:checked');return e?e.value:null}
 function setRadio(n,v){document.querySelectorAll('input[name='+n+']').forEach(function(e){e.checked=(e.value===v)})}
 function read(){return {
  pump_name:{status:val('p_status'),value:$('p_value').value.trim()||null,printed_text:$('p_printed').value.trim()||null,other_acceptable_forms:$('p_other').value.split(',').map(function(x){return x.trim()}).filter(Boolean)},
  transaction_date:{status:val('d_status'),printed_text:$('d_printed').value.trim()||null,iso_date:$('d_iso').value||null,order_provable_from_receipt:$('d_provable').checked,order_evidence:$('d_evidence').value||null},
  total_amount:{status:val('a_status'),printed_text:$('a_printed').value.trim()||null,value:$('a_value').value===''?null:Number($('a_value').value)},
  notes:$('notes').value.trim()||null}}
 function write(r){r=r||{};var p=r.pump_name||{},d=r.transaction_date||{},a=r.total_amount||{};
  setRadio('p_status',p.status||null);$('p_value').value=p.value||'';$('p_printed').value=p.printed_text||'';$('p_other').value=(p.other_acceptable_forms||[]).join(', ');
  setRadio('d_status',d.status||null);$('d_printed').value=d.printed_text||'';$('d_iso').value=d.iso_date||'';$('d_provable').checked=!!d.order_provable_from_receipt;$('d_evidence').value=d.order_evidence||'';
  setRadio('a_status',a.status||null);$('a_printed').value=a.printed_text||'';$('a_value').value=(a.value==null?'':a.value);$('notes').value=r.notes||''}
 function save(){S[M.items[i].id]=read();localStorage.setItem(KEY,JSON.stringify(S));progress()}
 function progress(){var done=M.items.filter(function(it){var r=S[it.id];return r&&r.pump_name.status&&r.transaction_date.status&&r.total_amount.status}).length;$('progress').textContent=(i+1)+' / '+M.items.length+'  (complete: '+done+')'}
 function show(){var it=M.items[i];$('img').src=it.image;$('img').style.transform='scale('+zoom+')';write(S[it.id]);progress();
  $('meta').textContent='Receipt '+it.id+(M.blind?'  (blind: provisional labels hidden)':'  PROVISIONAL labels shown: '+JSON.stringify(it.provisional))}
 // Choosing "illegible" / "not printed" clears that field's value immediately (a status and a value must never contradict each other);
 // the printed-text box is kept. Then the state is saved, so what is on screen is exactly what is stored and exported.
 var CLEARS={p_status:['illegible','not_printed'],d_status:['illegible'],a_status:['illegible']};
 function clearValues(name){
  if(name==='p_status'){$('p_value').value='';$('p_other').value=''}
  if(name==='d_status'){$('d_iso').value='';$('d_provable').checked=false;$('d_evidence').value=''}
  if(name==='a_status'){$('a_value').value=''}}
 function onEdit(ev){var t=ev.target;
  if(t&&t.type==='radio'&&t.checked&&CLEARS[t.name]&&CLEARS[t.name].indexOf(t.value)>=0)clearValues(t.name);
  save()}
 document.getElementById('f').addEventListener('input',onEdit);document.getElementById('f').addEventListener('change',onEdit);
 $('prev').onclick=function(){save();if(i>0){i--;show()}};$('next').onclick=function(){save();if(i<M.items.length-1){i++;show()}};
 $('zoomIn').onclick=function(){zoom=Math.min(4,zoom+0.25);show()};$('zoomOut').onclick=function(){zoom=Math.max(0.25,zoom-0.25);show()};
 // Export is validated with the same rules apply_verification.mjs enforces (embedded copy of src/reviewValidation.mjs). An invalid export is never downloaded.
 $('export').onclick=function(){save();var box=$('errors');box.hidden=true;box.textContent='';
  var res=ReviewValidation.buildExportPayload({items:M.items,state:S,reviewerId:$('rid').value,authorizationConfirmed:$('auth').checked,packId:M.pack_id,blind:M.blind,exportedAt:new Date().toISOString()});
  if(!res.ok){box.textContent='Export blocked - fix these first:\n'+res.errors.slice(0,15).join('\n')+(res.errors.length>15?'\n... and '+(res.errors.length-15)+' more':'');box.hidden=false;return}
  if(res.incomplete.length&&!confirm(res.incomplete.length+' receipt(s) are incomplete and will be exported WITHOUT verification:\n'+res.incomplete.slice(0,10).join('\n')+'\nExport anyway?'))return;
  var blob=new Blob([JSON.stringify(res.payload,null,2)],{type:'application/json'});
  var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='fuel-review-'+M.pack_id+'-'+res.payload.reviewer_id.replace(/[^A-Za-z0-9_-]/g,'_')+'.json';a.click()};
 show();
})();
</script></body></html>`;
const html = htmlTemplate.replace("/*__RV_INLINE__*/", () => rvInline);
fs.writeFileSync(path.join(outDir, "review.html"), html);

const csvHead = "receipt_id,image,reviewer_id,authorization_confirmed(true/false),pump_status(read|logo_only|not_printed|illegible),pump_name,pump_printed_text,pump_other_forms(;),date_status(read|illegible),date_printed_text,date_iso(YYYY-MM-DD),order_provable_from_receipt(true/false),order_evidence,amount_status(read|illegible),amount_printed_text,amount_value,notes";
fs.writeFileSync(path.join(outDir, "review_sheet.csv"), [csvHead, ...items.map((it) => `${it.id},${it.image},,,,,,,,,,,,,,,`)].join("\n") + "\n");
fs.writeFileSync(path.join(outDir, "INSTRUCTIONS.txt"), [
  "FUEL RECEIPT LABEL REVIEW - instructions for the authorized reviewer",
  "",
  "Who: only a person the company has authorized to view these receipts. Tick the authorization box only if that is true.",
  "What: for each receipt record three things as PRINTED on that image: the pump/station name, the transaction date, and the total amount paid for the fuel.",
  "Do not use any other document, the Expense Report, or a claim. If a field is unreadable choose 'illegible' - never guess.",
  "Date: record the transaction date (not 'Printed on'). If day and month are both 12 or less and nothing ON THE RECEIPT proves the order, untick 'provable from this receipt alone' and say how you decided.",
  "Amount: the printed total of the fuel purchase - not rate per litre, litres, totaliser, preset value, points or balance.",
  `Mode: ${showProv ? "PROVISIONAL LABELS SHOWN (not blind - agreement statistics will be biased)" : "BLIND - provisional labels are hidden"}.`,
  "",
  "Open review.html in a browser (works offline). Your answers are kept in the browser only; click 'Export JSON' and send the file to the experiment owner.",
  "review_sheet.csv is only a paper worksheet listing the receipts: there is NO importer for it, so answers written in it are NOT recorded or verified - only the JSON file exported by review.html is accepted.",
  "Choosing 'illegible' or 'not printed' clears that field's value. 'Export JSON' is blocked, with a message, until every completed receipt is consistent (real YYYY-MM-DD date, numeric amount, a value for every 'read' field, and no value on an illegible field).",
  "Two independent reviewers per receipt is recommended. For two reviewers to count as independent agreement, their exported JSON files must be applied together in ONE run (a separate later review of an already-verified receipt is only logged, it does not add a second reviewer to the label). Where reviewers disagree the receipt goes to adjudication and is NOT marked verified.",
].join("\n"));
console.log(`review pack: ${outDir}\n  receipts: ${items.length} (${items.filter((x) => x.kind === "existing").length} existing authentic, ${items.filter((x) => x.kind === "intake").length} new intake)  blind=${!showProv}\n  intake entries not offered: ${summary.skipped_intake_entries} rejected by validation, ${notOffered.length} with an unavailable or unverified image`);
