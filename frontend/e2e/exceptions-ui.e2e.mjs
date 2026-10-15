// Rendered-UI regression checks for the Document Viewer's Exceptions tab:
// the Exceptions / Review Required selector and the horizontally scrollable table.
//
// Not wired into package.json on purpose (no new project dependency). Run it
// against a DISPOSABLE backend + `vite` dev server that already holds an extracted
// expense report with both exceptions and review-required items:
//
//   PLAYWRIGHT_CORE=<dir containing node_modules/playwright-core> \
//   E2E_EDGE="C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" \
//   E2E_DOC=expense:1 node frontend/e2e/exceptions-ui.e2e.mjs
//
// Optional: E2E_BASE_URL (default http://localhost:5200), E2E_EMAIL, E2E_PASSWORD.
// Exits non-zero if any check fails.

import { createRequire } from 'node:module'
import path from 'node:path'

const require = createRequire(path.join(process.env.PLAYWRIGHT_CORE || process.cwd(), 'noop.js'))
const { chromium } = require('playwright-core')

const BASE = process.env.E2E_BASE_URL || 'http://localhost:5200'
const DOC = process.env.E2E_DOC || 'expense:1'
const EMAIL = process.env.E2E_EMAIL || 'intern@spincotech.com'
const PASSWORD = process.env.E2E_PASSWORD || 'ChangeMe123!'
const VIEWPORTS = [
  [1440, 900], // Electron window default
  [1280, 720],
  [1100, 700],
]

let failures = 0
function check(name, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  [${detail}]` : ''}`)
  if (!ok) failures += 1
}

const browser = await chromium.launch({
  executablePath: process.env.E2E_EDGE,
  headless: true,
  // Playwright hides scrollbars in headless mode by default; the checks below need the real ones.
  ignoreDefaultArgs: ['--hide-scrollbars'],
})

async function newPage(vw, vh, { freshContext = false } = {}) {
  // freshContext: a separate browsing context (own cache/cookies) instead of
  // reusing the shared default one - needed before page.route() mocks,
  // since a request the browser can already answer from disk/memory cache
  // never reaches the network layer at all, so an already-warm cache from an
  // earlier page in this same run can silently bypass the mock.
  const page = freshContext ? await (await browser.newContext()).newPage() : await browser.newPage({ viewport: { width: vw, height: vh } })
  if (freshContext) await page.setViewportSize({ width: vw, height: vh })
  await page.goto(`${BASE}/`)
  await page.fill('input[type=email]', EMAIL)
  await page.fill('input[type=password]', PASSWORD)
  await page.click('button[type=submit]')
  await page.waitForURL((u) => !u.pathname.startsWith('/login'))
  return page
}

