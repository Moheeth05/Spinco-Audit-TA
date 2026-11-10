"""SYNTHETIC capacity probe for the central backend (NOT a pytest module: its name does not start with test_).

It starts the REAL application as a real HTTP server on loopback (uvicorn in a thread), in `serve` mode, on a NEW disposable database, with the in-memory FAKE Drive,
creates N users, and has them upload synthetic PDFs concurrently over HTTP. It then lets the durable job worker drain the extraction queue and reports what it measured.

WHAT THIS MEASURES: the backend's own request handling under concurrency - multipart parsing, PDF validation, hashing, the SQLite database, the durable queue, the
working-copy writes and the job worker. WHAT IT DOES NOT MEASURE (so it can never support a claim about them): Google Drive network speed, latency or rate limits
(the fake Drive is instant and in memory); real OCR time (the synthetic PDFs have no scanned pages); PostgreSQL; a second machine; the network. Run the staging check and
a staged load test against the real staging Shared Drive before any capacity statement about the real system.

    python tests\\capacity_central.py --work D:\\SpincoTA-artifacts\\central-staging\\capacity-001 --users 25 --docs 250 --size-mb 5
"""

import argparse
import ctypes
import json
import os
import secrets
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
APPROVED_ROOT = Path(r"D:\SpincoTA-artifacts\central-staging")


def peak_rss_mb() -> float | None:
    if os.name != "nt":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return round(counters.PeakWorkingSetSize / (1024 * 1024), 1)


def padded_pdf(marker: str, size_mb: float) -> bytes:
    """A valid one-page PDF whose size is about `size_mb` (random bytes in a metadata stream), different per marker. Synthetic: no content of any real document."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200 + (sum(map(ord, marker)) % 400), height=200)
    writer.add_metadata({"/Title": f"synthetic-capacity-{marker}"})
    stream = writer._add_object(__import__("pypdf").generic.DecodedStreamObject())
    stream.set_data(os.urandom(max(int(size_mb * 1024 * 1024) - 2048, 1)))
    writer._root_object[__import__("pypdf").generic.NameObject("/SyntheticPadding")] = stream.indirect_reference
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", required=True)
    parser.add_argument("--users", type=int, default=25)
    parser.add_argument("--docs", type=int, default=250)
    parser.add_argument("--size-mb", type=float, default=5.0)
    parser.add_argument("--port", type=int, default=58790)
    args = parser.parse_args(argv)
    work = Path(args.work).resolve()
    try:
        work.relative_to(APPROVED_ROOT.resolve())
    except ValueError:
        print(f"Refused: the work folder must be under {APPROVED_ROOT}.", file=sys.stderr)
        return 2
    if work.exists() and any(work.iterdir()):
        print("Refused: the work folder is not empty; use a new one.", file=sys.stderr)
        return 2
    work.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(BACKEND_DIR))
    sys.path.insert(0, str(BACKEND_DIR / "tests"))
    from tools.init_central_staging_db import main as init_db

    admin_password = "Cap-" + secrets.token_urlsafe(18)
    os.environ.update({"SPINCO_STORAGE_ROOT": str(work / "storage"), "SPINCO_CENTRAL_STORAGE_MODE": "serve", "SPINCO_JOB_WORKER": "off", "TA_BACKEND_URL": "off",
                       "SPINCO_ADMIN_INITIAL_PASSWORD": admin_password, "SPINCO_ENVIRONMENT": "dev", "SPINCO_ARGON2_TIME_COST": "1", "SPINCO_ARGON2_MEMORY_COST": "8",
                       "SPINCO_ARGON2_PARALLELISM": "1"})
    if init_db(["--db", str(work / "capacity.db")]) != 0:
        return 2

    import httpx
    import uvicorn

    from app.main import app
    from app.services import central_storage, processing_jobs
    from drive_testkit import make_stack

    stack = make_stack(Path(tempfile.mkdtemp(prefix="capacity-", dir=work)))
    central_storage.manager = lambda: stack.manager
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{args.port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/api/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)

    def signed_in(email, password):
        client = httpx.Client(base_url=base, timeout=120)
        response = client.post("/api/auth/login", json={"email": email, "password": password})
        response.raise_for_status()
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return client

    admin = signed_in("intern@spincotech.com", admin_password)
    password = "Cap-" + secrets.token_urlsafe(12)
    clients = []
    for n in range(args.users):
        email = f"capacity.user{n}@example.test"
        admin.post("/api/users", json={"name": f"Capacity User {n}", "email": email, "role": "ADMIN", "password": password}).raise_for_status()
        clients.append(signed_in(email, password))

    docs = [(f"cap-{n}", f"ER-8{n:05d}.pdf") for n in range(args.docs)]
    content_of = {}
    print(f"generating {args.docs} synthetic PDFs of about {args.size_mb} MB ...")
    t0 = time.time()
    for marker, _ in docs:
        content_of[marker] = padded_pdf(marker, args.size_mb)
    print(f"generated in {time.time() - t0:.1f}s")

    latencies, results, errors = [], {}, []
    lock = threading.Lock()

    def one(index):
        marker, name = docs[index]
        client = clients[index % len(clients)]
        started = time.time()
        try:
            response = client.post("/api/uploads/expense-reports", files=[("files", (name, content_of[marker], "application/pdf"))])
            elapsed = time.time() - started
            if response.status_code != 200:
                with lock:
                    errors.append(f"HTTP {response.status_code}")
                return
            result = response.json()["results"][0]["result"]
        except httpx.HTTPError as exc:
            with lock:
                errors.append(type(exc).__name__)
            return
        with lock:
            latencies.append(elapsed)
            results[result] = results.get(result, 0) + 1

    started = time.time()
    with ThreadPoolExecutor(max_workers=args.users) as pool:
        list(pool.map(one, range(len(docs))))
    upload_seconds = time.time() - started
    total_mb = sum(len(c) for c in content_of.values()) / (1024 * 1024)

    queued_before = len(admin.get("/api/storage/jobs?state=queued").json())
    drain_started = time.time()
    processing_jobs.start_worker(poll_seconds=0.2)
    deadline = time.time() + 1800
    while time.time() < deadline:
        counts = admin.get("/api/central/status").json().get("job_counts") or {}
        if counts.get("QUEUED", 0) + counts.get("RUNNING", 0) == 0:
            break
        time.sleep(1)
    drain_seconds = time.time() - drain_started
    processing_jobs.stop_worker()
    final_counts = admin.get("/api/central/status").json().get("job_counts")
    summary = {
        "what_this_is": "synthetic, loopback, FAKE Drive, trivial extraction - see the module docstring for what it cannot show",
        "users": args.users, "documents": args.docs, "document_size_mb": args.size_mb, "total_mb": round(total_mb, 1),
        "upload_phase_seconds": round(upload_seconds, 1), "uploads_per_minute": round(len(latencies) / upload_seconds * 60, 1), "mb_per_second": round(total_mb / upload_seconds, 2),
        "latency_seconds": {"p50": round(statistics.median(latencies), 2), "p95": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 2), "max": round(max(latencies), 2)} if latencies else None,
        "results": results, "http_or_transport_errors": {e: errors.count(e) for e in set(errors)},
        "queue_before_drain": queued_before, "drain_seconds": round(drain_seconds, 1), "job_counts_after": final_counts,
        "drive_files_created": stack.fake.file_count(), "peak_working_set_mb": peak_rss_mb(),
        "database_mb": round((work / "capacity.db").stat().st_size / (1024 * 1024), 1),
    }
    (work / "capacity-result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    server.should_exit = True
    thread.join(timeout=15)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
