// Loaded as this app's PDF.js worker (see lib/pdfWorker.js) instead of
// pdf.worker.min.mjs directly. A Web Worker runs in its own JS realm with
// its own Uint8Array global, so the main-thread polyfill in
// lib/typedArrayPolyfill.js never reaches code running in here - and
// pdfjs-dist calls Uint8Array.prototype.toHex() from inside this worker,
// which Electron 33's bundled Chromium doesn't implement yet (a recent
// standalone Chrome does - see typedArrayPolyfill.js for the full
// explanation). Each polyfill is a no-op if already natively supported.
if (!Uint8Array.prototype.toHex) {
  Uint8Array.prototype.toHex = function toHex() {
    let out = ''
    for (let i = 0; i < this.length; i++) {
      out += this[i].toString(16).padStart(2, '0')
    }
    return out
  }
}

if (!Uint8Array.fromHex) {
  Uint8Array.fromHex = function fromHex(hex) {
    const bytes = new Uint8Array(hex.length / 2)
    for (let i = 0; i < bytes.length; i++) {
      bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16)
    }
    return bytes
  }
}

if (!Uint8Array.prototype.toBase64) {
  Uint8Array.prototype.toBase64 = function toBase64() {
    let binary = ''
    for (let i = 0; i < this.length; i++) binary += String.fromCharCode(this[i])
    return btoa(binary)
  }
}

if (!Uint8Array.fromBase64) {
  Uint8Array.fromBase64 = function fromBase64(base64) {
    const binary = atob(base64)
    const bytes = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
    return bytes
  }
}

// Same story as the Uint8Array methods above, for the (also very new)
// Map/WeakMap "upsert" proposal methods.
for (const Ctor of [Map, WeakMap]) {
  if (!Ctor.prototype.getOrInsert) {
    Ctor.prototype.getOrInsert = function getOrInsert(key, value) {
      if (!this.has(key)) this.set(key, value)
      return this.get(key)
    }
  }
  if (!Ctor.prototype.getOrInsertComputed) {
    Ctor.prototype.getOrInsertComputed = function getOrInsertComputed(key, callbackFn) {
      if (!this.has(key)) this.set(key, callbackFn(key))
      return this.get(key)
    }
  }
}

import './pdf.worker.min.mjs'
