"use strict";

/**
 * Pure (no Electron imports) connection diagnostics for REMOTE (central backend) mode, so it can be unit tested with plain Node.
 *
 * It answers one question - "why can this PC not use the central server?" - with ONE of a fixed set of categories, a plain-language
 * message and concrete next steps. Nothing here ever returns a full URL, a response body, a cookie or a credential: only the host
 * (host[:port]) and a fixed message per category.
 *
 *   INVALID_URL          the configured address is not usable (reported by remote-config.js before any network use)
 *   DNS_FAILURE          the host NAME could not be resolved
 *   CONNECTION_REFUSED   the server PC answered, but nothing listens on that port (backend not running / wrong port)
 *   TIMEOUT              no answer at all: typically a firewall dropping the traffic, a wrong address, or a powered-off PC
 *   NETWORK_UNREACHABLE  the operating system has no route to that host
 *   TLS_ERROR            HTTPS certificate problem (untrusted, expired, wrong name)
 *   BACKEND_UNAVAILABLE  something answered but it is not a working Spinco backend (wrong service, gateway/proxy error, connection reset, 502/503/504)
 *   AUTH_FAILURE         the server (or a gateway in front of it) refused the request as unauthorised (401/403)
 *   SERVER_ERROR         the Spinco backend answered with an internal error (500 ...)
 *   OK                   a Spinco backend answered its health check
 */

const http = require("node:http");
const https = require("node:https");

const CATEGORY = Object.freeze({
  OK: "OK",
  INVALID_URL: "INVALID_URL",
  DNS_FAILURE: "DNS_FAILURE",
  CONNECTION_REFUSED: "CONNECTION_REFUSED",
  TIMEOUT: "TIMEOUT",
  NETWORK_UNREACHABLE: "NETWORK_UNREACHABLE",
  TLS_ERROR: "TLS_ERROR",
  BACKEND_UNAVAILABLE: "BACKEND_UNAVAILABLE",
  AUTH_FAILURE: "AUTH_FAILURE",
  SERVER_ERROR: "SERVER_ERROR",
  NO_HOST_FOUND: "NO_HOST_FOUND", // LAN demo: no Spinco demo host answered the network search
});

// Categories that cannot fix themselves in a few seconds: do not keep retrying them at start-up.
const FATAL_CATEGORIES = new Set([CATEGORY.INVALID_URL, CATEGORY.TLS_ERROR, CATEGORY.AUTH_FAILURE]);

const DNS_CODES = new Set(["ENOTFOUND", "EAI_AGAIN", "EAI_NONAME", "EAI_FAIL"]);
const UNREACHABLE_CODES = new Set(["EHOSTUNREACH", "ENETUNREACH", "ENETDOWN", "EADDRNOTAVAIL"]);
const TIMEOUT_CODES = new Set(["ETIMEDOUT", "ESOCKETTIMEDOUT", "SPINCO_TIMEOUT"]);
const RESET_CODES = new Set(["ECONNRESET", "EPIPE", "ECONNABORTED", "HPE_INVALID_CONSTANT"]);

function isTlsCode(code) {
  return (
    /^(CERT_|ERR_TLS_|ERR_SSL_|DEPTH_ZERO|SELF_SIGNED|UNABLE_TO_|HOSTNAME_MISMATCH|ERR_OSSL|EPROTO)/.test(code) ||
    code === "ERR_CERT_COMMON_NAME_INVALID"
  );
}

function errorCode(err) {
  if (!err) return "";
  // Node >= 20 reports a failed multi-address connect as an AggregateError whose own code is empty: look inside.
  if (!err.code && Array.isArray(err.errors) && err.errors.length > 0) return errorCode(err.errors[0]);
  return String(err.code || "");
}

