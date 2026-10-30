import { Search, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { apiGet } from '../../api/client'

const KIND_LABELS = {
  EXPENSE_REPORT: 'Expense Report',
  TRIP_REPORT: 'Trip Report',
  POLICY_CATEGORY: 'Policy Category',
  MERCHANT: 'Merchant',
  EXCEPTION_TYPE: 'Exception Type',
}

export function SearchBox() {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const containerRef = useRef(null)
  const inputRef = useRef(null)
  const navigate = useNavigate()

  useEffect(() => {
    if (!open) return undefined
    function onClickOutside(e) {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false)
      }
    }
    function onKeyDown(e) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onClickOutside)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onClickOutside)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  useEffect(() => {
    if (open) inputRef.current?.focus()
  }, [open])

  // The search status (results / loading / error) is reset in updateQuery(),
  // the single place that changes the query, not in this effect: it only
  // schedules the debounced request.
  useEffect(() => {
    const trimmed = query.trim()
    if (trimmed.length < 2) return undefined
    const timer = setTimeout(() => {
      apiGet('/search', { q: trimmed })
        .then((res) => setResults(res.results || []))
        .catch((err) => setError(err))
        .finally(() => setLoading(false))
    }, 250)
    return () => clearTimeout(timer)
  }, [query])

  function updateQuery(next) {
    setQuery(next)
    if (next.trim().length < 2) {
      setResults([])
      setLoading(false)
      setError(null)
    } else {
      setLoading(true)
      setError(null)
    }
  }

  function goTo(item) {
    setOpen(false)
    updateQuery('')
    if (item.document_id) {
      navigate(`/documents/${item.document_id}`)
    } else if (item.path) {
      navigate(item.path)
    }
  }

  const trimmed = query.trim()
  const showDropdown = open && trimmed.length >= 2

  return (
    <div className="search-box" ref={containerRef}>
      {!open && (
        <button className="icon-button" aria-label="Search" onClick={() => setOpen(true)}>
          <Search size={16} />
        </button>
      )}
      {open && (
        <div className="search-box-input-wrap">
          <Search size={15} className="search-box-icon" />
          <input
            ref={inputRef}
            className="search-box-input"
            type="text"
            placeholder="Search reports, employees, categories, merchants..."
            value={query}
            onChange={(e) => updateQuery(e.target.value)}
          />
          <button
            className="search-box-close"
            aria-label="Close search"
            onClick={() => {
              setOpen(false)
              updateQuery('')
            }}
          >
            <X size={14} />
          </button>
          {showDropdown && (
            <div className="search-box-dropdown">
              {loading && <div className="search-box-status">Searching...</div>}
              {!loading && error && <div className="search-box-status search-box-error">Search failed. The server may be unreachable.</div>}
              {!loading && !error && results.length === 0 && (
                <div className="search-box-status">No matches for "{trimmed}"</div>
              )}
              {!loading &&
                !error &&
                results.map((item, idx) => (
                  <button key={`${item.kind}-${item.document_id || item.path}-${idx}`} className="search-box-result" onClick={() => goTo(item)}>
                    <span className="search-box-result-kind">{KIND_LABELS[item.kind] || item.kind}</span>
                    <span className="search-box-result-title">{item.title}</span>
                    {item.subtitle && <span className="search-box-result-subtitle">{item.subtitle}</span>}
                  </button>
                ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
