"use strict";

/**
 * LAN DEMO discovery (mDNS / Bonjour). Pure Node, no Electron imports, so it is unit-testable.
 *
 * The demo host advertises a service of type `_spinco-ta-demo._tcp` (see tools/demo/demo_advertise.py). A client browses for it, keeps only
 * PRIVATE IPv4 addresses (10/8, 172.16/12, 192.168/16 - never a public address), and VERIFIES each candidate by asking its /api/health
 * (it must answer as a Spinco backend) before it is ever used. Nothing is hard-coded: no address, no port.
 *
 * Security note (demo only): the connection is plain HTTP on the local network and the advertisement is not authenticated; use synthetic documents
 * and per-participant demo passwords. This is NOT used for the company server, which is configured explicitly.
 */

const SERVICE_TYPE = "spinco-ta-demo";

function isPrivateIPv4(ip) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(String(ip || ""));
  if (!m) return false;
  const [a, b, c, d] = m.slice(1).map(Number);
  if ([a, b, c, d].some((n) => n > 255)) return false;
  return a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168);
}

/** Validates what a person typed as the manual fallback: "host:port" or "http://host:port" with a private IPv4 or a plain host name (no path, no credentials). */
function parseManualAddress(text) {
  const raw = String(text || "").trim();
  if (!raw) return { ok: false, reason: "Enter the host address and port exactly as shown on the host laptop (host:port)." };
  const withScheme = /^https?:\/\//i.test(raw) ? raw : `http://${raw}`;
  let u;
  try {
    u = new URL(withScheme);
  } catch {
    return { ok: false, reason: "That is not a valid address. Use host:port exactly as shown on the host laptop." };
  }
  if (u.protocol !== "http:") return { ok: false, reason: "The demo host uses plain http on the local network." };
  if (u.username || u.password || (u.pathname && u.pathname !== "/") || u.search || u.hash) return { ok: false, reason: "Enter only the host and port (no path or credentials)." };
  if (!u.port) return { ok: false, reason: "Include the port shown on the host laptop (host:port)." };
  const host = u.hostname;
  const isIp = /^\d+\.\d+\.\d+\.\d+$/.test(host);
  if (isIp && !isPrivateIPv4(host) && !/^127\./.test(host)) return { ok: false, reason: "Only addresses on the local network (10.x, 172.16-31.x, 192.168.x) are accepted for the demo." };
  return { ok: true, url: u.origin, host: u.host };
}

/**
 * Browses for demo hosts for `timeoutMs`, then verifies every candidate. `bonjourFactory` / `probe` are injectable for tests.
 * Returns [{ name, url, host, address, port, info }] (verified Spinco backends only), best candidate first.
 */
async function discoverDemoHosts({ timeoutMs = 8000, bonjourFactory, probe, onFound } = {}) {
  const create = bonjourFactory || (() => new (require("bonjour-service").Bonjour)());
  const verify = probe || ((url) => require("./connection-diagnostics").probeBackend(url, { timeoutMs: 3000 }));
  const found = new Map();
  let bonjour;
  try {
    bonjour = create();
    const browser = bonjour.find({ type: SERVICE_TYPE, protocol: "tcp" }, (svc) => {
      const addresses = (svc.addresses || []).filter(isPrivateIPv4);
      if (!addresses.length || !svc.port) return;
      found.set(`${svc.name}|${svc.port}`, { name: svc.name, addresses, port: svc.port, txt: svc.txt || {} });
      if (onFound) onFound(svc.name);
    });
    await new Promise((resolve) => setTimeout(resolve, timeoutMs));
    try {
      browser.stop();
    } catch {
      // already stopped
    }
  } finally {
    try {
      if (bonjour) bonjour.destroy();
    } catch {
      // best effort
    }
  }

  const verified = [];
  for (const entry of found.values()) {
    for (const address of entry.addresses) {
      const url = `http://${address}:${entry.port}`;
      const result = await verify(url);
      if (result && result.ok) {
        verified.push({ name: entry.name, url, host: `${address}:${entry.port}`, address, port: entry.port, info: result.info || {} });
        break;
      }
    }
  }
  return verified;
}

module.exports = { SERVICE_TYPE, isPrivateIPv4, parseManualAddress, discoverDemoHosts };