const ADVICE = {
  [CATEGORY.INVALID_URL]: [
    "Ask your administrator for the correct server address.",
    "Fix or remove central-backend.json (use Open settings folder) and start the app again.",
  ],
  [CATEGORY.DNS_FAILURE]: [
    "Check the server name for typing mistakes.",
    "Check that this PC is on the company network (or connected to the VPN).",
    "Ask IT whether the server name is registered in DNS.",
  ],
  [CATEGORY.CONNECTION_REFUSED]: [
    "The server PC is reachable but the Spinco backend is not listening on that port.",
    "Ask the administrator to confirm the backend is running and that the port in the address is correct.",
  ],
  [CATEGORY.TIMEOUT]: [
    "The server did not answer. Most often a firewall on the server (or in between) is blocking the port, or the server PC is off.",
    "Check that you are on the company network or VPN.",
    "Ask IT to allow inbound access to the Spinco backend port from your network (see the IT network guide).",
  ],
  [CATEGORY.NETWORK_UNREACHABLE]: [
    "This PC has no network route to the server. Check the network cable / Wi-Fi / VPN connection.",
  ],
  [CATEGORY.TLS_ERROR]: [
    "The server's HTTPS certificate was not accepted (untrusted, expired or issued for a different name).",
    "Do not bypass this warning. Ask IT to install the company certificate authority or renew the server certificate.",
  ],
  [CATEGORY.BACKEND_UNAVAILABLE]: [
    "Something answered, but it is not a working Spinco backend (a wrong address, a proxy/gateway error, or the backend is still starting).",
    "Wait a minute and try again; if it persists, ask the administrator to check the backend.",
  ],
  [CATEGORY.AUTH_FAILURE]: [
    "The server (or a gateway in front of it) refused the connection as not authorised.",
    "Ask the administrator whether a gateway or proxy requires separate access for this PC.",
  ],
  [CATEGORY.SERVER_ERROR]: [
    "The Spinco backend is running but reported an internal error. Try again in a moment.",
    "If it persists, ask the administrator to check the backend log.",
  ],
  [CATEGORY.NO_HOST_FOUND]: [
    "Make sure the demo host laptop is running the demo and that you are on the SAME Wi-Fi network as it.",
    "Some Wi-Fi networks block laptops from seeing each other (client isolation) or block discovery messages; on such a network only the manual address works, if the network allows it at all.",
    "On the host laptop the Wi-Fi network must be set to Private and the demo firewall rule added (the host instructions explain it).",
    "Type the host address and port shown on the host laptop below and press Connect.",
  ],
  [CATEGORY.OK]: [],
};

const TITLES = {
  [CATEGORY.INVALID_URL]: "The server address is not valid",
  [CATEGORY.DNS_FAILURE]: "The server name could not be found",
  [CATEGORY.CONNECTION_REFUSED]: "The server refused the connection",
  [CATEGORY.TIMEOUT]: "The server did not respond",
  [CATEGORY.NETWORK_UNREACHABLE]: "The server cannot be reached from this PC",
  [CATEGORY.TLS_ERROR]: "The server's security certificate was not accepted",
  [CATEGORY.BACKEND_UNAVAILABLE]: "The Spinco backend is not available",
  [CATEGORY.AUTH_FAILURE]: "The connection was refused as not authorised",
  [CATEGORY.SERVER_ERROR]: "The server reported an error",
  [CATEGORY.NO_HOST_FOUND]: "No Spinco demo host was found on this network",
  [CATEGORY.OK]: "Connected",
};

function describe(category, host, extra = {}) {
  const messages = {
    [CATEGORY.INVALID_URL]: extra.reason || "The configured server address is not valid.",
    [CATEGORY.DNS_FAILURE]: `This PC cannot resolve the server name "${host}".`,
    [CATEGORY.CONNECTION_REFUSED]: `The computer "${host}" actively refused the connection: nothing is listening on that port.`,
    [CATEGORY.TIMEOUT]: `"${host}" did not answer within the time limit.`,
    [CATEGORY.NETWORK_UNREACHABLE]: `There is no network route from this PC to "${host}".`,
    [CATEGORY.TLS_ERROR]: `The HTTPS certificate presented by "${host}" could not be verified.`,
    [CATEGORY.BACKEND_UNAVAILABLE]: `"${host}" answered, but not as a working Spinco backend${extra.httpStatus ? ` (HTTP ${extra.httpStatus})` : ""}.`,
    [CATEGORY.AUTH_FAILURE]: `"${host}" refused the request as not authorised (HTTP ${extra.httpStatus}).`,
    [CATEGORY.SERVER_ERROR]: `"${host}" reported an internal server error (HTTP ${extra.httpStatus}).`,
    [CATEGORY.NO_HOST_FOUND]: "The search of the local network did not find a Spinco demo host.",
    [CATEGORY.OK]: `Connected to "${host}".`,
  };
  return {
    ok: category === CATEGORY.OK,
    category,
    title: TITLES[category],
    message: messages[category],
    advice: ADVICE[category].slice(),
    host,
    httpStatus: extra.httpStatus || null,
    fatal: FATAL_CATEGORIES.has(category),
  };
}