async function openExceptions(page, doc = DOC) {
  await page.goto(`${BASE}/documents/${doc}`)
  await page.getByRole('button', { name: /^Exceptions/ }).first().click()
  await page.getByRole('tab', { name: /^Exceptions \(/ }).waitFor()
  await page.locator('table.data-table, .state-panel').first().waitFor()
}

const tabCount = async (page, re) => {
  const label = await page.getByRole('tab', { name: re }).innerText()
  return Number(label.match(/\((\d+)\)/)[1])
}

const rowSeverities = (page) => page.locator('#exceptions-tabpanel tbody tr td:nth-child(6)').allInnerTexts()
const rowKeys = async (page) =>
  (await page.locator('#exceptions-tabpanel tbody tr').allInnerTexts()).map((t) => t.replace(/\s+/g, ' ').trim())

// ---------------------------------------------------------------- tabs
{
  const page = await newPage(1440, 900)
  const reportId = DOC.split(':')[1]
  await openExceptions(page)

  const records = await (await page.request.get(`${BASE}/api/expense-reports/${reportId}/exceptions`)).json()
  const extraction = await (await page.request.get(`${BASE}/api/documents/${DOC}/extraction`)).json()
  const active = records.filter((r) => r.status !== 'RESOLVED')
  const apiExc = active.filter((r) => r.severity === 'EXCEPTION')
  const apiRev = active.filter((r) => r.severity === 'REVIEW REQUIRED')
  check('fixture has both exception and review-required records', apiExc.length > 0 && apiRev.length > 0, `exc=${apiExc.length} rev=${apiRev.length}`)

  // Counts: tab labels == canonical report counts == Report Status banner.
  const bannerText = await page.locator('.card', { hasText: 'Report Status' }).first().innerText()
  const bannerExc = Number(bannerText.match(/(\d+)\s*\n?\s*Exceptions/)?.[1])
  const bannerRev = Number(bannerText.match(/(\d+)\s*\n?\s*Review Required/)?.[1])
  check('Exceptions tab count == canonical exception_count', (await tabCount(page, /^Exceptions \(/)) === extraction.exception_count)
  check('Review Required tab count == canonical review_required_count', (await tabCount(page, /^Review Required \(/)) === extraction.review_required_count)
  check('tab counts == Report Status banner', bannerExc === extraction.exception_count && bannerRev === extraction.review_required_count, `banner=${bannerExc}/${bannerRev}`)

  // Exceptions view shows ONLY exception records.
  await page.getByRole('tab', { name: /^Exceptions \(/ }).click()
  check('Exceptions tab is selected (aria-selected)', (await page.getByRole('tab', { name: /^Exceptions \(/ }).getAttribute('aria-selected')) === 'true')
  const excSev = await rowSeverities(page)
  const excRows = await rowKeys(page)
  check('Exceptions view: every row badge is "Exception"', excSev.length > 0 && excSev.every((s) => s.trim() === 'Exception'), `${excSev.length} rows`)
  check('Exceptions view: row count == active EXCEPTION records', excSev.length === apiExc.length, `${excSev.length} vs ${apiExc.length}`)
  check('selected tab is visually distinct', (await page.getByRole('tab', { name: /^Exceptions \(/ }).evaluate((el) => getComputedStyle(el).backgroundColor)) !==
    (await page.getByRole('tab', { name: /^Review Required \(/ }).evaluate((el) => getComputedStyle(el).backgroundColor)))

  // Source evidence from the Exceptions tab.
  const excSource = page.locator('#exceptions-tabpanel .view-source-btn').first()
  if (await excSource.count()) {
    await excSource.click()
    // The PDF viewer mounts react-pdf/loads its worker after the tab switch -
    // not instant, so this must wait rather than check isVisible() immediately.
    const excViewerOk = await page.locator('.viewer-canvas-wrap, .viewer-toolbar').first().waitFor({ state: 'visible', timeout: 5000 }).then(() => true).catch(() => false)
    check('View Source (Exceptions tab) opens the PDF viewer', excViewerOk)
    await openExceptions(page)
  } else check('Exceptions tab has a View Source button', false)

  // Review Required view shows ONLY review-required records.
  await page.getByRole('tab', { name: /^Review Required \(/ }).click()
  check('Review Required tab is selected', (await page.getByRole('tab', { name: /^Review Required \(/ }).getAttribute('aria-selected')) === 'true')
  const revSev = await rowSeverities(page)
  const revRows = await rowKeys(page)
  check('Review view: every row badge is "Review Required"', revSev.length > 0 && revSev.every((s) => s.trim() === 'Review Required'), `${revSev.length} rows`)
  check('Review view: row count == active REVIEW REQUIRED records', revSev.length === apiRev.length, `${revSev.length} vs ${apiRev.length}`)
  check('the two views share no row', !excRows.some((r) => revRows.includes(r)))

  const revSource = page.locator('#exceptions-tabpanel .view-source-btn').first()
  if (await revSource.count()) {
    await revSource.click()
    const revViewerOk = await page.locator('.viewer-canvas-wrap, .viewer-toolbar').first().waitFor({ state: 'visible', timeout: 5000 }).then(() => true).catch(() => false)
    check('View Source (Review Required tab) opens the PDF viewer', revViewerOk)
    await openExceptions(page)
    await page.getByRole('tab', { name: /^Review Required \(/ }).click()
  } else check('Review Required tab has a View Source button', false)

  // Switching back and forth neither changes counts nor reclassifies anything.
  await page.getByRole('tab', { name: /^Exceptions \(/ }).click()
  const excAgain = await rowKeys(page)
  await page.getByRole('tab', { name: /^Review Required \(/ }).click()
  const revAgain = await rowKeys(page)
  check('switching back and forth preserves both sets', JSON.stringify(excAgain) === JSON.stringify(excRows) && JSON.stringify(revAgain) === JSON.stringify(revRows))
  check('tab counts unchanged after switching', (await tabCount(page, /^Exceptions \(/)) === extraction.exception_count && (await tabCount(page, /^Review Required \(/)) === extraction.review_required_count)
  const after = await (await page.request.get(`${BASE}/api/expense-reports/${reportId}/exceptions`)).json()
  check('switching tabs changed no underlying record', JSON.stringify(after) === JSON.stringify(records))
  await page.close()
}

// ----------------------------------------------------- empty-state behaviour
{
  const page = await newPage(1440, 900, { freshContext: true })
  const reportId = DOC.split(':')[1]
  // Same report, but with every review-required record removed from the response.
  await page.route(`**/api/expense-reports/${reportId}/exceptions`, async (route) => {
    const res = await route.fetch()
    const rows = (await res.json()).filter((r) => r.severity !== 'REVIEW REQUIRED')
    await route.fulfill({ response: res, json: rows })
  })
  // The browser URL-encodes the ':' in a document id (expense:1 -> expense%3A1),
  // so a glob built from the literal DOC string never matches the real request -
  // wildcard the id segment instead.
  await page.route(`**/api/documents/*/extraction`, async (route) => {
    const res = await route.fetch()
    await route.fulfill({ response: res, json: { ...(await res.json()), review_required_count: 0 } })
  })
  await openExceptions(page)
  await page.getByRole('tab', { name: /^Review Required \(/ }).click()
  check('empty tab shows its empty-state message', await page.getByText('Nothing needs review').isVisible())
  check('empty tab renders no table', (await page.locator('#exceptions-tabpanel table').count()) === 0)
  check('empty tab count is 0', (await tabCount(page, /^Review Required \(/)) === 0)
  await page.close()
}

// -------------------------------------------------------- horizontal scroll
for (const [vw, vh] of VIEWPORTS) {
  const page = await newPage(vw, vh)
  await openExceptions(page)
  for (const tab of [/^Exceptions \(/, /^Review Required \(/]) {
    await page.getByRole('tab', { name: tab }).click()
    const label = `${vw}x${vh} ${String(tab).includes('Review') ? 'Review' : 'Exceptions'}`
    const wrap = page.locator('#exceptions-tabpanel .table-wrap--scroll')
    if (!(await wrap.count())) { check(`${label}: scroll container present`, false); continue }
    await wrap.scrollIntoViewIfNeeded()
    const m = await wrap.evaluate((el) => {
      const r = el.getBoundingClientRect()
      return { sw: el.scrollWidth, cw: el.clientWidth, sh: el.scrollHeight, ch: el.clientHeight, bottom: r.bottom, top: r.top, bar: el.offsetHeight - el.clientHeight, ox: getComputedStyle(el).overflowX, vh: innerHeight }
    })
    check(`${label}: overflow-x is scrollable`, m.ox === 'auto' || m.ox === 'scroll', m.ox)
    check(`${label}: content wider than container (scrollWidth > clientWidth)`, m.sw > m.cw, `${m.sw} > ${m.cw}`)
    check(`${label}: horizontal scrollbar rendered`, m.bar >= 6, `${m.bar}px`)
    check(`${label}: scrollbar edge is inside the viewport`, m.bottom <= m.vh + 1, `bottom=${Math.round(m.bottom)} vh=${m.vh}`)

    // Keyboard (focusable region) and wheel both move it; the last column becomes reachable.
    // A real user focuses this region by clicking it - element.focus() alone
    // (with no prior click) was observed to set document.activeElement
    // correctly but not reliably enable native arrow-key scroll in Chromium.
    // A short settle delay after the click avoids a race between the
    // click's focus event finishing and the immediately-following keydown
    // (observed flaky without it - inconsistent per run, not per tab).
    await wrap.locator('tbody td').first().click()
    await page.waitForTimeout(400)
    await page.keyboard.press('ArrowRight')
    await page.waitForTimeout(100)
    const afterKey = await wrap.evaluate((el) => el.scrollLeft)
    check(`${label}: keyboard ArrowRight scrolls the table`, afterKey > 0, `scrollLeft=${afterKey}`)
    await wrap.evaluate((el) => { el.scrollLeft = 0 })
    const box = await wrap.boundingBox()
    await page.mouse.move(box.x + box.width / 2, box.y + Math.min(box.height / 2, 120))
    await page.mouse.wheel(400, 0)
    await page.waitForTimeout(200)
    check(`${label}: horizontal wheel scrolls the table`, (await wrap.evaluate((el) => el.scrollLeft)) > 0)

    await wrap.evaluate((el) => { el.scrollLeft = el.scrollWidth })
    const end = await wrap.evaluate((el) => {
      const w = el.getBoundingClientRect()
      const lastTh = el.querySelector('thead th:last-child').getBoundingClientRect()
      const cells = [...el.querySelectorAll('tbody tr td:last-child')]
      const srcCells = [...el.querySelectorAll('tbody tr td:nth-last-child(2)')]
      const inside = (r) => r.left >= w.left - 1 && r.right <= w.right + 1
      const btns = [...el.querySelectorAll('tbody button')].map((b) => b.getBoundingClientRect())
      const tds = [...el.querySelectorAll('tbody td')].map((c) => ({ r: c.getBoundingClientRect(), b: c.querySelector('button')?.getBoundingClientRect() }))
      const overlap = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom
      let overlaps = 0
      for (let i = 0; i < btns.length; i++) for (let j = i + 1; j < btns.length; j++) if (overlap(btns[i], btns[j])) overlaps++
      const clipped = tds.filter((t) => t.b && (t.b.right > t.r.right + 1 || t.b.left < t.r.left - 1)).length
      return { lastThInside: inside(lastTh), lastCellsInside: cells.every((c) => inside(c.getBoundingClientRect())), srcInside: srcCells.every((c) => inside(c.getBoundingClientRect())), overlaps, clipped, scrolledToEnd: Math.abs(el.scrollLeft + el.clientWidth - el.scrollWidth) <= 1 }
    })
    check(`${label}: scrolled to end`, end.scrolledToEnd)
    check(`${label}: final (Status/action) column fully reachable`, end.lastThInside && end.lastCellsInside)
    check(`${label}: Source column fully reachable`, end.srcInside)
    check(`${label}: no overlapping action buttons`, end.overlaps === 0)
    check(`${label}: no button clipped by its cell`, end.clipped === 0)

    // Vertical scrolling: inner container when rows overflow it, otherwise the page.
    const v = await wrap.evaluate((el) => {
      const before = el.scrollTop
      el.scrollTop = el.scrollHeight
      return { canScroll: el.scrollHeight > el.clientHeight, moved: el.scrollTop > before }
    })
    check(`${label}: vertical scrolling works`, !v.canScroll || v.moved)
    check(`${label}: header stays aligned with cells`, await wrap.evaluate((el) => {
      const ths = [...el.querySelectorAll('thead th')].map((t) => Math.round(t.getBoundingClientRect().left))
      const tds = [...el.querySelectorAll('tbody tr:first-child td')].map((t) => Math.round(t.getBoundingClientRect().left))
      return ths.length === tds.length && ths.every((x, i) => Math.abs(x - tds[i]) <= 1)
    }))
  }
  // The page itself must never grow a horizontal scrollbar because of the table.
  check(`${vw}x${vh}: page has no horizontal overflow`, (await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)))
  await page.close()
}

// ------------------------------------------- other pages that share the table
{
  const page = await newPage(1280, 720)
  const routes = ['/upload', '/expense-reports', '/trip-reports', '/excel-files', '/analytics', '/audit-logs', '/users-roles', '/policy-master', '/settings']
  for (const route of routes) {
    await page.goto(`${BASE}${route}`)
    await page.waitForTimeout(900)
    const r = await page.evaluate(() => ({
      pageOverflow: document.documentElement.scrollWidth > innerWidth + 1,
      wraps: [...document.querySelectorAll('.table-wrap')].map((w) => ({ sw: w.scrollWidth, cw: w.clientWidth, right: w.getBoundingClientRect().right, ox: getComputedStyle(w).overflowX })),
      rendered: document.querySelector('.page-content')?.innerText.length > 20,
    }))
    check(`shared tables ${route}: renders, no page-level horizontal overflow, every .table-wrap contained`, r.rendered && !r.pageOverflow && r.wraps.every((w) => w.right <= 1280 + 1 && (w.sw <= w.cw || w.ox !== 'visible')), `${r.wraps.length} table(s)`)
  }
  await page.close()
}

await browser.close()
console.log(failures ? `\n${failures} CHECK(S) FAILED` : '\nALL CHECKS PASSED')
process.exit(failures ? 1 : 0)
