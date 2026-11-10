"use strict";

// Configuration layers added for the release candidate: machine-wide file, "environment" label, BOM tolerance.
//   node --test electron/test/remote-config-layers.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { resolveBackendMode, CONFIG_FILE_NAME } = require("../remote-config");

const USER = path.join("C:", "u");
const MACHINE = path.join("C:", "ProgramData", "Spinco TA Audit");
const USER_FILE = path.join(USER, CONFIG_FILE_NAME);
const MACHINE_FILE = path.join(MACHINE, CONFIG_FILE_NAME);

function enoent() {
  const e = new Error("nf");
  e.code = "ENOENT";
  return e;
}
function resolve({ env = {}, user, machine } = {}) {
  const files = { [USER_FILE]: user, [MACHINE_FILE]: machine };
  return resolveBackendMode({
    env,
    userDataDir: USER,
    machineDataDir: MACHINE,
    readFile: (p) => {
      if (!(p in files)) throw new Error("unexpected path " + p);
      if (files[p] === undefined) throw enoent();
      if (files[p] instanceof Error) throw files[p];
      return files[p];
    },
  });
}
const f = (o) => JSON.stringify(o);

test("only a machine-wide file -> remote, source machine-file", () => {
  const r = resolve({ machine: f({ backendUrl: "https://spinco.corp.example", environment: "production" }) });
  assert.equal(r.mode, "remote");
  assert.equal(r.source, "machine-file");
  assert.equal(r.environment, "production");
  assert.equal(r.url, "https://spinco.corp.example");
});

test("the per-user file wins over the machine-wide file", () => {
  const r = resolve({ user: f({ backendUrl: "https://user.example" }), machine: f({ backendUrl: "https://machine.example" }) });
  assert.equal(r.url, "https://user.example");
  assert.equal(r.source, "file");
});

test("the environment variable wins over both files", () => {
  const r = resolve({ env: { SPINCO_REMOTE_BACKEND_URL: "https://env.example" }, user: f({ backendUrl: "https://user.example" }), machine: f({ backendUrl: "https://machine.example" }) });
  assert.equal(r.url, "https://env.example");
  assert.equal(r.source, "env");
});

test("an unusable machine-wide file is an ERROR, never a silent fall-through to local mode", () => {
  for (const machine of ["{not json", f({ nope: 1 }), f({ backendUrl: "ftp://x" }), f(["a"])]) {
    assert.equal(resolve({ machine }).mode, "invalid", machine);
  }
  assert.equal(resolve({ machine: Object.assign(new Error("denied"), { code: "EACCES" }) }).mode, "invalid");
});

test("an unusable per-user file is an ERROR even when a valid machine-wide file exists", () => {
  const r = resolve({ user: "{bad", machine: f({ backendUrl: "https://machine.example" }) });
  assert.equal(r.mode, "invalid");
});

test("neither file -> local", () => {
  assert.deepEqual(resolve(), { mode: "local" });
});

test("environment must be one of development / staging / production (case-insensitive) or absent", () => {
  assert.equal(resolve({ user: f({ backendUrl: "https://a.example", environment: "Staging" }) }).environment, "staging");
  assert.equal(resolve({ user: f({ backendUrl: "https://a.example" }) }).environment, null);
  for (const environment of ["prod", "", 5, null, "test"]) {
    assert.equal(resolve({ user: f({ backendUrl: "https://a.example", environment }) }).mode, "invalid", String(environment));
  }
});

test("production with plain HTTP is refused even when allowInsecureDevelopment is set", () => {
  const r = resolve({ user: f({ backendUrl: "http://pc1.corp:8001", allowInsecureDevelopment: true, environment: "production" }) });
  assert.equal(r.mode, "invalid");
  assert.match(r.reason, /HTTPS/);
  // ... while staging over plain HTTP with the explicit flag keeps working (the PC-to-PC test setup)
  const s = resolve({ user: f({ backendUrl: "http://pc1.corp:8001", allowInsecureDevelopment: true, environment: "staging" }) });
  assert.equal(s.mode, "remote");
  assert.equal(s.insecure, true);
});

test("a UTF-8 byte-order mark (written by Windows PowerShell 5) is tolerated", () => {
  const r = resolve({ user: "﻿" + f({ backendUrl: "https://bom.example" }) });
  assert.equal(r.mode, "remote");
  assert.equal(r.url, "https://bom.example");
});

test("reasons never echo the configured URL or the file contents", () => {
  const secretish = "https://user:hunter2@host.example";
  const r = resolve({ user: f({ backendUrl: secretish }) });
  assert.equal(r.mode, "invalid");
  assert.ok(!r.reason.includes("hunter2"));
  assert.ok(!r.reason.includes("host.example"));
});
