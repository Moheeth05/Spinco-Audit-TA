"""Uploads one unreadable-page PDF through the packaged app's UI and waits for processing. Usage: <cdp_port> <pdf> <evidence_dir>"""
import json, sys, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

PORT, PDF, EVID = sys.argv[1], sys.argv[2], Path(sys.argv[3])
t = time.time()
while time.time() - t < 150:
    try:
        if any(x.get("type") == "page" and x.get("url", "").startswith("http://127.0.0.1") for x in json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=3).read())):
            break
    except Exception:
        pass
    time.sleep(2)
with sync_playwright() as p:
    b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    ctx = b.contexts[0]
    page = next(pg for pg in ctx.pages if pg.url.startswith("http://127.0.0.1"))
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.wait_for_selector('input[type="email"]', timeout=30000)
    page.fill('input[type="email"]', "intern@spincotech.com"); page.fill('input[type="password"]', "ChangeMe123!")
    page.click('button[type="submit"]'); page.wait_for_selector("text=Upload", timeout=30000)
    page.click("text=Upload Files"); page.wait_for_selector('input[type="file"]', state="attached", timeout=15000)
    page.set_input_files('input[type="file"]', PDF)
    doc_id, t0 = None, time.time()
    while time.time() - t0 < 120 and doc_id is None:
        for it in ctx.request.get(f"{base}/api/expense-reports").json():
            if it["original_filename"] == Path(PDF).name:
                doc_id = it["document_id"]
        time.sleep(2)
    status = None
    while time.time() - t0 < 600:
        status = ctx.request.get(f"{base}/api/documents/{doc_id}/extraction").json().get("extraction_status")
        if status in ("EXTRACTED", "REVIEW_REQUIRED", "FAILED"):
            break
        time.sleep(3)
    print(f"weak-OCR document processed through the packaged app: status={status} after {time.time()-t0:.0f}s")
    (EVID / "weak_probe.json").write_text(json.dumps({"doc_id": doc_id, "status": status}))
    b.close()