/** Maps a Node network error to a category (never inspects or returns the error message text, which may contain addresses). */
function classifyNetworkError(err) {
  const code = errorCode(err);
  if (DNS_CODES.has(code)) return CATEGORY.DNS_FAILURE;
  if (code === "ECONNREFUSED") return CATEGORY.CONNECTION_REFUSED;
  if (TIMEOUT_CODES.has(code)) return CATEGORY.TIMEOUT;
  if (UNREACHABLE_CODES.has(code)) return CATEGORY.NETWORK_UNREACHABLE;
  if (isTlsCode(code)) return CATEGORY.TLS_ERROR;
  if (RESET_CODES.has(code)) return CATEGORY.BACKEND_UNAVAILABLE;
  return CATEGORY.BACKEND_UNAVAILABLE;
}

/** Maps an HTTP answer from GET /api/health to a category. `body` is the (already read, size-capped) response text. */
function classifyHttpAnswer(statusCode, body) {
  if (statusCode === 401 || statusCode === 403 || statusCode === 407) return { category: CATEGORY.AUTH_FAILURE };
  if (statusCode === 502 || statusCode === 503 || statusCode === 504 || statusCode === 404) return { category: CATEGORY.BACKEND_UNAVAILABLE };
  if (statusCode >= 500) return { category: CATEGORY.SERVER_ERROR };
  if (statusCode === 200) {
    let parsed = null;
    try {
      parsed = JSON.parse(body);
    } catch {
      parsed = null;
    }
    if (parsed && parsed.status === "ok") return { category: CATEGORY.OK, info: parsed };
    return { category: CATEGORY.BACKEND_UNAVAILABLE }; // 200 from something that is not our /api/health
  }
  return { category: CATEGORY.BACKEND_UNAVAILABLE };
}

/**
 * One health probe: GET <origin>/api/health with a hard overall timeout. Resolves (never rejects) with the result of describe().
 * `origin` must already have been validated by remote-config.validateBackendUrl.
 * `info` (when OK) is the backend's own health document ({status, environment, ...}); it contains no secret.
 */
function probeBackend(origin, { timeoutMs = 5000, requestImpl } = {}) {
  let url;
  try {
    url = new URL("/api/health", origin);
  } catch {
    return Promise.resolve(describe(CATEGORY.INVALID_URL, "", { reason: "The configured server address is not valid." }));
  }
  const host = url.host;
  const client = requestImpl || (url.protocol === "https:" ? https : http);

  return new Promise((resolve) => {
    let settled = false;
    let req = null;
    const done = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        if (req) req.destroy();
      } catch {
        // already closed
      }
      resolve(result);
    };
    const timer = setTimeout(() => done(describe(CATEGORY.TIMEOUT, host)), timeoutMs);

    try {
      req = client.get(url, { timeout: timeoutMs, headers: { Accept: "application/json" } }, (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          if (body.length < 8192) body += chunk; // a health answer is tiny; never buffer an unbounded body
        });
        res.on("end", () => {
          const { category, info } = classifyHttpAnswer(res.statusCode, body);
          const result = describe(category, host, { httpStatus: res.statusCode });
          if (info) result.info = info;
          done(result);
        });
        res.on("error", (err) => done(describe(classifyNetworkError(err), host)));
      });
      req.on("timeout", () => done(describe(CATEGORY.TIMEOUT, host)));
      req.on("error", (err) => done(describe(classifyNetworkError(err), host)));
    } catch (err) {
      done(describe(classifyNetworkError(err), host));
    }
  });
}

/**
 * Probes repeatedly (a server may be starting) until it is OK, a category that cannot fix itself appears, or `totalMs` has passed.
 * Returns the last result (OK or the failure). `onAttempt(result, n)` is for logging (it receives the same safe result object).
 */
async function waitForBackend(origin, { totalMs = 20000, intervalMs = 1500, probeTimeoutMs = 4000, requestImpl, sleep, onAttempt } = {}) {
  const wait = sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const deadline = Date.now() + totalMs;
  let attempt = 0;
  let last;
  for (;;) {
    attempt += 1;
    last = await probeBackend(origin, { timeoutMs: probeTimeoutMs, requestImpl });
    if (onAttempt) {
      try {
        onAttempt(last, attempt);
      } catch {
        // logging must never break the connection flow
      }
    }
    if (last.ok || last.fatal) break;
    if (Date.now() + intervalMs >= deadline) break;
    await wait(intervalMs);
  }
  last.attempts = attempt;
  return last;
}

/** A result for an invalid configuration (remote-config.js "invalid" mode), in the same shape. */
function invalidConfigResult(reason) {
  return describe(CATEGORY.INVALID_URL, "", { reason });
}

module.exports = {
  CATEGORY,
  FATAL_CATEGORIES,
  describe,
  classifyNetworkError,
  classifyHttpAnswer,
  probeBackend,
  waitForBackend,
  invalidConfigResult,
};
