"use strict";

// node --test electron/test/lan-discovery.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { isPrivateIPv4, parseManualAddress, discoverDemoHosts } = require("../lan-discovery");
const { resolveBackendMode } = require("../remote-config");

test("only private IPv4 addresses are acceptable for the demo", () => {
  for (const ip of ["10.1.2.3", "172.16.0.1", "172.31.255.1", "192.168.0.9"]) assert.equal(isPrivateIPv4(ip), true, ip);
  for (const ip of ["8.8.8.8", "172.32.0.1", "172.15.0.1", "192.169.0.1", "169.254.1.1", "127.0.0.1", "999.1.1.1", "not-an-ip", "", null]) assert.equal(isPrivateIPv4(ip), false, String(ip));
});

test("manual address: host:port on the local network only, no path or credentials", () => {
  assert.deepEqual(parseManualAddress("192.168.1.5:47831"), { ok: true, url: "http://192.168.1.5:47831", host: "192.168.1.5:47831" });
  assert.equal(parseManualAddress("http://laptop-7:47831").ok, true);
  for (const bad of ["", "192.168.1.5", "8.8.8.8:47831", "http://u:p@192.168.1.5:47831", "192.168.1.5:47831/app", "https://192.168.1.5:47831", "192.168.1.5:47831?x=1", "ftp://192.168.1.5:1"]) {
    assert.equal(parseManualAddress(bad).ok, false, bad);
  }
});

test("discovery keeps only VERIFIED hosts with private addresses, and never probes a public address", async () => {
  const probed = [];
  const services = [
    { name: "SpincoDemo-A", addresses: ["8.8.8.8", "10.0.0.7"], port: 47831, txt: {} }, // public address ignored, private one used
    { name: "Impostor", addresses: ["10.0.0.8"], port: 47831, txt: {} }, // answers, but is not a Spinco backend
    { name: "PublicOnly", addresses: ["1.2.3.4"], port: 47831, txt: {} }, // dropped
  ];
  const bonjourFactory = () => ({
    find: (_opts, onUp) => {
      services.forEach((s) => onUp(s));
      return { stop() {} };
    },
    destroy() {},
  });
  const probe = async (url) => {
    probed.push(url);
    return url.includes("10.0.0.7") ? { ok: true, info: { environment: "staging" } } : { ok: false };
  };
  const hosts = await discoverDemoHosts({ timeoutMs: 20, bonjourFactory, probe });
  assert.deepEqual(hosts.map((h) => h.url), ["http://10.0.0.7:47831"]);
  assert.deepEqual(probed.sort(), ["http://10.0.0.7:47831", "http://10.0.0.8:47831"]);
});

test("nothing found -> an empty list (the caller shows the manual-address page)", async () => {
  const bonjourFactory = () => ({ find: () => ({ stop() {} }), destroy() {} });
  assert.deepEqual(await discoverDemoHosts({ timeoutMs: 10, bonjourFactory, probe: async () => ({ ok: true }) }), []);
});

test("demo defaults are the LOWEST priority: any explicit setting wins, and the company build (no file) stays local/unconfigured", () => {
  const USER = path.join("C:", "u"), DEMO = path.join("C:", "res", "demo-defaults.json");
  const read = (files) => (p) => {
    if (p in files) return files[p];
    const e = new Error("nf");
    e.code = "ENOENT";
    throw e;
  };
  const demo = JSON.stringify({ discoverDemoHost: true });
  assert.deepEqual(resolveBackendMode({ env: {}, userDataDir: USER, demoDefaultsPath: DEMO, readFile: read({ [DEMO]: demo }) }), { mode: "discover", source: "demo-defaults" });
  assert.equal(resolveBackendMode({ env: {}, userDataDir: USER, demoDefaultsPath: DEMO, readFile: read({}) }).mode, "local"); // company build: no file
  const userFile = path.join(USER, "central-backend.json");
  const explicit = resolveBackendMode({ env: {}, userDataDir: USER, demoDefaultsPath: DEMO, readFile: read({ [DEMO]: demo, [userFile]: JSON.stringify({ backendUrl: "https://company.example" }) }) });
  assert.equal(explicit.mode, "remote");
  assert.equal(explicit.url, "https://company.example");
  assert.equal(resolveBackendMode({ env: { SPINCO_REMOTE_BACKEND_URL: "https://env.example" }, userDataDir: USER, demoDefaultsPath: DEMO, readFile: read({ [DEMO]: demo }) }).source, "env");
  assert.equal(resolveBackendMode({ env: {}, userDataDir: USER, demoDefaultsPath: DEMO, readFile: read({ [DEMO]: "{bad" }) }).mode, "invalid");
  // a user may also opt in to discovery explicitly, without a server address
  assert.equal(resolveBackendMode({ env: {}, userDataDir: USER, readFile: read({ [userFile]: demo }) }).mode, "discover");
});
