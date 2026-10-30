"""POSITIVE CONTROL for the provider check: run the packaged backend exe DIRECTLY (bypassing electron/main.js, which is what
forces the providers off) with hostile provider settings pointed at the loopback mock, upload a page that local OCR
cannot read, and report how many requests reached the mock. A non-zero count proves the mock can see a call; the packaged
app run with the same file must then show zero.

Usage: python provider_control.py <backend_exe> <weak_pdf> <workdir> <mock_log>   (mock must already be running on 58803)
"""
import os, subprocess, sys, time
from pathlib import Path

import httpx

EXE, PDF, WORK, MOCK_LOG = sys.argv[1:5]
work = Path(WORK); work.mkdir(parents=True, exist_ok=True)
port = 58811
env = dict(os.environ)
env.update({
    "SPINCO_DB_PATH": str(work / "ctl.db"), "SPINCO_STORAGE_ROOT": str(work / "docs"), "SPINCO_BACKEND_PORT": str(port),
    "SPINCO_FRONTEND_DIST": str(work / "nofrontend"), "SPINCO_COOKIE_SECURE": "false",
    "SPINCO_TESSERACT_DIR": r"D:\Bills\resources\tesseract",
    "SPINCO_NVIDIA_ENABLED": "true", "SPINCO_NVIDIA_API_KEY": "dummy-not-a-credential",
    "SPINCO_NVIDIA_OCR_ENDPOINT": "http://127.0.0.1:58803/ocr", "SPINCO_NVIDIA_BASE_URL": "http://127.0.0.1:58803/v1",
    "SPINCO_DEEPSEEK_ENABLED": "true", "SPINCO_DEEPSEEK_API_KEY": "dummy-not-a-credential", "SPINCO_DEEPSEEK_BASE_URL": "http://127.0.0.1:58803/v1",
})
env.pop("ELECTRON_RUN_AS_NODE", None)
proc = subprocess.Popen([EXE], env=env, stdout=open(work / "backend_stdout.log", "w"), stderr=subprocess.STDOUT)
try:
    base = f"http://127.0.0.1:{port}"
    for _ in range(90):
        try:
            if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(1)
    with httpx.Client(base_url=base, timeout=60) as c:
        login = c.post("/api/auth/login", json={"email": "intern@spincotech.com", "password": "ChangeMe123!"}).json()
        h = {"X-CSRF-Token": login["csrf_token"]}
        with open(PDF, "rb") as f:
            r = c.post("/api/uploads/expense-reports", files={"files": (Path(PDF).name, f, "application/pdf")}, headers=h)
        doc_id = r.json()["results"][0]["document_id"]
        for _ in range(200):
            st = c.get(f"/api/documents/{doc_id}/extraction").json().get("extraction_status")
            if st in ("EXTRACTED", "REVIEW_REQUIRED", "FAILED"):
                break
            time.sleep(3)
    print("status:", st)
finally:
    proc.terminate()
    try:
        proc.wait(10)
    except Exception:
        proc.kill()
hits = Path(MOCK_LOG).read_text().strip().splitlines() if Path(MOCK_LOG).exists() else []
print(f"requests that reached the mock: {len(hits)}")
for line in hits[:5]:
    print("  ", line)
