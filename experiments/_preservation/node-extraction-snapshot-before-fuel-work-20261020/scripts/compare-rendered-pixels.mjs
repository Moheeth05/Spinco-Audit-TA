// Pixel-level comparison of the two renders, using only @napi-rs/canvas
// (already a dependency) - no new package added.
import { createCanvas, loadImage } from "@napi-rs/canvas";
import { readFile } from "node:fs/promises";

const OUT_DIR = "reports/ocr-diagnostic-page6";

async function stats(path, label) {
  const buf = await readFile(path);
  const img = await loadImage(buf);
  const canvas = createCanvas(img.width, img.height);
  const ctx = canvas.getContext("2d");
  ctx.drawImage(img, 0, 0);
  const { data } = ctx.getImageData(0, 0, img.width, img.height);

  let sumR = 0, sumG = 0, sumB = 0, sumA = 0;
  let minLum = 255, maxLum = 0;
  const histogram = new Array(26).fill(0); // 10-wide luminance buckets
  const n = img.width * img.height;
  for (let i = 0; i < data.length; i += 4) {
    const r = data[i], g = data[i + 1], b = data[i + 2], a = data[i + 3];
    sumR += r; sumG += g; sumB += b; sumA += a;
    const lum = 0.299 * r + 0.587 * g + 0.114 * b;
    if (lum < minLum) minLum = lum;
    if (lum > maxLum) maxLum = lum;
    histogram[Math.min(25, Math.floor(lum / 10))]++;
  }
  console.log(`\n=== ${label} (${img.width}x${img.height}, ${n} px) ===`);
  console.log(`  mean R=${(sumR / n).toFixed(1)} G=${(sumG / n).toFixed(1)} B=${(sumB / n).toFixed(1)} A=${(sumA / n).toFixed(1)}`);
  console.log(`  luminance range: min=${minLum.toFixed(1)} max=${maxLum.toFixed(1)}`);
  console.log(`  luminance histogram (10-wide buckets, 0-255): ${histogram.join(",")}`);
  return { width: img.width, height: img.height, meanR: sumR / n, meanG: sumG / n, meanB: sumB / n, meanA: sumA / n, minLum, maxLum };
}

const nodeStats = await stats(`${OUT_DIR}/node-render-dpi200.png`, "Node render (pdfjs-dist + canvas)");
const pyStats = await stats(`${OUT_DIR}/python-render-dpi200.png`, "Python render (PyMuPDF)");

console.log("\n=== Delta (Python - Node) ===");
console.log(`  meanR: ${(pyStats.meanR - nodeStats.meanR).toFixed(1)}`);
console.log(`  meanG: ${(pyStats.meanG - nodeStats.meanG).toFixed(1)}`);
console.log(`  meanB: ${(pyStats.meanB - nodeStats.meanB).toFixed(1)}`);
console.log(`  meanA: ${(pyStats.meanA - nodeStats.meanA).toFixed(1)}`);
