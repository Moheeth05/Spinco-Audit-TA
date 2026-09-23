// Electron 33's bundled Chromium predates the TC39 "Uint8Array to/from
// base64 and hex" proposal (Uint8Array.prototype.toHex/fromHex/toBase64/
// fromBase64), which a recent standalone Chrome already ships. pdfjs-dist
// (react-pdf's PDF rendering engine) calls Uint8Array.prototype.toHex()
// internally - without it, opening any PDF inside the packaged desktop app
// throws "n.toHex is not a function" and blanks the whole React tree
// (confirmed via Chrome DevTools Protocol exception capture: identical
// production build renders correctly in a standalone Chrome browser and in
// Vite dev mode, only Electron's older bundled Chromium fails). Each
// polyfill below is a no-op if the method already exists natively, so this
// has no effect once Electron ships a newer Chromium.
const HEX_CHARS = '0123456789abcdef'

if (!Uint8Array.prototype.toHex) {
  Uint8Array.prototype.toHex = function toHex() {
    let out = ''
    for (let i = 0; i < this.length; i++) {
      out += HEX_CHARS[this[i] >> 4] + HEX_CHARS[this[i] & 0x0f]
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

// Same story for the Map/WeakMap "upsert" proposal methods pdfjs-dist also
// calls - kept in sync with public/pdf-worker-wrapper.mjs, which needs its
// own copy since a Worker has its own JS realm this file's polyfills never
// reach.
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
