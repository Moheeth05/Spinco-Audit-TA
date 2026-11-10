"""AUTOMATED LAN TEST (one PC): the real host launcher (Start-DemoHost.ps1, listening on the Wi-Fi address, advertising by mDNS, real STAGING Drive) and THREE packaged
clients that have NO settings and find the host by themselves (demo-defaults.json -> mDNS discovery), each signed in with their OWN account.
This is NOT a physical multi-laptop test: all processes run on this PC (real sockets and real multicast, but one machine - it cannot show Wi-Fi isolation or other laptops' firewalls).
usage: lan_e2e.py <win-unpacked of the LAN-demo build> <evidence dir>"""
import csv
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from io import BytesIO
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright
from pypdf import PdfWriter

APP_DIR, EVID = Path(sys.argv[1]), Path(sys.argv[2])
APP = APP_DIR / "Spinco TA Audit.exe"
HOST_SCRIPT = Path(r"D:\SpincoTA-artifacts\lan-demo\tools\Start-DemoHost.ps1")
RUNS = Path(r"D:\SpincoTA-artifacts\central-staging")
EVID.mkdir(parents=True, exist_ok=True)
R = []
NAMES = ["Priya", "Arun", "Meena"]


def rec(name, ok, detail=""):
    R.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""), flush=True)


def wait_for(pred, timeout, step=0.7):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = pred()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def jget(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def synthetic_pdf():
    w = PdfWriter(); w.add_blank_page(width=300 + int(time.time()) % 200, height=200); w.add_metadata({"/Title": "synthetic-lan-demo"})
    b = BytesIO(); w.write(b); return b.getvalue()


host_proc = None


def start_host(resume=None):
    global host_proc
    args = ["powershell", "-NoProfile", "-File", str(HOST_SCRIPT)]
    args += ["-Resume", str(resume)] if resume else ["-Participants", ",".join(NAMES)]
    out = open(EVID / ("host-resume.out" if resume else "host.out"), "wb")
    host_proc = subprocess.Popen(args, stdout=out, stderr=out)


def stop_host():
    subprocess.run(["taskkill", "/PID", str(host_proc.pid), "/T", "/F"], capture_output=True)
    # the launcher's children (backend, advertiser) are in the tree; also make sure nothing is left on the demo ports
    subprocess.run(["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'demo_advertise.py|run_server.py' } | % { Stop-Process -Id $_.ProcessId -Force }"], capture_output=True)


class Client:
    def __init__(self, label, pw):
        self.label, self.ud = label, EVID / f"ud-{label}"
        self.ud.mkdir(parents=True, exist_ok=True)  # NO central-backend.json: the build's demo defaults make it search the LAN by itself
        self.cdp = free_port()
        env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE" and not k.startswith("SPINCO_")}
        self.proc = subprocess.Popen([str(APP), f"--user-data-dir={self.ud}", f"--remote-debugging-port={self.cdp}"], env=env)
        wait_for(lambda: len(jget(f"http://127.0.0.1:{self.cdp}/json")) > 0, 60)
        self.browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp}")
        self.page = self.browser.contexts[0].pages[0]

    def text(self):
        try:
            return self.page.inner_text("body", timeout=3000)
        except Exception:
            return ""

    def url(self):
        try:
            return self.page.url
        except Exception:
            return ""

    def login(self, email, password):
        self.page.fill('input[type="email"], input[name="email"]', email)
        self.page.fill('input[type="password"]', password)
        self.page.click('button[type="submit"]')
        return wait_for(lambda: "Sign in" not in self.text() and "Upload" in self.text(), 30)

    def api(self, path, method="GET"):
        return self.page.evaluate("""async ([p, m]) => { const h = {}; if (m !== 'GET') { const me = await (await fetch('/api/auth/me', {credentials:'include'})).json(); h['X-CSRF-Token'] = me.csrf_token; }
            const r = await fetch('/api' + p, {method: m, credentials:'include', headers: h}); let b = null; try { b = await r.json(); } catch (e) {} return {status: r.status, body: b}; }""", [path, method])

    def sha_of(self, path):
        # crypto.subtle does not exist on a plain-HTTP LAN origin (not a secure context), so the bytes are fetched in the page and hashed here
        r = self.page.evaluate("async (p) => { const r = await fetch('/api' + p, {credentials:'include'}); const u = new Uint8Array(await r.arrayBuffer()); let s = ''; for (let i = 0; i < u.length; i += 8192) s += String.fromCharCode.apply(null, u.subarray(i, i + 8192)); return {status: r.status, b64: btoa(s)}; }", path)
        import base64
        raw = base64.b64decode(r["b64"])
        return {"status": r["status"], "size": len(raw), "head": list(raw[:2]), "sha": hashlib.sha256(raw).hexdigest()}
    def shot(self, tag):
        try:
            self.page.screenshot(path=str(EVID / f"{self.label}-{tag}.png"))
        except Exception:
            pass

    def close(self):
        try:
            self.browser.close()
        except Exception:
            pass
        subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)


