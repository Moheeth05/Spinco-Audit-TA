/** Centralized upload/document API calls - components never call fetch/XHR directly. */
import { apiFetch, getCsrfToken } from './client'

const BASE_URL = '/api'

/** Uploads one or more files with progress reporting (fetch has no upload-progress event, so XHR is used here). */
export function uploadFiles(sourceType, files, onProgress) {
  const endpoint = sourceType === 'EXPENSE_REPORT' ? 'expense-reports' : 'trip-reports'
  const formData = new FormData()
  files.forEach((file) => formData.append('files', file))

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${BASE_URL}/uploads/${endpoint}`)
    xhr.withCredentials = true
    const csrfToken = getCsrfToken()
    if (csrfToken) xhr.setRequestHeader('X-CSRF-Token', csrfToken)

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
      } else if (xhr.status === 401) {
        reject(new Error('Your session has expired. Please sign in again.'))
      } else if (xhr.status === 403) {
        reject(new Error('You do not have permission to upload documents.'))
      } else {
        reject(new Error(`Upload failed with status ${xhr.status}`))
      }
    }
    xhr.onerror = () => reject(new Error('Upload failed - could not reach the server.'))
    xhr.send(formData)
  })
}

export async function fetchUploads() {
  const res = await apiFetch('/uploads')
  return res.json()
}

export async function fetchDocument(documentId) {
  const res = await apiFetch(`/documents/${encodeURIComponent(documentId)}`)
  return res.json()
}

export async function fetchExtraction(documentId) {
  const res = await apiFetch(`/documents/${encodeURIComponent(documentId)}/extraction`)
  return res.json()
}

export async function reprocessDocument(documentId) {
  const res = await apiFetch(`/documents/${encodeURIComponent(documentId)}/reprocess`, { method: 'POST' })
  return res.json()
}

export function documentFileUrl(documentId, { download = false } = {}) {
  const query = download ? '?download=true' : ''
  return `${BASE_URL}/documents/${encodeURIComponent(documentId)}/file${query}`
}

export async function deleteDocument(documentId) {
  await apiFetch(`/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' })
}
