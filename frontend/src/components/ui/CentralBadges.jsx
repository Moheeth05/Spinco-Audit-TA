import { AlertCircle, AlertTriangle, Archive, CheckCircle2, Clock, CloudOff, XCircle } from 'lucide-react'
import { StatusBadge } from './StatusBadge'

// Never relies on color alone - icon + label, same look as StatusBadge.
const STORAGE_MAP = {
  STORED: { cls: 'status-success', icon: CheckCircle2, label: 'Stored centrally' },
  PENDING: { cls: 'status-info', icon: Clock, label: 'Pending central copy' },
  NOT_CENTRAL: { cls: 'status-neutral', icon: CloudOff, label: 'Not central' },
  FAILED: { cls: 'status-error', icon: XCircle, label: 'Failed' },
  ARCHIVED: { cls: 'status-neutral', icon: Archive, label: 'Archived' },
  MISSING_EXTERNAL: { cls: 'status-error', icon: AlertTriangle, label: 'Missing from central storage' },
}

/** `state` is the API's storage_state; a missing/unknown value reads as "Not central". */
export function StorageStateBadge({ state }) {
  const entry = STORAGE_MAP[state] || STORAGE_MAP.NOT_CENTRAL
  const Icon = entry.icon
  return (
    <span className={`status-badge ${entry.cls}`}>
      <Icon />
      {entry.label}
    </span>
  )
}

/** `state` is the API's processing_state (NONE | QUEUED | RUNNING | DONE | DEFERRED | FAILED). */
export function ProcessingStateBadge({ state }) {
  if (!state || state === 'NONE') return <span>-</span>
  if (state === 'QUEUED') return <StatusBadge status="QUEUED" />
  if (state === 'RUNNING') return <StatusBadge status="PROCESSING" />
  if (state === 'DONE') return <StatusBadge status="PROCESSED" />
  if (state === 'FAILED') return <StatusBadge status="FAILED" />
  if (state === 'DEFERRED') {
    return (
      <span className="status-badge status-warning">
        <AlertCircle />
        Waiting to retry
      </span>
    )
  }
  return <StatusBadge status={state} />
}