before = set(p.name for p in RUNS.glob("lan-demo-*"))
start_host()
run = wait_for(lambda: next((p for p in RUNS.glob("lan-demo-*") if p.name not in before and (p / "participants.csv").exists()), None), 240, 2)
rec("host launcher started the backend, advertised it and created the participant accounts", bool(run), str(run.name if run else ""))
if not run:
    stop_host(); sys.exit(1)
settings = json.loads((run / "host-settings.json").read_text())
port = settings["port"]
lan_ip = next((i for i in re.findall(r"(\d+\.\d+\.\d+\.\d+)", (EVID / "host.out").read_text(errors="ignore")) if i.startswith(("10.", "192.168."))), None)
rec("the host listens on the LAN address, not only on loopback", bool(lan_ip) and bool(wait_for(lambda: jget(f"http://{lan_ip}:{port}/api/health")["status"] == "ok", 20)), f"{lan_ip}:{port}")
rec("the demo port is not one of the old test ports", port not in (8001, 55686, 58732), str(port))
creds = {row["name"]: row for row in csv.DictReader(open(run / "participants.csv", encoding="utf-8"))}
pw_of = {n: creds[n]["password (first sign-in forces a new one)"] for n in NAMES}
rec("each participant has an individual account and a different password", len(creds) == 3 and len(set(pw_of.values())) == 3, "emails " + ", ".join(c["email"] for c in creds.values()))
admin_pw = [l for l in (run / "host-admin.txt").read_text().splitlines() if l.startswith("password:")][0].split(":", 1)[1].strip()
rec("no participant password equals the host admin password or the development default", admin_pw not in pw_of.values() and "ChangeMe123!" not in pw_of.values())

# every participant sets their OWN new password first (the first sign-in forces it) - done through the API, then they use the UI
own_pw = {}
for n in NAMES:
    http = httpx.Client(base_url=f"http://{lan_ip}:{port}", timeout=30)
    r = http.post("/api/auth/login", json={"email": creds[n]["email"], "password": pw_of[n]})
    assert r.status_code == 200, r.text
    own_pw[n] = pw_of[n] + "-own7!"
    c = http.post("/api/auth/change-password", headers={"X-CSRF-Token": r.json()["csrf_token"]}, json={"current_password": pw_of[n], "new_password": own_pw[n], "confirm_new_password": own_pw[n]})
    assert c.status_code == 200, c.text
rec("each participant changed to their own password (forced on first sign-in)", True)

