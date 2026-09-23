/** Centralized Excel workbook API calls - components never call fetch/XHR directly. */
import { apiFetch } from './client'

const BASE_URL = '/api'

export async function generateWorkbook() {
  const res = await apiFetch('/excel-files/generate', { method: 'POST' })
  return res.json()
}

/** Returns the download URL only - the caller decides when (if ever) to navigate
 * to it. Never fetched automatically anywhere in this module. */
export function excelDownloadUrl() {
  return `${BASE_URL}/excel-files/download`
}
