"""Loopback stand-in for the hosted AI providers (NVIDIA / DeepSeek).

The packaged app is launched with HOSTILE provider settings (enabled=true, a dummy key, endpoints pointed
here). electron/main.js is supposed to force the providers off regardless. Every request that reaches this
server is logged to <log_file>; the validation passes only if the log stays empty. The key used is a dummy
string, not a credential. Usage: python mock_provider.py <port> <log_file>
"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT, LOG = int(sys.argv[1]), sys.argv[2]


class Handler(BaseHTTPRequestHandler):
    def _log(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%H:%M:%S"), "method": self.command, "path": self.path, "bytes": len(body)}) + "\n")
        self.send_response(503)
        self.end_headers()

    do_GET = do_POST = do_PUT = _log

    def log_message(self, *a):  # silence console
        pass


HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
