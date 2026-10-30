import { linearText, positionedText } from "./src/pdfExtract.mjs";
import { parseBillText } from "./src/billParser.mjs";

const pdfPath = process.argv[2];
const pageNum = parseInt(process.argv[3], 10);

const positioned = await positionedText(pdfPath, pageNum);
console.log("=== POSITIONED TEXT ===");
console.log(positioned);
console.log("\n=== parseBillText(positioned) ===");
console.log(JSON.stringify(parseBillText(positioned), null, 2));
