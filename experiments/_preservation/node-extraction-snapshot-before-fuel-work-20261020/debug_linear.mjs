import { linearText } from "./src/pdfExtract.mjs";
import { parseBillText } from "./src/billParser.mjs";

const pdfPath = process.argv[2];
const pageNum = parseInt(process.argv[3], 10);

const linear = await linearText(pdfPath, pageNum);
console.log("=== LINEAR TEXT ===");
console.log(linear);
console.log("\n=== parseBillText(linear) ===");
console.log(JSON.stringify(parseBillText(linear), null, 2));
