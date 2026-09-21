import { pdfjs } from 'react-pdf'

// Served as a static asset from public/ (copied from node_modules/pdfjs-dist at the
// same version react-pdf depends on) rather than relying on bundler-specific `new URL()`
// resolution, which behaves differently between Vite dev and an eventual Electron build.
pdfjs.GlobalWorkerOptions.workerSrc = '/pdf.worker.min.mjs'
