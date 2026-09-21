import { FileText, Map, UploadCloud } from 'lucide-react'
import { useState } from 'react'
import { Card } from '../components/ui/Card'
import { EmptyState } from '../components/ui/EmptyState'

const TABS = [
  { key: 'expense', label: 'Expense Reports', icon: FileText },
  { key: 'trip', label: 'Trip Reports', icon: Map },
]

export default function UploadPage() {
  const [tab, setTab] = useState('expense')

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div className="btn-group">
        {TABS.map((t) => (
          <button key={t.key} className={tab === t.key ? 'active' : ''} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>

      <Card
        title={tab === 'expense' ? 'Upload Expense Reports' : 'Upload Trip Reports'}
        subtitle="Supports single-file and bulk/multiple-file upload. Files are organized by their own report date, not by upload batch."
      >
        <EmptyState
          icon={UploadCloud}
          title="Document processing arrives in the next phase"
          description="The upload intake UI is being finalized here in Phase 1. PDF extraction, OCR, and the processing queue are built and tested as their own dedicated phase so each stage can be verified independently before real documents are accepted."
        />
      </Card>

      <Card title="Processing Queue" subtitle="Filename, person, type, status, and progress for in-flight uploads">
        <EmptyState title="Queue is empty" description="Files queued for processing will appear here." />
      </Card>
    </div>
  )
}
