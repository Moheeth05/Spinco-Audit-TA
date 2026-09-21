import { useApi } from '../api/useApi'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'

function formatLimit(policy) {
  if (policy.reimbursement_basis === 'ACTUAL') return 'Actual'
  if (policy.limit_amount === null || policy.limit_amount === undefined) return '-'
  return `Rs.${Number(policy.limit_amount).toLocaleString('en-IN')}`
}

const columns = [
  { key: 's_no', header: 'S.No', render: (r) => r.s_no },
  { key: 'category', header: 'Category' },
  { key: 'reimbursement_basis', header: 'Reimbursement Basis' },
  { key: 'limit', header: 'Limit', align: 'right', render: formatLimit },
  { key: 'document_required', header: 'Document Required' },
  { key: 'claim_type', header: 'Claim Type' },
  { key: 'status', header: 'Status' },
  { key: 'effective_date', header: 'Effective Date' },
  { key: 'policy_version', header: 'Version' },
]

export default function PolicyMasterPage() {
  const { data: policies, loading, error } = useApi('/policy-master')
  const rows = policies ? policies.map((p, i) => ({ ...p, s_no: i + 1 })) : null

  return (
    <Card
      title="Policy Master"
      subtitle="Controlled configuration of the 29 Travel & Expense categories. Policy changes are versioned and auditable."
    >
      {loading && <LoadingState rows={8} />}
      {error && <ErrorState description="Could not load the policy master from the audit engine." />}
      {!loading && !error && rows && <DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />}
    </Card>
  )
}
