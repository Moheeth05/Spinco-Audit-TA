"""Drives the PACKAGED Spinco TA Audit app (launched separately with --user-data-dir and
--remote-debugging-port on disposable data) over CDP and records evidence.

Usage: python drive_packaged.py <cdp_port> <evidence_dir> <userdata_dir> <real_baseline_json> <fixtures_dir> <approved_er_pdf>

Everything here talks only to the app's own loopback backend. The real %APPDATA% data folder is
compared against a baseline listing taken before launch, both before any upload and at the end.
"""
import json, os, re, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

CDP_PORT, EVID, USERDATA, REAL_BASELINE, FIXTURES, APPROVED_ER = sys.argv[1:7]
EVID = Path(EVID); EVID.mkdir(parents=True, exist_ok=True)
EMAIL, PASSWORD = "intern@spincotech.com", "ChangeMe123!"
ev = {"steps": [], "checks": {}, "fixtures": {}}


def step(name, **kw):
    ev["steps"].append({"step": name, **kw})
    print(f"[{time.strftime('%H:%M:%S')}] {name} {kw if kw else ''}", flush=True)


def check(name, ok, detail=""):
    ev["checks"][name] = {"ok": bool(ok), "detail": detail}
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def real_data_matches_baseline():
    real = os.path.join(os.environ["APPDATA"], "Spinco TA Audit")
    base = json.loads(Path(REAL_BASELINE).read_text(encoding="utf-8-sig"))
    want = {}
    for b in base:
        s = re.sub(r"(\.\d{6})\d*Z$", r"\1+00:00", b["mtime"])
        want[b["path"].lstrip("\\")] = (b["bytes"], datetime.fromisoformat(s).timestamp())
    have = {}
    for root, _, files in os.walk(real):
        for f in files:
            full = os.path.join(root, f)
            st = os.stat(full)
            have[os.path.relpath(full, real)] = (st.st_size, st.st_mtime)
    changed = [k for k in want if k not in have or have[k][0] != want[k][0] or abs(have[k][1] - want[k][1]) > 0.01]
    added = [k for k in have if k not in want]
    return not changed and not added, f"baseline={len(want)} now={len(have)} changed={len(changed)} added={len(added)}"


def save():
    (EVID / "evidence.json").write_text(json.dumps(ev, indent=2, default=str), encoding="utf-8")


manifest = json.loads((Path(FIXTURES) / "fixtures.json").read_text(encoding="utf-8"))
UPLOADS = [(Path(FIXTURES) / m["file"]) for m in manifest.values()] + [Path(APPROVED_ER)]

