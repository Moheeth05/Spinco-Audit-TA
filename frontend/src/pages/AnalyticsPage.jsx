import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useApi } from '../api/useApi'
import { HorizontalBarList } from '../components/charts/HorizontalBarList'
import { MonthlyVolumeChart } from '../components/charts/MonthlyVolumeChart'
import { StatusSummaryBar } from '../components/charts/StatusSummaryBar'
import { TrendLineChart } from '../components/charts/TrendLineChart'
import { Card } from '../components/ui/Card'
import { DataTable } from '../components/ui/DataTable'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { KPICard } from '../components/ui/KPICard'
import { LoadingState } from '../components/ui/LoadingState'

function formatINR(amount) {
  return `Rs.${Number(amount || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function formatINRCompact(amount) {
  return `Rs.${Number(amount || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
}

function Section({ title, subtitle, loading, error, empty, emptyDescription, children }) {
  return (
    <Card title={title} subtitle={subtitle}>
      {loading && <LoadingState rows={3} />}
      {!loading && error && <ErrorState title="Could not load this data" description="The server may be unreachable. Try again shortly." />}
      {!loading && !error && empty && <EmptyState title="No data for the selected period" description={emptyDescription} />}
      {!loading && !error && !empty && children}
    </Card>
  )
}

export default function AnalyticsPage() {
  const navigate = useNavigate()
  const [month, setMonth] = useState('')

  const filters = month ? { month } : undefined

  const { data: months } = useApi('/analytics/months')
  const { data: summary, loading: summaryLoading, error: summaryError } = useApi('/analytics/summary', filters)
  const { data: statusPoints, loading: statusLoading, error: statusError } = useApi('/analytics/status', filters)
  const { data: monthlyVolume, loading: volumeLoading, error: volumeError } = useApi('/analytics/monthly')
  const { data: monthlyClaimed, loading: claimedTrendLoading, error: claimedTrendError } = useApi('/analytics/monthly-claimed')
  const { data: categories, loading: categoriesLoading, error: categoriesError } = useApi('/analytics/categories', filters)
  const { data: exceptionTypes, loading: excLoading, error: excError } = useApi('/analytics/exceptions', filters)
  const { data: exceptionMonthly, loading: excMonthlyLoading, error: excMonthlyError } = useApi('/analytics/exceptions/monthly')
  const { data: billSummary, loading: billLoading, error: billError } = useApi('/analytics/bills', filters)
  const { data: tripMatch, loading: tripMatchLoading, error: tripMatchError } = useApi('/analytics/trips', filters)
  const { data: tripReports, loading: tripReportsLoading, error: tripReportsError } = useApi('/analytics/trip-reports')
  const { data: policyLimits, loading: policyLoading, error: policyError } = useApi('/analytics/policy-limits', filters)
  const { data: employeeClaimed, loading: empLoading, error: empError } = useApi('/analytics/employee-claimed')

  function goToStatus(status) {
    if (month) navigate(`/expense-reports/${month}?status=${encodeURIComponent(status)}`)
    else navigate('/expense-reports')
  }

  const confirmedExceptions = (exceptionTypes || []).filter((e) => e.severity === 'EXCEPTION')
  const reviewConditions = (exceptionTypes || []).filter((e) => e.severity === 'REVIEW REQUIRED')

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>
      <Card title="Analytics" subtitle="Detailed analysis of the current audit database state">
        <div className="filter-bar">
          <label style={{ fontSize: 12.5, color: 'var(--gray-600)', marginRight: 8 }}>Report Month</label>
          <select className="input" style={{ maxWidth: 220 }} value={month} onChange={(e) => setMonth(e.target.value)}>
            <option value="">All Months</option>
            {(months || []).map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
      </Card>

      {summaryLoading && <LoadingState rows={2} />}
      {summaryError && <ErrorState title="Could not load summary" description="The server may be unreachable." />}
      {!summaryLoading && !summaryError && summary && (
        <div className="kpi-grid">
          <KPICard label="Total Reports" value={summary.total_reports} />
          <KPICard label="Account Verified" value={summary.account_verified_reports} />
          <KPICard label="Exceptions" value={summary.exception_reports} />
          <KPICard label="Review Required" value={summary.review_required_reports} />
          <KPICard label="Total Claimed" value={formatINR(summary.total_claimed_amount)} />
          <KPICard label="Total Expenses" value={summary.total_expenses} />
        </div>
      )}

      <Section
        title="Report Status"
        subtitle="ACCOUNT VERIFIED / EXCEPTION / REVIEW REQUIRED - click a segment to view those reports"
        loading={statusLoading}
        error={statusError}
        empty={!statusPoints || statusPoints.every((p) => p.count === 0)}
        emptyDescription="No expense reports for the selected period."
      >
        {statusPoints && <StatusSummaryBar points={statusPoints} onSegmentClick={goToStatus} />}
      </Section>

      <div className="grid-2">
        <Section
          title="Monthly Report Trend"
          subtitle="Expense Reports vs Trip Reports, by report month"
          loading={volumeLoading}
          error={volumeError}
          empty={!monthlyVolume || monthlyVolume.length === 0}
          emptyDescription="Trends appear once expense/trip reports are processed."
        >
          {monthlyVolume && <MonthlyVolumeChart data={monthlyVolume} />}
        </Section>

        <Section
          title="Monthly Claimed Amount"
          subtitle="Sum of each report's own stated total, by report month"
          loading={claimedTrendLoading}
          error={claimedTrendError}
          empty={!monthlyClaimed || monthlyClaimed.length === 0}
          emptyDescription="Trends appear once expense reports are processed."
        >
          {monthlyClaimed && <TrendLineChart data={monthlyClaimed} dataKey="claimed_amount" valueFormatter={formatINRCompact} />}
        </Section>
      </div>

      <Section
        title="Category Analysis"
        subtitle="Expense count and claimed amount by policy category"
        loading={categoriesLoading}
        error={categoriesError}
        empty={!categories || categories.length === 0}
        emptyDescription="Category data appears once expense reports are processed."
      >
        {categories && (
          <DataTable
            columns={[
              { key: 'category', header: 'Category' },
              { key: 'expense_count', header: 'Expense Count', align: 'right' },
              { key: 'claimed_amount', header: 'Claimed Amount', align: 'right', render: (r) => formatINR(r.claimed_amount) },
            ]}
            rows={categories}
            rowKey={(r) => r.category}
          />
        )}
      </Section>

      <Card title="Exception Analysis" subtitle="Confirmed exceptions and review-required conditions, by exact Phase 7 type">
        <div className="grid-2">
          <Section
            title="Confirmed Exceptions by Type"
            loading={excLoading}
            error={excError}
            empty={confirmedExceptions.length === 0}
            emptyDescription="No confirmed exceptions for the selected period."
          >
            <HorizontalBarList items={confirmedExceptions} labelKey="exception_type" valueKey="count" color="var(--error)" />
          </Section>
          <Section
            title="Review-Required Conditions by Type"
            loading={excLoading}
            error={excError}
            empty={reviewConditions.length === 0}
            emptyDescription="No review-required conditions for the selected period."
          >
            <HorizontalBarList items={reviewConditions} labelKey="exception_type" valueKey="count" color="var(--warning)" />
          </Section>
        </div>
        <div style={{ marginTop: 16 }}>
          <Section
            title="Exceptions Over Time"
            subtitle="Confirmed exceptions per report month (parent report's own month, not when the row was last computed)"
            loading={excMonthlyLoading}
            error={excMonthlyError}
            empty={!exceptionMonthly || exceptionMonthly.length === 0}
            emptyDescription="No exception history to show yet."
          >
            {exceptionMonthly && <TrendLineChart data={exceptionMonthly} dataKey="exception_count" color="var(--error)" />}
          </Section>
        </div>
      </Card>

      <div className="grid-2">
        <Section
          title="Bill Matching"
          subtitle="Phase 4 matching outcomes across all expenses"
          loading={billLoading}
          error={billError}
          empty={!billSummary || billSummary.by_status.length === 0}
          emptyDescription="No bill matching data yet."
        >
          {billSummary && <StatusSummaryBar points={billSummary.by_status} />}
        </Section>

        <Section
          title="Batta / Trip Matching"
          subtitle="Phase 6 matching outcomes for BATTA expenses only"
          loading={tripMatchLoading}
          error={tripMatchError}
          empty={!tripMatch || tripMatch.total_batta_expenses === 0}
          emptyDescription="No BATTA expenses for the selected period."
        >
          {tripMatch && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div style={{ display: 'flex', gap: 28 }}>
                <div>
                  <div style={{ fontSize: 20, fontWeight: 700 }}>{tripMatch.total_batta_expenses}</div>
                  <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>Total Batta Expenses</div>
                </div>
                <div>
                  <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--success)' }}>{tripMatch.matched_batta_expenses}</div>
                  <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>Matched</div>
                </div>
                <div>
                  <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--error)' }}>{tripMatch.unmatched_batta_expenses}</div>
                  <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>Unmatched</div>
                </div>
              </div>
              <StatusSummaryBar points={tripMatch.by_status} />
            </div>
          )}
        </Section>
      </div>

      <Card title="Trip Analytics" subtitle="Standalone Trip Reports (embedded TRIP SUMMARY sections shown on their Expense Report)">
        {tripReportsLoading && <LoadingState rows={3} />}
        {tripReportsError && <ErrorState description="Could not load trip analytics." />}
        {!tripReportsLoading && !tripReportsError && tripReports && (
          <div className="grid-2">
            <div>
              <div style={{ display: 'flex', gap: 28, marginBottom: 16 }}>
                <div>
                  <div style={{ fontSize: 20, fontWeight: 700 }}>{tripReports.total_trips}</div>
                  <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>Total Trips</div>
                </div>
                <div>
                  <div style={{ fontSize: 20, fontWeight: 700 }}>{tripReports.batta_linked_trips}</div>
                  <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>Batta-Linked Trips</div>
                </div>
              </div>
              {tripReports.by_month.length > 0 ? (
                <TrendLineChart data={tripReports.by_month} dataKey="trip_count" color="var(--navy-700)" />
              ) : (
                <EmptyState title="No trip data yet" description="Trip trends appear once Trip Reports are processed." />
              )}
            </div>
            <div>
              <div className="section-title" style={{ fontSize: 13, marginBottom: 10 }}>
                Trips by Destination
              </div>
              {tripReports.by_destination.length > 0 ? (
                <DataTable
                  columns={[
                    { key: 'destination', header: 'Destination' },
                    { key: 'trip_count', header: 'Trips', align: 'right' },
                  ]}
                  rows={tripReports.by_destination}
                  rowKey={(r) => r.destination}
                />
              ) : (
                <EmptyState title="No destination data yet" />
              )}
            </div>
          </div>
        )}
      </Card>

      <Section
        title="Policy Limit Analysis"
        subtitle="Fixed-limit (LIMIT-basis) categories only - ACTUAL-basis categories have no numeric limit and are shown separately"
        loading={policyLoading}
        error={policyError}
        empty={!policyLimits || (policyLimits.evaluated_fixed_limit === 0 && policyLimits.not_applicable_actual_basis === 0)}
        emptyDescription="No policy-checked expenses for the selected period."
      >
        {policyLimits && (
          <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap' }}>
            <div>
              <div style={{ fontSize: 20, fontWeight: 700 }}>{policyLimits.evaluated_fixed_limit}</div>
              <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>Evaluated Against Fixed Limit</div>
            </div>
            <div>
              <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--success)' }}>{policyLimits.passed_within_limit}</div>
              <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>Within Limit</div>
            </div>
            <div>
              <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--error)' }}>{policyLimits.over_limit}</div>
              <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>Over Limit</div>
            </div>
            <div>
              <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--warning)' }}>{policyLimits.review_required}</div>
              <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>Review Required</div>
            </div>
            <div>
              <div style={{ fontSize: 20, fontWeight: 700 }}>{policyLimits.not_applicable_actual_basis}</div>
              <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>ACTUAL Basis (No Fixed Limit)</div>
            </div>
            <div>
              <div style={{ fontSize: 20, fontWeight: 700 }}>{formatINR(policyLimits.over_limit_claimed_amount)}</div>
              <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>Claimed Amount Over Limit</div>
            </div>
          </div>
        )}
      </Section>

      <Section
        title="Claimed Amount by Employee"
        subtitle="Descriptive aggregate only - not a ranking or performance measure"
        loading={empLoading}
        error={empError}
        empty={!employeeClaimed || employeeClaimed.length === 0}
        emptyDescription="Employee spending appears once expense reports are processed."
      >
        {employeeClaimed && (
          <HorizontalBarList items={employeeClaimed} labelKey="employee_name" valueKey="claimed_amount" formatValue={formatINR} />
        )}
      </Section>
    </div>
  )
}
