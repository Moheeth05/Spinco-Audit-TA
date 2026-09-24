import { ChevronLeft, ChevronRight, Download, FileSpreadsheet, RefreshCw, Search } from 'lucide-react'
import { useState } from 'react'
import { excelDownloadUrl, generateWorkbook } from '../api/excel'
import { useApi } from '../api/useApi'
import { Button } from '../components/ui/Button'
import { Card } from '../components/ui/Card'
import { EmptyState } from '../components/ui/EmptyState'
import { ErrorState } from '../components/ui/ErrorState'
import { LoadingState } from '../components/ui/LoadingState'
import { StatusBadge } from '../components/ui/StatusBadge'
import { formatISTDateTime } from '../lib/datetime'

const PAGE_SIZE = 25
const DEFAULT_SHEET = 'Expense Reports'

function formatINR(amount) {
  if (amount === null || amount === undefined) return '-'
  return `Rs.${Number(amount).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function formatCell(value) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(value)) {
    const hasTime = value.slice(11, 19) !== '00:00:00'
    // A genuine time component only appears on system-generated timestamps
    // (business dates in the workbook are always midnight); date-only cells
    // are calendar dates and must render exactly as stated, untouched.
    return hasTime ? formatISTDateTime(value) : new Date(value).toLocaleDateString()
  }
  return String(value)
}

export default function ExcelFilesPage() {
  const [selectedSheet, setSelectedSheet] = useState(DEFAULT_SHEET)
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [generating, setGenerating] = useState(false)
  const [generateError, setGenerateError] = useState(null)

  const { data: metadata, loading: metaLoading, error: metaError, refetch: refetchMeta } = useApi('/excel-files/metadata')
  const { data: sheetsData, refetch: refetchSheets } = useApi('/excel-files/sheets')
  const {
    data: preview,
    loading: previewLoading,
    error: previewError,
    refetch: refetchPreview,
  } = useApi(`/excel-files/sheets/${encodeURIComponent(selectedSheet)}`, { page, page_size: PAGE_SIZE, search: search || undefined })

  const hasWorkbook = !metaError
  const totalRows = sheetsData ? sheetsData.sheets.reduce((sum, s) => sum + s.row_count, 0) : null

  async function handleGenerate() {
    setGenerating(true)
    setGenerateError(null)
    try {
      await generateWorkbook()
      refetchMeta()
      refetchSheets()
      refetchPreview()
    } catch (err) {
      setGenerateError(err.message)
    } finally {
      setGenerating(false)
    }
  }

  function selectSheet(name) {
    setSelectedSheet(name)
    setPage(1)
  }

  function runSearch(e) {
    e.preventDefault()
    setSearch(searchInput.trim())
    setPage(1)
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card
        title="Excel Files"
        subtitle="One consolidated workbook reflecting the current database state"
        action={
          <div style={{ display: 'flex', gap: 10 }}>
            <Button variant="secondary" icon={RefreshCw} onClick={handleGenerate} disabled={generating}>
              {generating ? 'Generating...' : 'Generate Now'}
            </Button>
            {hasWorkbook && (
              <a className="btn btn-primary" href={excelDownloadUrl()} download>
                <Download size={15} /> Download Excel
              </a>
            )}
          </div>
        }
      >
        {metaLoading && <LoadingState rows={2} />}
        {generateError && <ErrorState title="Generation failed" description={generateError} />}
        {!metaLoading && metaError && (
          <EmptyState
            icon={FileSpreadsheet}
            title="No workbook yet"
            description="Click Generate Now to build the consolidated workbook from the current database state."
          />
        )}
        {!metaLoading && !metaError && metadata && (
          <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap' }}>
            <div>
              <div className="viewer-meta-label">Workbook</div>
              <div style={{ fontWeight: 600 }}>{metadata.filename}</div>
            </div>
            <div>
              <div className="viewer-meta-label">Last Generated</div>
              <div style={{ fontWeight: 600 }}>{formatISTDateTime(metadata.last_updated)}</div>
            </div>
            <div>
              <div className="viewer-meta-label">Sheets</div>
              <div style={{ fontWeight: 600 }}>{sheetsData ? sheetsData.sheets.length : '-'}</div>
            </div>
            <div>
              <div className="viewer-meta-label">Total Rows</div>
              <div style={{ fontWeight: 600 }}>{totalRows ?? '-'}</div>
            </div>
            <div>
              <div className="viewer-meta-label">Reports / Expenses</div>
              <div style={{ fontWeight: 600 }}>
                {metadata.total_reports} / {metadata.total_expenses}
              </div>
            </div>
            <div>
              <div className="viewer-meta-label">Total Claimed</div>
              <div style={{ fontWeight: 600 }}>{formatINR(metadata.total_claimed)}</div>
            </div>
            <div>
              <div className="viewer-meta-label">Status</div>
              <StatusBadge status={metadata.status} />
            </div>
          </div>
        )}
      </Card>

      {hasWorkbook && sheetsData && (
        <Card title="Workbook Preview">
          <div className="btn-group" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
            {sheetsData.sheets.map((s) => (
              <button key={s.name} className={selectedSheet === s.name ? 'active' : ''} onClick={() => selectSheet(s.name)}>
                {s.name} ({s.row_count})
              </button>
            ))}
          </div>

          <form onSubmit={runSearch} style={{ display: 'flex', gap: 10, marginBottom: 14 }}>
            <input
              type="text"
              className="input"
              placeholder="Search this sheet..."
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              style={{ maxWidth: 320 }}
            />
            <Button variant="secondary" icon={Search} type="submit">
              Search
            </Button>
            {search && (
              <Button
                variant="secondary"
                type="button"
                onClick={() => {
                  setSearch('')
                  setSearchInput('')
                  setPage(1)
                }}
              >
                Clear
              </Button>
            )}
          </form>

          {previewLoading && <LoadingState rows={5} />}
          {previewError && <ErrorState title="Could not load sheet preview" description="The server may be unreachable." />}
          {!previewLoading && !previewError && preview && preview.rows.length === 0 && (
            <div className="state-panel" style={{ padding: '32px 16px' }}>
              <div className="state-title">No rows{search ? ' match this search' : ''}</div>
            </div>
          )}
          {!previewLoading && !previewError && preview && preview.rows.length > 0 && (
            <>
              <div className="table-wrap" style={{ overflowX: 'auto' }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      {preview.headers.map((h) => (
                        <th key={h}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {preview.rows.map((row, i) => (
                      <tr key={i}>
                        {row.map((cell, j) => (
                          <td key={j} style={{ whiteSpace: 'nowrap' }}>
                            {formatCell(cell)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 14 }}>
                <span style={{ fontSize: 12.5, color: 'var(--gray-500)' }}>
                  Page {preview.page} of {preview.total_pages} - {preview.total_rows} row(s)
                </span>
                <div className="viewer-toolbar-group">
                  <button className="icon-action" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                    <ChevronLeft size={15} />
                  </button>
                  <button className="icon-action" disabled={page >= preview.total_pages} onClick={() => setPage((p) => p + 1)}>
                    <ChevronRight size={15} />
                  </button>
                </div>
              </div>
            </>
          )}
        </Card>
      )}
    </div>
  )
}