# The splash page navigates to the backend a few seconds after DevTools opens; a Playwright connection made
# while the splash is showing can keep reporting the stale data: URL. Wait (over plain HTTP) until DevTools
# itself lists a page on the backend, and only then attach.
import urllib.request
_t = time.time()
while time.time() - _t < 150:
    try:
        listing = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=3).read())
        if any(x.get("type") == "page" and x.get("url", "").startswith("http://127.0.0.1") for x in listing):
            break
    except Exception:
        pass
    time.sleep(2)

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
    ctx = browser.contexts[0]
    t0, page = time.time(), None
    while time.time() - t0 < 90 and page is None:
        for pg in ctx.pages:
            if pg.url.startswith("http://127.0.0.1"):
                page = pg
        time.sleep(1)
    check("electron window loaded the backend-served app", page is not None,
          page.url if page else f"no window reached 127.0.0.1; contexts={len(browser.contexts)} pages={[pg.url for c in browser.contexts for pg in c.pages]}")
    if page is None:
        save(); sys.exit(1)
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.set_viewport_size({"width": 1280, "height": 720})

    # ---- isolation gate: before any login/upload
    in_disposable = Path(USERDATA, "database", "spinco_ta.db").exists()
    ok_real, detail_real = real_data_matches_baseline()
    check("app data lives in the disposable user-data folder", in_disposable, USERDATA)
    check("real %APPDATA%\\Spinco TA Audit untouched (before uploads)", ok_real, detail_real)
    if not (in_disposable and ok_real):
        save(); print("ABORTING: isolation gate failed"); sys.exit(2)

    seen_hosts = {}
    def _on_request(req):
        h = urlsplit(req.url).hostname or "(none)"
        seen_hosts[h] = seen_hosts.get(h, 0) + 1
    ctx.on("request", _on_request)
    page.reload(); page.wait_for_load_state("load"); page.wait_for_timeout(1500)

    r = ctx.request.get(f"{base}/api/health")
    check("backend health check", r.ok and r.json().get("status") == "ok", str(r.status))

    page.wait_for_selector('input[type="email"]', timeout=30000)
    page.fill('input[type="email"]', EMAIL); page.fill('input[type="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_selector("text=Upload", timeout=30000)
    me = ctx.request.get(f"{base}/api/auth/me")
    check("login works through the real UI", me.ok and me.json()["user"]["email"] == EMAIL)

    # ---- upload all documents in one UI action (background processing)
    page.click("text=Upload Files")
    page.wait_for_selector('input[type="file"]', state="attached", timeout=15000)
    t_up = time.time()
    with page.expect_response(lambda resp: "/api/uploads/expense-reports" in resp.url and resp.request.method == "POST", timeout=300000) as ri:
        page.set_input_files('input[type="file"]', [str(u) for u in UPLOADS])
    up = ri.value  # the UI navigates away at once, so the body is no longer readable: use status, then the list API
    check("upload request returns promptly (background processing)", up.ok and time.time() - t_up < 60, f"{time.time()-t_up:.1f}s, HTTP {up.status}, {len(UPLOADS)} files")
    docs, _t = {}, time.time()
    while time.time() - _t < 180 and len(docs) < len(UPLOADS):
        for item in ctx.request.get(f"{base}/api/expense-reports").json():
            stem = Path(item["original_filename"]).stem
            if stem in {u.stem for u in UPLOADS}:
                docs[stem] = (item["document_id"], int(item["document_id"].split(":")[1]))
        time.sleep(2)
    check("every uploaded file became an Expense Report record", len(docs) == len(UPLOADS), f"{sorted(docs)}")
    if len(docs) != len(UPLOADS):
        save(); sys.exit(1)

    deadline, status = time.time() + 1500, {}
    while time.time() < deadline:
        for name, (doc_id, _) in docs.items():
            if status.get(name) in ("EXTRACTED", "REVIEW_REQUIRED", "FAILED"):
                continue
            status[name] = ctx.request.get(f"{base}/api/documents/{doc_id}/extraction").json().get("extraction_status")
        if all(v in ("EXTRACTED", "REVIEW_REQUIRED", "FAILED") for v in status.values()) and len(status) == len(docs):
            break
        time.sleep(4)
    check("background processing reached a terminal state for every document",
          all(status.get(n) in ("EXTRACTED", "REVIEW_REQUIRED") for n in docs), f"{status} after {time.time()-t_up:.0f}s")
    page.reload(); page.wait_for_timeout(1500)
    page.screenshot(path=str(EVID / "ui_upload_done.png"))

    def fetch(name):
        rid = docs[name][1]
        exps = ctx.request.get(f"{base}/api/expense-reports/{rid}/expenses").json()
        excs = ctx.request.get(f"{base}/api/expense-reports/{rid}/exceptions").json()
        bills = ctx.request.get(f"{base}/api/expense-reports/{rid}/bills").json()
        by_id = {e["id"]: e for e in exps}
        rows = {}
        for e in exps:
            mine = [x for x in excs if x.get("expense_id") == e["id"] and x.get("status") != "RESOLVED"]
            rows[e["s_no"]] = {"match": e["bill_match_status"], "valid": e["validation_status"], "claimed": e["claimed_amount"],
                               "date": e["expense_date"], "exc": sorted(f"{x['exception_type']}[{x['severity'][:3]}]" for x in mine),
                               "reasons": " | ".join(x.get("reason") or "" for x in mine)}
        return rows, excs, bills

    def verified(row):
        return row["valid"] == "PASS"

    # ================= behaviours =================
    rows, excs, bills = fetch("pf_ambiguous_date")
    ev["fixtures"]["pf_ambiguous_date"] = {"rows": rows, "bills": [(b["page_number"], b["amount"], b["bill_date"], b["bill_date_raw"], b["bill_date_candidates"], b["bill_date_method"]) for b in bills]}
    amb = [b for b in bills if b.get("bill_date_candidates") and "|" in b["bill_date_candidates"]]
    check("ambiguous date: kept unresolved with both candidates and the raw string", len(amb) == 1 and amb[0]["bill_date"] is None and amb[0]["bill_date_raw"], f"{[(b['bill_date_raw'], b['bill_date_candidates'], b['bill_date']) for b in amb]}")
    check("ambiguous date: claim goes to review, not verified, no false DATE MISMATCH",
          not verified(rows[1]) and not any("DATE MISMATCH" in x for x in rows[1]["exc"]) and any("REVIEW" in x or "[REV]" in x for x in rows[1]["exc"]), f"{rows[1]}")
    check("ambiguous date: review text says the coincidence is not evidence", "not evidence" in rows[1]["reasons"] or "ambiguous" in rows[1]["reasons"], rows[1]["reasons"][:160])

    rows, excs, bills = fetch("pf_unreadable_amount")
    ev["fixtures"]["pf_unreadable_amount"] = {"rows": rows}
    check("unreadable amount: candidate only - not MATCHED, not verified", rows[1]["match"] == "REVIEW_REQUIRED" and not verified(rows[1]), f"{rows[1]['match']}/{rows[1]['valid']}")
    check("unreadable amount: raises a visible review item naming the amount", any("BILL MATCH REVIEW REQUIRED" in x for x in rows[1]["exc"]) and "amount" in rows[1]["reasons"].lower(), rows[1]["reasons"][:160])
    check("unreadable amount: a readable bill in the same report still verifies (control)", rows[2]["match"] == "MATCHED" and verified(rows[2]), f"{rows[2]['match']}/{rows[2]['valid']}")

    rows, excs, bills = fetch("pf_shared_bill")
    ev["fixtures"]["pf_shared_bill"] = {"rows": rows}
    check("shared bill: no competing claim is verified", all(not verified(r_) for r_ in rows.values()), f"{[(k, v['valid']) for k, v in rows.items()]}")
    check("shared bill: reuse stays visible on every claim", all(any(t.startswith("DUPLICATE BILL") for t in r_["exc"]) for r_ in rows.values()), f"{[(k, v['exc']) for k, v in rows.items()]}")

    rows, excs, bills = fetch("pf_repeated_amounts")
    ev["fixtures"]["pf_repeated_amounts"] = {"rows": rows}
    check("repeated amounts on different dates: each claim verified by its own bill, none flagged duplicate",
          all(r_["match"] == "MATCHED" and verified(r_) for r_ in rows.values()) and not any("DUPLICATE" in t for r_ in rows.values() for t in r_["exc"]), f"{[(k, v['match'], v['valid'], v['exc']) for k, v in rows.items()]}")

    rows, excs, bills = fetch("pf_duplicate_screenshot")
    ev["fixtures"]["pf_duplicate_screenshot"] = {"rows": rows}
    check("reused screenshot: first claim verified once", verified(rows[1]), f"{rows[1]['match']}/{rows[1]['valid']}")
    check("reused screenshot: second claim flagged as duplicate and not verified", not verified(rows[2]) and any("DUPLICATE BILL" in t for t in rows[2]["exc"]), f"{rows[2]}")

    rows, excs, bills = fetch("pf_flight_vs_lodging")
    ev["fixtures"]["pf_flight_vs_lodging"] = {"rows": rows}
    check("flight claimed as lodging: CATEGORY MISMATCH, claim category unchanged", any("CATEGORY MISMATCH" in t for t in rows[1]["exc"]) and not verified(rows[1]), f"{rows[1]}")
    check("flight claimed as lodging: expense category not rewritten", ctx.request.get(f"{base}/api/expense-reports/{docs['pf_flight_vs_lodging'][1]}/expenses").json()[0]["category"] == "LODGING")

    rows, excs, bills = fetch("pf_upi_status")
    ev["fixtures"]["pf_upi_status"] = {"rows": rows, "bills": [(b["page_number"], b["amount"], b["payment_status"], b["transaction_time"], b["transaction_reference"]) for b in bills]}
    check("UPI: completed payment verifies; pending and failed never do", verified(rows[1]) and not verified(rows[2]) and not verified(rows[3]), f"{[(k, v['valid']) for k, v in rows.items()]}")
    check("UPI: status, time and reference were read", {b["payment_status"] for b in bills if b["payment_status"]} >= {"COMPLETED", "PENDING", "FAILED"} and any(b["transaction_time"] for b in bills), f"{ev['fixtures']['pf_upi_status']['bills']}")

    rows, excs, bills = fetch("pf_same_day_times")
    ev["fixtures"]["pf_same_day_times"] = {"rows": rows}
    check("same amount and day, different times: two separate payments, both verified, no duplicate", all(verified(r_) for r_ in rows.values()) and not any("DUPLICATE" in t for r_ in rows.values() for t in r_["exc"]), f"{[(k, v['match'], v['valid'], v['exc']) for k, v in rows.items()]}")

    rows, excs, bills = fetch("pf_ocr_pages")
    ev["fixtures"]["pf_ocr_pages"] = {"rows": rows}
    check("packaged OCR: picture-only receipts are read and verify both claims", all(r_["match"] == "MATCHED" and verified(r_) for r_ in rows.values()), f"{[(k, v['match'], v['valid']) for k, v in rows.items()]}; bills={[(b['page_number'], b['amount'], b['bill_date']) for b in bills]}")

    rows, excs, bills = fetch(Path(APPROVED_ER).stem)
    ev["fixtures"]["approved_ER"] = {"rows": rows, "bill_count": len(bills), "with_amount": sum(1 for b in bills if b["amount"] is not None)}
    check("approved ER-39832: OCR produced bills with readable amounts", len(bills) >= 10 and ev["fixtures"]["approved_ER"]["with_amount"] >= 3, f"{len(bills)} bills / {ev['fixtures']['approved_ER']['with_amount']} with amount")
    check("approved ER-39832: no DUPLICATE BILL false positive", not any("DUPLICATE BILL[" in t for r_ in rows.values() for t in r_["exc"]))
    check("approved ER-39832: S.No 9 owns its exact-date bill; S.No 5 is not given it", rows[9]["match"] == "MATCHED" and rows[5]["match"] != "MATCHED", f"S9={rows[9]['match']} S5={rows[5]['match']}")

    # ================= UI: Exceptions / Review Required tabs and horizontal scrolling =================
    def ui_tabs(name, label):
        doc_id = docs[name][0]
        canon = ctx.request.get(f"{base}/api/documents/{doc_id}/extraction").json()
        page.goto(f"{base}/documents/{doc_id}")
        page.get_by_role("button", name=re.compile(r"^Exceptions")).first.click()
        page.get_by_role("tab", name=re.compile(r"^Exceptions \(")).wait_for(timeout=30000)
        page.wait_for_selector("table.data-table, .state-panel", timeout=30000)
        def tab_count(rx):
            return int(re.search(r"\((\d+)\)", page.get_by_role("tab", name=re.compile(rx)).inner_text()).group(1))
        exc_n, rev_n = tab_count(r"^Exceptions \("), tab_count(r"^Review Required \(")
        check(f"{label}: Exceptions tab count equals canonical", exc_n == canon["exception_count"], f"tab={exc_n} canonical={canon['exception_count']}")
        check(f"{label}: Review Required tab count equals canonical", rev_n == canon["review_required_count"], f"tab={rev_n} canonical={canon['review_required_count']}")
        excs_ = ctx.request.get(f"{base}/api/expense-reports/{docs[name][1]}/exceptions").json()
        act = [e for e in excs_ if e["status"] != "RESOLVED"]
        for tab_rx, sev, shown in ((r"^Exceptions \(", "EXCEPTION", "Exception"), (r"^Review Required \(", "REVIEW REQUIRED", "Review Required")):
            page.get_by_role("tab", name=re.compile(tab_rx)).click()
            col = page.locator("#exceptions-tabpanel tbody tr td:nth-child(6)").all_inner_texts()
            want = [e for e in act if e["severity"] == sev]
            check(f"{label}: {shown} tab lists only {shown.lower()} records", len(col) == len(want) and all(c.strip() == shown for c in col), f"{len(col)} rows vs {len(want)} api")
            wrap = page.locator("#exceptions-tabpanel .table-wrap--scroll")
            if wrap.count():
                m = wrap.evaluate("el => ({sw: el.scrollWidth, cw: el.clientWidth, ox: getComputedStyle(el).overflowX})")
                wrap.evaluate("el => { el.scrollLeft = el.scrollWidth }")
                end = wrap.evaluate("""el => { const w = el.getBoundingClientRect(); const th = el.querySelector('thead th:last-child').getBoundingClientRect();
                    return {atEnd: Math.abs(el.scrollLeft + el.clientWidth - el.scrollWidth) <= 1, lastInside: th.left >= w.left - 1 && th.right <= w.right + 1} }""")
                check(f"{label}: {shown} table scrolls horizontally to the last column", m["sw"] > m["cw"] and m["ox"] in ("auto", "scroll") and end["atEnd"] and end["lastInside"], f"scrollWidth={m['sw']} clientWidth={m['cw']}")
            else:
                # An empty tab has no table to scroll. That is correct ONLY if the API also has no such records.
                check(f"{label}: {shown} tab is empty because there are no {shown.lower()} records", len(want) == 0 and len(col) == 0,
                      f"api records={len(want)} rows shown={len(col)}; panel text={page.inner_text('#exceptions-tabpanel')[:80]!r}")
        return page.inner_text("#exceptions-tabpanel")

    shared_txt = ui_tabs("pf_shared_bill", "UI shared-bill report")
    check("UI shows the duplicate/reuse review item", "DUPLICATE BILL REVIEW REQUIRED" in shared_txt.upper(), shared_txt[:120].replace("\n", " "))
    page.screenshot(path=str(EVID / "ui_review_shared_bill.png"))
    ui_tabs(Path(APPROVED_ER).stem, "UI ER-39832")
    page.screenshot(path=str(EVID / "ui_exceptions_er39832.png"))
    check("page itself has no horizontal overflow", page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))

    ev["renderer_request_hosts"] = seen_hosts
    non_loop = sorted(h for h in seen_hosts if h not in ("127.0.0.1", "localhost", "(none)"))
    check("renderer made no request to any non-loopback host", not non_loop, f"hosts={seen_hosts}")
    ok_real2, detail_real2 = real_data_matches_baseline()
    check("real %APPDATA%\\Spinco TA Audit untouched (after all uploads)", ok_real2, detail_real2)
    save()
    failed = [k for k, v in ev["checks"].items() if not v["ok"]]
    print(f"\n{len(ev['checks']) - len(failed)}/{len(ev['checks'])} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    browser.close()
    sys.exit(1 if failed else 0)
