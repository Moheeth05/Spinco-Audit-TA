"""Browser smoke test of the offline review tool (a review pack's review.html): status changes clear values, invalid exports are blocked with a message,
a valid export downloads, and no network request is made.
Uses the Playwright already present in backend/.venv with the installed Edge/Chrome. Run:  python tests/review_tool_smoke.py <path to review.html>
Point it at a SYNTHETIC pack (see tests/review_ui_export.test.mjs / tests/synthetic_e2e.test.mjs); it drives the first receipt only.
The exported JSON is written to a temp folder only - it is a TEST export (reviewer 'SMOKE-TEST'), is never applied to the corpus, and verifies nothing."""
import json, sys, tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

html = Path(sys.argv[1]).resolve()
out = Path(tempfile.mkdtemp(prefix="fuel-review-smoke-"))
requests, dialogs = [], []
checks = {}
with sync_playwright() as p:
    for channel in ("msedge", "chrome", None):
        try:
            browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
            break
        except Exception as e:  # noqa: BLE001
            last = e
    else:
        print("SKIP: no browser available:", str(last)[:120]); sys.exit(0)
    ctx = browser.new_context(accept_downloads=True)
    ctx.on("request", lambda r: requests.append(r.url) if not r.url.startswith(("file:", "data:", "blob:", "edge:", "chrome:", "about:")) else None)  # browser-internal UI urls are not page requests
    page = ctx.new_page()
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))  # "N receipts incomplete - export anyway?" (accepted); blocked exports must raise none
    page.goto(html.as_uri())
    page.wait_for_selector("#img")
    img_ok = page.evaluate("document.getElementById('img').complete && document.getElementById('img').naturalWidth > 0")
    meta = page.inner_text("#meta")

    def stored():
        """The review state saved by the page (localStorage) for the first receipt, or None if storage is unavailable."""
        try:
            return page.evaluate("(function(){var k=Object.keys(localStorage).filter(function(x){return x.indexOf('fuel-review-state-v1-')===0})[0];var s=JSON.parse(localStorage.getItem(k)||'{}');return s[Object.keys(s)[0]]||null})()")
        except Exception:  # noqa: BLE001
            return None

    def blocked_export():
        """Click Export and report whether it was blocked (no download), plus the message shown."""
        try:
            with page.expect_download(timeout=1500):
                page.click("#export")
            return False, ""
        except PWTimeout:
            return True, (page.inner_text("#errors") if page.is_visible("#errors") else "")

    page.fill("#rid", "SMOKE-TEST"); page.check("#auth")

    # ---- pump: read -> illegible / not_printed clear the value, back to read accepts a new value
    page.check("input[name=p_status][value=read]"); page.fill("#p_value", "STALE DEALER"); page.fill("#p_other", "ALT A, ALT B"); page.fill("#p_printed", "PRINTED HEADER")
    page.check("input[name=p_status][value=illegible]")
    checks["read -> illegible clears the pump value"] = page.input_value("#p_value") == "" and page.input_value("#p_other") == ""
    checks["printed-text box is kept when a field becomes illegible"] = page.input_value("#p_printed") == "PRINTED HEADER"
    st = stored()
    checks["saved state matches the screen (pump value cleared in storage)"] = st is None or (st["pump_name"]["status"] == "illegible" and st["pump_name"]["value"] is None)
    page.check("input[name=p_status][value=read]"); page.fill("#p_value", "SECOND STALE")
    page.check("input[name=p_status][value=not_printed]")
    checks["read -> not_printed clears the pump value"] = page.input_value("#p_value") == ""
    page.check("input[name=p_status][value=read]")
    checks["switching back to read starts from an empty value"] = page.input_value("#p_value") == ""
    page.fill("#p_value", "TEST DEALER")
    checks["a new valid value can be entered after switching back"] = page.input_value("#p_value") == "TEST DEALER"

    # ---- date
    page.check("input[name=d_status][value=read]"); page.fill("#d_printed", "24/09/26"); page.fill("#d_iso", "2026-09-24"); page.check("#d_provable"); page.select_option("#d_evidence", "component_gt_12")
    page.check("input[name=d_status][value=illegible]")
    checks["read -> illegible clears the date, provability tick and evidence"] = page.input_value("#d_iso") == "" and not page.is_checked("#d_provable") and page.input_value("#d_evidence") == "" and page.input_value("#d_printed") == "24/09/26"
    page.check("input[name=d_status][value=read]"); page.fill("#d_iso", "2026-09-24"); page.check("#d_provable"); page.select_option("#d_evidence", "component_gt_12")

    # ---- amount
    page.check("input[name=a_status][value=read]"); page.fill("#a_printed", "999.00"); page.fill("#a_value", "999")
    page.check("input[name=a_status][value=illegible]")
    checks["read -> illegible clears the amount"] = page.input_value("#a_value") == "" and page.input_value("#a_printed") == "999.00"
    page.check("input[name=a_status][value=read]"); page.fill("#a_printed", "1234.50"); page.fill("#a_value", "1234.5")

    # ---- invalid exports are blocked, with a visible message, and nothing is downloaded
    n_dialogs = len(dialogs)
    page.fill("#p_value", "")  # status read with no value
    was_blocked, msg = blocked_export()
    checks["export blocked: status read with an empty pump value"] = was_blocked and "pump_name" in msg and "Export blocked" in msg and len(dialogs) == n_dialogs
    page.fill("#p_value", "TEST DEALER")
    page.fill("#d_iso", "")  # status read with no (valid) date
    was_blocked, msg = blocked_export()
    checks["export blocked: status read with no calendar date"] = was_blocked and "transaction_date" in msg and "calendar date" in msg
    page.fill("#d_iso", "2026-09-24")
    page.check("input[name=a_status][value=illegible]"); page.fill("#a_value", "55")  # typed a value onto an illegible field
    was_blocked, msg = blocked_export()
    checks["export blocked: a value typed onto an illegible field"] = was_blocked and "total_amount" in msg and "contradicts" in msg
    page.check("input[name=a_status][value=read]"); page.fill("#a_value", "1234.5")
    page.fill("#rid", "x")
    was_blocked, msg = blocked_export()
    checks["export blocked: malformed reviewer id"] = was_blocked and "reviewer_id" in msg
    page.fill("#rid", "SMOKE-TEST")
    page.uncheck("#auth")
    was_blocked, msg = blocked_export()
    checks["export blocked: authorization not ticked"] = was_blocked and "authorization" in msg
    page.check("#auth")

    # ---- a valid export downloads
    with page.expect_download() as dl:
        page.click("#export")
    f = out / "export.json"; dl.value.save_as(str(f))
    checks["error box is hidden after a valid export"] = not page.is_visible("#errors")
    data = json.loads(f.read_text(encoding="utf-8"))
    first_id = next(iter(data["records"]))
    rec = data["records"][first_id]
    browser.close()
checks["image loads"] = img_ok
checks["blind: provisional labels hidden"] = "blind" in meta and "PROVISIONAL labels shown" not in meta
checks["export schema"] = data["schema"] == "fuel-review-export/v1" and data["reviewer_id"] == "SMOKE-TEST" and data["authorization_confirmed"] is True and data["blind"] is True
checks["only complete receipts exported"] = list(data["records"]) == [first_id]
checks["values round-trip"] = rec["pump_name"]["value"] == "TEST DEALER" and rec["transaction_date"]["iso_date"] == "2026-09-24" and rec["total_amount"]["value"] == 1234.5 and rec["transaction_date"]["order_provable_from_receipt"] is True
checks["NO network request"] = requests == []
for k, v in checks.items():
    print(("PASS " if v else "FAIL ") + k)
print("non-file requests seen:", requests)
sys.exit(0 if all(checks.values()) else 1)