pw = sync_playwright().start()
clients = {}
try:
    for n in NAMES:
        clients[n] = Client(n, pw)
    for n in NAMES:
        c = clients[n]
        found = wait_for(lambda c=c: "Sign in" in c.text() or "Email" in c.text(), 90)
        rec(f"{n}: the app found the demo host by itself (no settings, no address typed) and shows the login page", found, c.url())
        rec(f"{n}: the connection points at the LAN address of the host", lan_ip in c.url() and str(port) in c.url(), c.url())
    rec("no client has a company-server settings file or a local database", not any((clients[n].ud / x).exists() for n in NAMES for x in ("central-backend.json", "database", "documents")))
    for n in NAMES:
        rec(f"{n}: signed in with their own account", bool(clients[n].login(creds[n]["email"], own_pw[n])))
    for n in NAMES:
        t = wait_for(lambda n=n: "Staging" in clients[n].text() and "Connected" in clients[n].text(), 30)
        rec(f"{n}: header shows the host and 'Connected'", t)
    A, B, C = clients["Priya"], clients["Arun"], clients["Meena"]

    # 1-2: A uploads, B sees it
    pdf = synthetic_pdf(); sha = hashlib.sha256(pdf).hexdigest()
    p1 = EVID / "ER-24680.pdf"; p1.write_bytes(pdf)
    A.page.goto(f"http://{lan_ip}:{port}/upload"); wait_for(lambda: "Upload Expense Reports" in A.text(), 20)
    A.page.set_input_files('input[type="file"]', str(p1))
    rec("laptop A (Priya): upload shows 'Stored centrally'", bool(wait_for(lambda: "Stored centrally" in A.text(), 90)))
    B.page.goto(f"http://{lan_ip}:{port}/upload")
    rec("laptop B (Arun): sees the uploaded document without uploading it", bool(wait_for(lambda: "ER-24680.pdf" in B.text() and "Stored centrally" in B.text(), 40)))
    la = A.api("/uploads")["body"]
    rec("the document records Priya as uploader", len(la) == 1 and "Priya" in str(la[0].get("uploaded_by_name")), str(la[0].get("uploaded_by_name")) if la else "")
    doc_id = la[0]["document_id"]
    # 3-6: B uploads the exact same file (other name) -> duplicate
    p2 = EVID / "renamed-copy.pdf"; p2.write_bytes(pdf)
    B.page.goto(f"http://{lan_ip}:{port}/upload"); wait_for(lambda: "Upload Expense Reports" in B.text(), 20)
    B.page.set_input_files('input[type="file"]', str(p2))
    wait_for(lambda: "uplicate" in B.text(), 40)
    ub = B.api("/uploads")["body"]
    rec("only ONE canonical document exists centrally", len(ub) == 1 and ub[0]["document_id"] == doc_id)
    att = B.api("/uploads/attempts?result=DUPLICATE")["body"]
    dup = [x for x in att if x.get("original_filename") == "renamed-copy.pdf"]
    rec("the duplicate attempt is recorded once, with Arun as the uploader and the original document referenced", len(dup) == 1 and "Arun" in str(dup[0].get("uploaded_by_name")) and dup[0].get("duplicate_of", {}).get("document_id") == doc_id, str(dup[0].get("uploaded_by_name")) if dup else "")
    # 7: C sees the same duplicate information
    ac = C.api("/uploads/attempts?result=DUPLICATE")["body"]
    rec("laptop C (Meena) sees the same duplicate information", [x["id"] for x in ac] == [x["id"] for x in att] and len(ac) == 1)
    C.page.goto(f"http://{lan_ip}:{port}/duplicates"); wait_for(lambda: "renamed-copy.pdf" in C.text(), 25)
    rec("laptop C's Duplicates page shows the attempt", "renamed-copy.pdf" in C.text() and "Arun" in C.text()); C.shot("duplicates")
    # 8-9: processing finished centrally, identical results everywhere
    final = wait_for(lambda: (lambda x: x if x and x[0].get("extraction_status") not in (None, "PENDING", "QUEUED", "PROCESSING", "RUNNING") else None)(A.api("/uploads")["body"]), 120)
    rec("processing completed centrally (a final extraction state is reached)", bool(final), str(final[0].get("extraction_status")) if final else "")
    views = [x.api("/uploads")["body"] for x in (A, B, C)]
    reports = [x.api("/expense-reports")["body"] for x in (A, B, C)]
    rec("all three clients see identical document, extraction and storage state", views[0] == views[1] == views[2], str(views[0][0].get("extraction_status")))
    rec("all three clients see identical expense-report / audit results", reports[0] == reports[1] == reports[2])
    rid = doc_id.split(":")[1]
    ex = [x.api(f"/expense-reports/{rid}/exceptions")["body"] for x in (A, B, C)]
    rec("all three clients see identical exception results", ex[0] == ex[1] == ex[2], f"{len(ex[0]) if isinstance(ex[0], list) else 'n/a'} exceptions")
    # 10: download from another client, Excel export
    d = C.sha_of(f"/documents/{doc_id}/file?download=true")
    rec("laptop C downloads the central copy and its SHA-256 equals the original", d["status"] == 200 and d["sha"] == sha)
    g = A.api("/excel-files/generate", "POST")
    rec("Excel export generated centrally (and a copy kept in the Drive 'Excel Exports' folder)", g["status"] == 200 and g["body"].get("central_copy") == "STORED", str(g["body"].get("central_copy") if g["body"] else g["status"]))
    xb, xc = B.sha_of("/excel-files/download"), C.sha_of("/excel-files/download")
    rec("laptops B and C download the SAME workbook (valid xlsx, identical SHA-256)", xb["status"] == 200 and xb["head"] == [80, 75] and xb["sha"] == xc["sha"])
    # roles
    jobs = B.api("/storage/jobs")
    rec("a participant cannot use administrator functions (storage jobs / users / audit logs refused)", jobs["status"] == 403 and B.api("/users")["status"] == 403 and B.api("/audit-logs")["status"] == 403, f"jobs {jobs['status']}")
    recs = B.api("/storage/records")
    leaked = [k for row in (recs["body"] or []) for k in row if k in ("location", "drive_id", "file_id", "url", "md5", "sha256")]
    admin_only = [row for row in (recs["body"] or []) if row.get("failure_class") or row.get("attempts") is not None]
    rec("the participant's storage view is the ordinary one: no Drive id/location/checksum/URL and no administrator progress fields", recs["status"] == 200 and not leaked and not admin_only, f"fields leaked: {leaked}")    # 11: outage
    A.shot("before-outage"); stop_host()
    lost = [wait_for(lambda c=c: "Cannot reach the Spinco server" in c.text(), 90) for c in (A, B, C)]
    rec("host stopped: all three clients show the outage alert", all(lost))
    rec("host stopped: no client switched to a local database or started a local backend", not any((clients[n].ud / x).exists() for n in NAMES for x in ("database", "documents")))
    A.shot("outage")
    # 12: restart the host with the same data
    start_host(resume=run)
    back = [wait_for(lambda c=c: "Cannot reach the Spinco server" not in c.text() and "Connected" in c.text(), 150) for c in (A, B, C)]
    rec("host restarted (same database and Drive folder): all three clients recover by themselves", all(back))
    after = B.api("/uploads")
    rec("after the restart the shared document and its central copy are still there for another client", after["status"] == 200 and len(after["body"]) == 1 and after["body"][0]["storage_state"] == "STORED", str(after["status"]))
finally:
    for c in clients.values():
        c.close()
    pw.stop()
    stop_host()

(EVID / "lan-e2e-results.json").write_text(json.dumps(R, indent=2), encoding="utf-8")
ok = sum(1 for r in R if r["ok"])
print(f"\nLAN-E2E: {ok}/{len(R)} checks passed (one PC; NOT a physical multi-laptop test)")
sys.exit(0 if ok == len(R) else 1)
