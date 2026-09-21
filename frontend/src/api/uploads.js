/** Centralized upload/document API calls - components never call fetch/XHR directly. */

const BASE_URL = '/api'

/** Uploads one or more files with progress reporting (fetch has no upload-progress event, so XHR is used here). */
export function uploadFiles(sourceType, files, onProgress) {
  const endpoint = sourceType === 'EXPENSE_REPORT' ? 'expense-reports' : 'trip-reports'
  const formData = new FormData()
  files.forEach((file) => formData.append('files', file))

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${BASE_URL}/uploads/${endpoint}`)

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText))
        } catch {
          reject(new Error('The server returned an unreadable response.'))
        }
      } else {
        reject(new Error(`Upload failed with status ${xhr.status}`))
      }
    }
    xhr.onerror = () => reject(new Error('Upload failed - could not reach the server.'))
    xhr.send(formData)
  })
}

export async function fetchUploads() {
  const res = await fetch(`${BASE_URL}/uploads`)
  if (!res.ok) throw new Error('Could not load uploaded files.')
  return res.json()
}

export async function fetchDocument(documentId) {
  const res = await fetch(`${BASE_URL}/documents/${encodeURIComponent(documentId)}`)
  if (!res.ok) throw new Error('Could not load document details.')
  return res.json()
}

export async function fetchExtraction(documentId) {
  const res = await fetch(`${BASE_URL}/documents/${encodeURIComponent(documentId)}/extraction`)
  if (!res.ok) throw new Error('Could not load extracted details.')
  return res.json()
}

export async function reprocessDocument(documentId) {
  const res = await fetch(`${BASE_URL}/documents/${encodeURIComponent(documentId)}/reprocess`, { method: 'POST' })
  if (!res.ok) throw new Error('Could not reprocess this document.')
  return res.json()
}

export function documentFileUrl(documentId, { download = false } = {}) {
  const query = download ? '?download=true' : ''
  return `${BASE_URL}/documents/${encodeURIComponent(documentId)}/file${query}`
}

export async function deleteDocument(documentId) {
  const res = await fetch(`${BASE_URL}/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('Could not delete this document.')
}
