'use strict';

/** Minimal multipart/form-data parser over a fully buffered body (the request size is capped before this runs). */

class MultipartError extends Error {}

function boundaryOf(contentType) {
  const m = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType || '');
  return m ? (m[1] || m[2]).trim() : null;
}

function parseHeaders(text) {
  const headers = {};
  for (const line of text.split('\r\n')) {
    const i = line.indexOf(':');
    if (i > 0) headers[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
  }
  return headers;
}

function dispositionOf(value = '') {
  const name = /(?:^|;)\s*name="([^"]*)"/i.exec(value);
  const star = /filename\*=(?:UTF-8|utf-8)''([^;]+)/.exec(value);
  const plain = /filename="([^"]*)"/i.exec(value);
  let filename = null;
  if (star) {
    try {
      filename = decodeURIComponent(star[1]);
    } catch {
      filename = star[1];
    }
  } else if (plain) filename = plain[1];
  return { name: name ? name[1] : null, filename };
}

/**
 * @returns {{name:string|null, filename:string|null, contentType:string|null, data:Buffer}[]}
 * @throws MultipartError on a malformed body or more than maxParts parts.
 */
function parseMultipart(body, contentType, { maxParts = 100 } = {}) {
  const boundary = boundaryOf(contentType);
  if (!boundary) throw new MultipartError('Missing multipart boundary.');
  const delim = Buffer.from(`--${boundary}`, 'latin1');
  const parts = [];
  let pos = body.indexOf(delim);
  if (pos === -1) throw new MultipartError('Malformed multipart body.');
  for (;;) {
    pos += delim.length;
    if (body[pos] === 0x2d && body[pos + 1] === 0x2d) break; // closing "--"
    if (body[pos] === 0x0d && body[pos + 1] === 0x0a) pos += 2;
    const headerEnd = body.indexOf('\r\n\r\n', pos, 'latin1');
    if (headerEnd === -1) throw new MultipartError('Malformed multipart headers.');
    const headers = parseHeaders(body.subarray(pos, headerEnd).toString('utf8'));
    const bodyStart = headerEnd + 4;
    const next = body.indexOf(Buffer.from(`\r\n--${boundary}`, 'latin1'), bodyStart);
    if (next === -1) throw new MultipartError('Malformed multipart body (unterminated part).');
    const { name, filename } = dispositionOf(headers['content-disposition']);
    parts.push({ name, filename, contentType: headers['content-type'] || null, data: body.subarray(bodyStart, next) });
    if (parts.length > maxParts) throw new MultipartError(`Too many parts (max ${maxParts}).`);
    pos = next + 2;
  }
  return parts;
}

module.exports = { parseMultipart, MultipartError };
