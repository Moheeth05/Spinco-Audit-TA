"""Every upload the app ACCEPTS is also copied to the TA Backend (Node.js archive), best effort.

The copy must (a) carry the exact bytes, name and upload channel, (b) never change the result of the upload or delay it,
(c) skip anything the app itself refused or already had, and (d) be switchable off. A throw-away local HTTP server stands in for
the TA Backend; all PDFs are SYNTHETIC blank pages.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

import pytest
from pypdf import PdfWriter

from app.services import ta_backend_forward as fwd


def _pdf(width: int) -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=width, height=200)  # a different width = different bytes = a different document
    buf = BytesIO()
    w.write(buf)
    return buf.getvalue()


class FakeTaBackend:
    """Records every request; answers with whatever `status` / `body` / `delay` say."""

    def __init__(self):
        self.received = []
        self.status = 200
        self.body = {"results": [{"filename": "x", "success": True, "document_id": "expense:1"}]}
        self.delay = 0.0
        self.gate = threading.Event()  # a test may clear it to hold every request until it calls gate.set()
        self.gate.set()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(length)
                if outer.delay:
                    time.sleep(outer.delay)
                outer.gate.wait(30)
                outer.received.append({"path": self.path, "headers": dict(self.headers), "body": data})
                payload = json.dumps(outer.body).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def tabackend(monkeypatch):
    fake = FakeTaBackend()
    monkeypatch.setenv("TA_BACKEND_URL", fake.url)
    monkeypatch.setattr(fwd, "_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    yield fake
    fake.stop()


def _upload(client, endpoint, name, content):
    return client.post(f"/api/uploads/{endpoint}", files=[("files", (name, content, "application/pdf"))])


# ---------- what is sent ----------


def test_an_accepted_expense_upload_is_copied_with_its_exact_bytes_name_and_channel(client, tabackend):
    pdf = _pdf(201)
    resp = _upload(client, "expense-reports", "ER-12345.pdf", pdf)
    assert resp.json()["results"][0]["success"] is True
    fwd.wait_for_pending()
    assert len(tabackend.received) == 1
    req = tabackend.received[0]
    assert req["path"] == "/api/uploads/expense-reports"
    assert pdf in req["body"]  # byte-for-byte
    assert b'name="files"; filename="ER-12345.pdf"' in req["body"]
    assert req["headers"]["Content-Type"].startswith("multipart/form-data")
    assert "Authorization" not in req["headers"]


def test_a_trip_upload_goes_to_the_trip_channel(client, tabackend):
    _upload(client, "trip-reports", "TRIP-777.pdf", _pdf(202))
    fwd.wait_for_pending()
    assert [r["path"] for r in tabackend.received] == ["/api/uploads/trip-reports"]


def test_the_api_key_is_sent_as_a_bearer_token_when_configured(client, tabackend, monkeypatch):
    monkeypatch.setenv("TA_BACKEND_API_KEY", "k" * 24)
    _upload(client, "expense-reports", "ER-1.pdf", _pdf(203))
    fwd.wait_for_pending()
    assert tabackend.received[0]["headers"]["Authorization"] == "Bearer " + "k" * 24


# ---------- what is NOT sent ----------


def test_a_duplicate_the_app_already_has_is_not_sent_again(client, tabackend):
    pdf = _pdf(204)
    first = _upload(client, "expense-reports", "ER-2.pdf", pdf).json()["results"][0]
    second = _upload(client, "expense-reports", "again.pdf", pdf).json()["results"][0]
    fwd.wait_for_pending()
    assert first["success"] is True
    assert second["success"] is False and second["error_message"] == "File already exists."
    assert len(tabackend.received) == 1


def test_a_file_the_app_refuses_is_not_sent(client, tabackend):
    for name, content in (("notes.txt", _pdf(205)), ("empty.pdf", b""), ("fake.pdf", b"not a pdf")):
        resp = _upload(client, "expense-reports", name, content)
        assert resp.json()["results"][0]["success"] is False
    fwd.wait_for_pending()
    assert tabackend.received == []


# ---------- the upload itself is never affected ----------


@pytest.mark.parametrize("status,body", [(500, {"detail": "boom"}), (401, {"detail": "key"}), (400, {"detail": "bad"}), (200, {"results": []})])
def test_whatever_the_tabackend_answers_the_upload_result_is_unchanged(client, tabackend, status, body):
    tabackend.status, tabackend.body = status, body
    resp = _upload(client, "expense-reports", "ER-3.pdf", _pdf(206 + status % 7))
    fwd.wait_for_pending()
    item = resp.json()["results"][0]
    assert resp.status_code == 200 and item["success"] is True and item["processing_status"] == "PROCESSED"


def test_a_tabackend_that_is_not_running_changes_nothing(client, monkeypatch):
    monkeypatch.setenv("TA_BACKEND_URL", "http://127.0.0.1:9")  # nothing listens here
    monkeypatch.setattr(fwd, "_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    resp = _upload(client, "expense-reports", "ER-4.pdf", _pdf(215))
    fwd.wait_for_pending()
    assert resp.status_code == 200 and resp.json()["results"][0]["success"] is True


def test_a_stalled_tabackend_does_not_hold_up_the_upload_response(client, tabackend):
    tabackend.gate.clear()  # the TA Backend accepts the connection but does not answer until released
    resp = _upload(client, "expense-reports", "ER-5.pdf", _pdf(216))
    assert resp.json()["results"][0]["success"] is True  # the response is already back, with the copy still in flight
    assert tabackend.received == []
    tabackend.gate.set()
    fwd.wait_for_pending(15)
    assert len(tabackend.received) == 1


def test_the_upload_is_not_affected_when_sending_raises_unexpectedly(client, monkeypatch, caplog):
    monkeypatch.setenv("TA_BACKEND_URL", "http://127.0.0.1:9")

    def boom(**kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(fwd, "send_upload", boom)
    resp = _upload(client, "expense-reports", "ER-6.pdf", _pdf(217))
    fwd.wait_for_pending()
    assert resp.json()["results"][0]["success"] is True


# ---------- settings ----------


def test_default_is_this_computer_port_4000_and_it_can_be_switched_off():
    assert fwd.load_settings({}).url == "http://127.0.0.1:4000"
    assert fwd.load_settings({"TA_BACKEND_URL": "http://localhost:4000/"}).url == "http://localhost:4000"
    for off in ("off", "OFF", "false", "0", "none", "disabled", "", "  "):
        assert fwd.load_settings({"TA_BACKEND_URL": off}).enabled is False, off
    for bad in ("ftp://x", "localhost:4000", "javascript:alert(1)", "http://"):
        assert fwd.load_settings({"TA_BACKEND_URL": bad}).enabled is False, bad
    assert fwd.load_settings({"TA_BACKEND_URL": "https://ta.example.com"}).url == "https://ta.example.com"
    s = fwd.load_settings({"TA_BACKEND_API_KEY": " abc ", "TA_BACKEND_TIMEOUT_SECONDS": "7"})
    assert (s.api_key, s.timeout) == ("abc", 7.0)
    assert fwd.load_settings({"TA_BACKEND_TIMEOUT_SECONDS": "x"}).timeout == 30.0
    assert fwd.load_settings({"TA_BACKEND_TIMEOUT_SECONDS": "100000"}).timeout == 300.0


def test_disabled_means_no_request_and_no_thread(client, tabackend, monkeypatch):
    monkeypatch.setenv("TA_BACKEND_URL", "off")
    before = threading.active_count()
    _upload(client, "expense-reports", "ER-7.pdf", _pdf(218))
    fwd.wait_for_pending()
    assert tabackend.received == []
    assert fwd.send_upload(expense=True, filename="a.pdf", content=b"x") == "disabled"
    assert threading.active_count() <= before + 1  # only the extraction machinery, never a forwarding thread


def test_the_test_suite_default_is_off_so_a_real_tabackend_is_never_reached(client):
    import os

    assert os.environ["TA_BACKEND_URL"] == "off"


# ---------- send_upload outcomes ----------


def test_send_upload_reports_what_happened(tabackend):
    s = fwd.load_settings({"TA_BACKEND_URL": tabackend.url})
    assert fwd.send_upload(expense=True, filename="a.pdf", content=b"%PDF-x", settings=s) == "stored"
    tabackend.body = {"results": [{"success": False, "error_message": "File already exists."}]}
    assert fwd.send_upload(expense=True, filename="a.pdf", content=b"%PDF-x", settings=s) == "exists"
    tabackend.body = {"results": [{"success": False, "error_message": "The PDF has no pages."}]}
    assert fwd.send_upload(expense=False, filename="a.pdf", content=b"%PDF-x", settings=s) == "rejected"
    tabackend.status = 401
    assert fwd.send_upload(expense=True, filename="a.pdf", content=b"%PDF-x", settings=s) == "rejected"
    tabackend.status = 500
    assert fwd.send_upload(expense=True, filename="a.pdf", content=b"%PDF-x", settings=s) == "unreachable"
