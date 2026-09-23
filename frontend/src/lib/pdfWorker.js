import { pdfjs } from 'react-pdf'

// Points at a thin wrapper (public/pdf-worker-wrapper.mjs) rather than
// pdf.worker.min.mjs directly - see that file for why: it polyfills a
// Uint8Array method pdfjs-dist needs that Electron's bundled Chromium is
// missing, then imports the real worker. pdf.worker.min.mjs itself is
// served as a static asset from public/ (copied from node_modules/pdfjs-dist
// at the same version react-pdf depends on) rather than relying on
// bundler-specific `new URL()` resolution, which behaves differently
// between Vite dev and an Electron build.
pdfjs.GlobalWorkerOptions.workerSrc = '/pdf-worker-wrapper.mjs'
