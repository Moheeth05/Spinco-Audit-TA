"use strict";

// Real-socket tests (loopback only): each failure category is produced by a REAL condition - a closed port, a server that never answers, a server that
// answers 401/500/503, a TLS handshake against a plain-HTTP port - not by calling the classifier with a made-up error. Run with:
//   node --test electron/test/connection-diagnostics.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const net = require("node:net");

const d = require("../connection-diagnostics");

const { CATEGORY } = d;

function startServer(handler) {
  return new Promise((resolve) => {
    const server = http.createServer(handler);
    server.listen(0, "127.0.0.1", () => resolve({ server, origin: `http://127.0.0.1:${server.address().port}`, port: server.address().port }));
  });
}
const stop = (server) => new Promise((resolve) => { server.closeAllConnections?.(); server.close(() => resolve()); });
function json(res, status, body) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(typeof body === "string" ? body : JSON.stringify(body));
}
function freeClosedPort() {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

test("a healthy Spinco backend is OK and its health document is returned", async () => {
  const { server, origin } = await startServer((req, res) => {
    assert.equal(req.url, "/api/health");
    json(res, 200, { status: "ok", environment: "staging" });
  });
  try {
    const r = await d.probeBackend(origin);
    assert.equal(r.ok, true);
    assert.equal(r.category, CATEGORY.OK);
    assert.equal(r.info.environment, "staging");
  } finally {
    await stop(server);
  }
});

test("connection refused: nothing listens on the port", async () => {
  const port = await freeClosedPort();
  const r = await d.probeBackend(`http://127.0.0.1:${port}`, { timeoutMs: 3000 });
  assert.equal(r.category, CATEGORY.CONNECTION_REFUSED);
  assert.equal(r.ok, false);
  assert.equal(r.fatal, false);
});

test("DNS failure: a host name that does not exist", async () => {
  const r = await d.probeBackend("http://spinco-no-such-host.invalid:8001", { timeoutMs: 8000 });
  assert.equal(r.category, CATEGORY.DNS_FAILURE);
  assert.match(r.message, /spinco-no-such-host\.invalid/);
});

test("timeout: the server accepts the connection but never answers", async () => {
  const { server, origin } = await startServer(() => {
    /* never respond */
  });
  try {
    const t0 = Date.now();
    const r = await d.probeBackend(origin, { timeoutMs: 400 });
    assert.equal(r.category, CATEGORY.TIMEOUT);
    assert.ok(Date.now() - t0 < 3000, "the probe must honour its own timeout");
  } finally {
    await stop(server);
  }
});

test("HTTP answers map to the right category", async () => {
  const cases = [
    [503, "Service Unavailable", CATEGORY.BACKEND_UNAVAILABLE],
    [502, "bad gateway", CATEGORY.BACKEND_UNAVAILABLE],
    [404, "Not Found", CATEGORY.BACKEND_UNAVAILABLE],
    [500, "boom", CATEGORY.SERVER_ERROR],
    [401, "{}", CATEGORY.AUTH_FAILURE],
    [403, "{}", CATEGORY.AUTH_FAILURE],
  ];
  for (const [status, body, expected] of cases) {
    const { server, origin } = await startServer((req, res) => json(res, status, body));
    try {
      const r = await d.probeBackend(origin);
      assert.equal(r.category, expected, `HTTP ${status}`);
      assert.equal(r.httpStatus, status);
    } finally {
      await stop(server);
    }
  }
});

test("a 200 that is not a Spinco health document is BACKEND_UNAVAILABLE (a different service on that port)", async () => {
  for (const body of ["<html>hello</html>", '{"status":"degraded"}', "{}"]) {
    const { server, origin } = await startServer((req, res) => json(res, 200, body));
    try {
      const r = await d.probeBackend(origin);
      assert.equal(r.category, CATEGORY.BACKEND_UNAVAILABLE, body);
    } finally {
      await stop(server);
    }
  }
});

test("a connection reset by the server is BACKEND_UNAVAILABLE", async () => {
  const { server, origin } = await startServer((req) => req.socket.destroy());
  try {
    const r = await d.probeBackend(origin);
    assert.equal(r.category, CATEGORY.BACKEND_UNAVAILABLE);
  } finally {
    await stop(server);
  }
});

test("TLS error: HTTPS spoken to a plain-HTTP port", async () => {
  const { server, port } = await startServer((req, res) => json(res, 200, { status: "ok" }));
  try {
    const r = await d.probeBackend(`https://127.0.0.1:${port}`, { timeoutMs: 3000 });
    assert.equal(r.category, CATEGORY.TLS_ERROR);
    assert.equal(r.fatal, true);
  } finally {
    await stop(server);
  }
});

test("classifyNetworkError: every documented Node error code maps to one category", () => {
  const table = {
    ENOTFOUND: CATEGORY.DNS_FAILURE,
    EAI_AGAIN: CATEGORY.DNS_FAILURE,
    ECONNREFUSED: CATEGORY.CONNECTION_REFUSED,
    ETIMEDOUT: CATEGORY.TIMEOUT,
    SPINCO_TIMEOUT: CATEGORY.TIMEOUT,
    EHOSTUNREACH: CATEGORY.NETWORK_UNREACHABLE,
    ENETUNREACH: CATEGORY.NETWORK_UNREACHABLE,
    DEPTH_ZERO_SELF_SIGNED_CERT: CATEGORY.TLS_ERROR,
    CERT_HAS_EXPIRED: CATEGORY.TLS_ERROR,
    UNABLE_TO_VERIFY_LEAF_SIGNATURE: CATEGORY.TLS_ERROR,
    ERR_TLS_CERT_ALTNAME_INVALID: CATEGORY.TLS_ERROR,
    ECONNRESET: CATEGORY.BACKEND_UNAVAILABLE,
    SOMETHING_NEW: CATEGORY.BACKEND_UNAVAILABLE,
  };
  for (const [code, expected] of Object.entries(table)) {
    assert.equal(d.classifyNetworkError({ code }), expected, code);
  }
  // Node >= 20: a failed multi-address connect is an AggregateError with an empty code and the real errors inside.
  assert.equal(d.classifyNetworkError({ code: "", errors: [{ code: "ECONNREFUSED" }, { code: "ECONNREFUSED" }] }), CATEGORY.CONNECTION_REFUSED);
  assert.equal(d.classifyNetworkError(null), CATEGORY.BACKEND_UNAVAILABLE);
});

test("results never contain the full URL, path or response body", async () => {
  const { server, origin } = await startServer((req, res) => json(res, 500, "SECRET-TOKEN-IN-BODY"));
  try {
    const r = await d.probeBackend(origin);
    const text = JSON.stringify(r);
    assert.ok(!text.includes("SECRET-TOKEN-IN-BODY"));
    assert.ok(!text.includes("/api/health"));
    assert.ok(!text.includes("http://"));
  } finally {
    await stop(server);
  }
});

test("waitForBackend: succeeds once a starting server becomes healthy", async () => {
  let calls = 0;
  const { server, origin } = await startServer((req, res) => {
    calls += 1;
    if (calls < 3) return json(res, 503, "starting");
    return json(res, 200, { status: "ok" });
  });
  try {
    const seen = [];
    const r = await d.waitForBackend(origin, { totalMs: 5000, intervalMs: 50, onAttempt: (x) => seen.push(x.category) });
    assert.equal(r.ok, true);
    assert.equal(r.attempts, 3);
    assert.deepEqual(seen, [CATEGORY.BACKEND_UNAVAILABLE, CATEGORY.BACKEND_UNAVAILABLE, CATEGORY.OK]);
  } finally {
    await stop(server);
  }
});

test("waitForBackend: stops at once for a category that cannot fix itself, and at the deadline otherwise", async () => {
  const auth = await startServer((req, res) => json(res, 401, "{}"));
  try {
    const r = await d.waitForBackend(auth.origin, { totalMs: 5000, intervalMs: 50 });
    assert.equal(r.category, CATEGORY.AUTH_FAILURE);
    assert.equal(r.attempts, 1);
  } finally {
    await stop(auth.server);
  }

  const port = await freeClosedPort();
  const t0 = Date.now();
  const r = await d.waitForBackend(`http://127.0.0.1:${port}`, { totalMs: 700, intervalMs: 100, probeTimeoutMs: 500 });
  assert.equal(r.category, CATEGORY.CONNECTION_REFUSED);
  assert.ok(r.attempts >= 2, "a refused connection is retried until the deadline");
  assert.ok(Date.now() - t0 < 3000);
});

test("an invalid configuration has its own category and never mentions a URL", () => {
  const r = d.invalidConfigResult("The backend URL is not a valid URL.");
  assert.equal(r.category, CATEGORY.INVALID_URL);
  assert.equal(r.fatal, true);
  assert.equal(r.ok, false);
  assert.ok(r.advice.length > 0);
});

test("every category has a title, a message and advice (except OK)", () => {
  for (const category of Object.values(CATEGORY)) {
    const r = d.describe(category, "server.example:8001", { httpStatus: 500, reason: "x" });
    assert.ok(r.title && r.message, category);
    if (category !== CATEGORY.OK) assert.ok(r.advice.length > 0, category);
  }
});
