import { getPageCount, linearText, positionedText } from "./src/pdfExtract.mjs";
import { parseBillText } from "./src/billParser.mjs";

const pdfPath = process.argv[2];
const pageNum = parseInt(process.argv[3] || "1", 10);

const count = await getPageCount(pdfPath);
console.log(`pages: ${count}`);

const linear = await linearText(pdfPath, pageNum);
const positioned = await positionedText(pdfPath, pageNum);

console.log(`\n=== LINEAR (page ${pageNum}) ===`);
console.log(linear.slice(0, 500));
console.log(`\n=== POSITIONED (page ${pageNum}) ===`);
console.log(positioned.slice(0, 500));

console.log(`\n=== parseBillText(linear) ===`);
console.log(parseBillText(linear));
console.log(`\n=== parseBillText(positioned) ===`);
console.log(parseBillText(positioned));
