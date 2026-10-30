// Minimal PNG decode/encode on node:zlib only (no dependencies), enough for the fixed preprocessing used by the OCR retry.
// Supports 8-bit, non-interlaced PNG of colour types 0 (grey), 2 (RGB), 3 (palette), 4 (grey+alpha), 6 (RGBA).
// Anything else throws: the caller then simply skips the preprocessing retry (never a silent wrong decode).
import zlib from "node:zlib";

const SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

export function decodePng(buf) {
  if (!buf.subarray(0, 8).equals(SIG)) throw new Error("not a PNG");
  let pos = 8, ihdr = null, plte = null;
  const idat = [];
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos), type = buf.toString("ascii", pos + 4, pos + 8), data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === "IHDR") ihdr = { w: data.readUInt32BE(0), h: data.readUInt32BE(4), depth: data[8], color: data[9], interlace: data[12] };
    else if (type === "PLTE") plte = data;
    else if (type === "IDAT") idat.push(data);
    else if (type === "IEND") break;
    pos += 12 + len;
  }
  if (!ihdr) throw new Error("no IHDR");
  if (ihdr.depth !== 8 || ihdr.interlace !== 0) throw new Error(`unsupported PNG (depth ${ihdr.depth}, interlace ${ihdr.interlace})`);
  const ch = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[ihdr.color];
  if (!ch) throw new Error(`unsupported colour type ${ihdr.color}`);
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = ihdr.w * ch;
  const out = Buffer.alloc(ihdr.h * stride);
  for (let y = 0; y < ihdr.h; y++) {
    const f = raw[y * (stride + 1)];
    const row = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    for (let x = 0; x < stride; x++) {
      const a = x >= ch ? out[y * stride + x - ch] : 0;
      const b = y > 0 ? out[(y - 1) * stride + x] : 0;
      const c = y > 0 && x >= ch ? out[(y - 1) * stride + x - ch] : 0;
      let v = row[x];
      if (f === 1) v += a;
      else if (f === 2) v += b;
      else if (f === 3) v += (a + b) >> 1;
      else if (f === 4) {
        const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      } else if (f !== 0) throw new Error(`bad filter ${f}`);
      out[y * stride + x] = v & 255;
    }
  }
  return { width: ihdr.w, height: ihdr.h, channels: ch, color: ihdr.color, data: out, palette: plte };
}

export function toGrey(img) {
  const { width: w, height: h, channels: ch, color, data, palette } = img;
  const g = Buffer.alloc(w * h);
  for (let i = 0; i < w * h; i++) {
    if (color === 0 || color === 4) g[i] = data[i * ch];
    else if (color === 3) { const k = data[i] * 3; g[i] = Math.round(0.299 * palette[k] + 0.587 * palette[k + 1] + 0.114 * palette[k + 2]); }
    else g[i] = Math.round(0.299 * data[i * ch] + 0.587 * data[i * ch + 1] + 0.114 * data[i * ch + 2]);
  }
  return { width: w, height: h, data: g };
}

// The same fixed recipe as the production retry (backend ocr_engine.preprocess_for_difficult_ocr): grey -> contrast x1.6 about the mean -> threshold 140.
export function preprocessForDifficultOcr(img, { contrast = 1.6, threshold = 140 } = {}) {
  const g = toGrey(img);
  let sum = 0;
  for (let i = 0; i < g.data.length; i++) sum += g.data[i];
  const mean = sum / g.data.length;
  const out = Buffer.alloc(g.data.length);
  for (let i = 0; i < g.data.length; i++) {
    const v = Math.max(0, Math.min(255, mean + contrast * (g.data[i] - mean)));
    out[i] = v < threshold ? 0 : 255;
  }
  return { width: g.width, height: g.height, data: out };
}

const CRC = (() => { const t = new Uint32Array(256); for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
const crc32 = (b) => { let c = 0xffffffff; for (let i = 0; i < b.length; i++) c = CRC[(c ^ b[i]) & 255] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
const chunk = (type, data) => { const len = Buffer.alloc(4); len.writeUInt32BE(data.length); const td = Buffer.concat([Buffer.from(type, "ascii"), data]); const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td)); return Buffer.concat([len, td, crc]); };

export function encodeGreyPng({ width, height, data }) {
  const rows = Buffer.alloc(height * (width + 1));
  for (let y = 0; y < height; y++) { rows[y * (width + 1)] = 0; data.copy(rows, y * (width + 1) + 1, y * width, (y + 1) * width); }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0); ihdr.writeUInt32BE(height, 4); ihdr[8] = 8; ihdr[9] = 0; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([SIG, chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(rows, { level: 6 })), chunk("IEND", Buffer.alloc(0))]);
}
