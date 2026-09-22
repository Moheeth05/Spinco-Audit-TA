/** Centralized Excel workbook API calls - components never call fetch/XHR directly. */

const BASE_URL = '/api'

export async function generateWorkbook() {
  const res = await fetch(`${BASE_URL}/excel-files/generate`, { method: 'POST' })
  if (!res.ok) throw new Error('Could not regenerate the workbook.')
  return res.json()
}

/** Returns the download URL only - the caller decides when (if ever) to navigate
 * to it. Never fetched automatically anywhere in this module. */
export function excelDownloadUrl() {
  return `${BASE_URL}/excel-files/download`
}
