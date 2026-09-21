import { AlertCircle, AlertTriangle, CheckCircle2, Clock, XCircle } from 'lucide-react'

// Never relies on color alone - every status pairs an icon with its label.
const STATUS_MAP = {
  'ACCOUNT VERIFIED': { cls: 'status-success', icon: CheckCircle2, label: 'Verified' },
  VERIFIED: { cls: 'status-success', icon: CheckCircle2, label: 'Verified' },
  PASS: { cls: 'status-success', icon: CheckCircle2, label: 'Pass' },
  EXCEPTION: { cls: 'status-error', icon: AlertTriangle, label: 'Exception' },
  FAIL: { cls: 'status-error', icon: AlertTriangle, label: 'Fail' },
  FAILED: { cls: 'status-error', icon: XCircle, label: 'Failed' },
  'REVIEW REQUIRED': { cls: 'status-warning', icon: AlertCircle, label: 'Review Required' },
  REVIEW_REQUIRED: { cls: 'status-warning', icon: AlertCircle, label: 'Review Required' },
  PROCESSING: { cls: 'status-info', icon: Clock, label: 'Processing' },
  QUEUED: { cls: 'status-neutral', icon: Clock, label: 'Queued' },
  PROCESSED: { cls: 'status-success', icon: CheckCircle2, label: 'Processed' },
  NOT_APPLICABLE: { cls: 'status-neutral', icon: Clock, label: 'Not Applicable' },
}

export function StatusBadge({ status }) {
  const entry = STATUS_MAP[status] || { cls: 'status-neutral', icon: Clock, label: status || 'Unknown' }
  const Icon = entry.icon
  return (
    <span className={`status-badge ${entry.cls}`}>
      <Icon />
      {entry.label}
    </span>
  )
}
