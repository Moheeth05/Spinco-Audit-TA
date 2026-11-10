"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");
const {
  resolveBackendMode,
  sanitizeChildEnv,
  scrubEnvInPlace,
  CONFIG_FILE_NAME,
} = require("../remote-config");

const USER_DATA = path.join("C:", "fake", "userData");
const CONFIG_PATH = path.join(USER_DATA, CONFIG_FILE_NAME);

function enoent() {
  const err = new Error("not found");
  err.code = "ENOENT";
  return err;
}

// readFile stub: serves `fileText` only for the expected config path.
function resolve({ env = {}, fileText } = {}) {
  return resolveBackendMode({
    env,
    userDataDir: USER_DATA,
    readFile: (p) => {
      assert.strictEqual(p, CONFIG_PATH);
      if (fileText === undefined) throw enoent();
      return fileText;
    },
  });
}

const fileWith = (obj) => JSON.stringify(obj);

test("no config -> local", () => {
  assert.deepStrictEqual(resolve(), { mode: "local" });
});

test("blank env var and no file -> local", () => {
  assert.deepStrictEqual(resolve({ env: { SPINCO_REMOTE_BACKEND_URL: "   " } }), { mode: "local" });
});

test("env var with valid https URL -> remote", () => {
  const r = resolve({ env: { SPINCO_REMOTE_BACKEND_URL: "https://central.example.com:8443" } });
  assert.strictEqual(r.mode, "remote");
  assert.strictEqual(r.url, "https://central.example.com:8443");
  assert.strictEqual(r.host, "central.example.com:8443");
  assert.strictEqual(r.insecure, false);
  assert.strictEqual(r.source, "env");
});

test("env var with loopback http -> remote, not insecure", () => {
  for (const url of ["http://localhost:8001", "http://127.0.0.1:8001", "http://127.5.6.7"]) {
    const r = resolve({ env: { SPINCO_REMOTE_BACKEND_URL: url } });
    assert.strictEqual(r.mode, "remote", url);
    assert.strictEqual(r.insecure, false, url);
  }
});

test("IPv6 loopback http -> remote, not insecure", () => {
  const r = resolve({ env: { SPINCO_REMOTE_BACKEND_URL: "http://[::1]:8001" } });
  assert.strictEqual(r.mode, "remote");
  assert.strictEqual(r.url, "http://[::1]:8001");
  assert.strictEqual(r.insecure, false);
});

test("non-loopback http is rejected from env var", () => {
  const r = resolve({ env: { SPINCO_REMOTE_BACKEND_URL: "http://central.example.com" } });
  assert.strictEqual(r.mode, "invalid");
  assert.match(r.reason, /HTTPS is required for a non-local backend/);
});

test("non-loopback http is rejected from file without allowInsecureDevelopment", () => {
  const r = resolve({ fileText: fileWith({ backendUrl: "http://10.0.0.5:8001" }) });
  assert.strictEqual(r.mode, "invalid");
  assert.match(r.reason, /HTTPS is required for a non-local backend/);
  const r2 = resolve({ fileText: fileWith({ backendUrl: "http://10.0.0.5:8001", allowInsecureDevelopment: false }) });
  assert.strictEqual(r2.mode, "invalid");
});

test("non-loopback http is accepted from file with allowInsecureDevelopment -> insecure", () => {
  const r = resolve({ fileText: fileWith({ backendUrl: "http://10.0.0.5:8001", allowInsecureDevelopment: true }) });
  assert.strictEqual(r.mode, "remote");
  assert.strictEqual(r.insecure, true);
  assert.strictEqual(r.url, "http://10.0.0.5:8001");
  assert.strictEqual(r.source, "file");
});

test("allowInsecureDevelopment must be a boolean", () => {
  const r = resolve({ fileText: fileWith({ backendUrl: "http://10.0.0.5", allowInsecureDevelopment: "yes" }) });
  assert.strictEqual(r.mode, "invalid");
});

test("URL with credentials is rejected, and the reason does not echo them", () => {
  for (const url of [
    "https://user:hunter2@central.example.com",
    "https://user@central.example.com",
    "https://@central.example.com",
  ]) {
    const r = resolve({ env: { SPINCO_REMOTE_BACKEND_URL: url } });
    assert.strictEqual(r.mode, "invalid", url);
    assert.match(r.reason, /credentials/);
    assert.doesNotMatch(r.reason, /hunter2/);
  }
  const f = resolve({ fileText: fileWith({ backendUrl: "https://a:b@central.example.com" }) });
  assert.strictEqual(f.mode, "invalid");
});

test("path, query and fragment are rejected", () => {
  for (const url of [
    "https://central.example.com/app",
    "https://central.example.com/api/health",
    "https://central.example.com//",
    "https://central.example.com/?x=1",
    "https://central.example.com?x=1",
    "https://central.example.com/#frag",
    "https://central.example.com/?",
  ]) {
    const r = resolve({ env: { SPINCO_REMOTE_BACKEND_URL: url } });
    assert.strictEqual(r.mode, "invalid", url);
  }
});

test("non-http(s) schemes and garbage URLs are rejected", () => {
  for (const url of ["ftp://central.example.com", "file:///C:/x", "javascript:alert(1)", "not a url", "localhost:5200", "https://"]) {
    const r = resolve({ env: { SPINCO_REMOTE_BACKEND_URL: url } });
    assert.strictEqual(r.mode, "invalid", url);
  }
});

test("garbage / non-JSON / wrong-shape file -> invalid (never local)", () => {
  for (const text of ["", "not json at all", "{broken", "[]", "null", "42", fileWith({}), fileWith({ backendUrl: 5 })]) {
    const r = resolve({ fileText: text });
    assert.strictEqual(r.mode, "invalid", JSON.stringify(text));
    assert.ok(r.reason && typeof r.reason === "string");
  }
});

test("invalid reason never contains file contents", () => {
  const r = resolve({ fileText: "SECRET-TOKEN-123 {{" });
  assert.strictEqual(r.mode, "invalid");
  assert.doesNotMatch(r.reason, /SECRET-TOKEN-123/);
});

test("unreadable file (non-ENOENT error) -> invalid, not local", () => {
  const r = resolveBackendMode({
    env: {},
    userDataDir: USER_DATA,
    readFile: () => {
      const err = new Error("denied");
      err.code = "EACCES";
      throw err;
    },
  });
  assert.strictEqual(r.mode, "invalid");
});

test("env var wins over file, including an invalid env value over a valid file", () => {
  const file = fileWith({ backendUrl: "https://from-file.example.com" });
  const r = resolve({ env: { SPINCO_REMOTE_BACKEND_URL: "https://from-env.example.com" }, fileText: file });
  assert.strictEqual(r.mode, "remote");
  assert.strictEqual(r.url, "https://from-env.example.com");
  assert.strictEqual(r.source, "env");

  const bad = resolve({ env: { SPINCO_REMOTE_BACKEND_URL: "http://example.com" }, fileText: file });
  assert.strictEqual(bad.mode, "invalid");
});

test("trailing slash is normalised away", () => {
  const r = resolve({ env: { SPINCO_REMOTE_BACKEND_URL: "https://central.example.com/" } });
  assert.strictEqual(r.mode, "remote");
  assert.strictEqual(r.url, "https://central.example.com");
  const f = resolve({ fileText: fileWith({ backendUrl: "  https://central.example.com:9443/  " }) });
  assert.strictEqual(f.url, "https://central.example.com:9443");
});

test("sanitizeChildEnv removes SPINCO_GDRIVE_* and SPINCO_CENTRAL_STORAGE_MODE only", () => {
  const input = {
    PATH: "/bin",
    SPINCO_DB_PATH: "x",
    SPINCO_GDRIVE_CREDENTIALS_FILE: "C:\\secret\\sa.json",
    SPINCO_GDRIVE_ROOT_FOLDER_ID: "abc",
    SPINCO_CENTRAL_STORAGE_MODE: "drive",
    spinco_gdrive_lowercase: "y",
    Spinco_Central_Storage_Mode: "drive",
    SPINCO_CENTRAL_STORAGE_MODE_EXTRA: "keep-me",
  };
  const copy = { ...input };
  const out = sanitizeChildEnv(input);
  assert.deepStrictEqual(out, {
    PATH: "/bin",
    SPINCO_DB_PATH: "x",
    SPINCO_CENTRAL_STORAGE_MODE_EXTRA: "keep-me",
  });
  assert.deepStrictEqual(input, copy, "input must not be mutated");
});

test("scrubEnvInPlace removes the blocked variables from the given object", () => {
  const env = { A: "1", SPINCO_GDRIVE_X: "2", SPINCO_CENTRAL_STORAGE_MODE: "drive" };
  scrubEnvInPlace(env);
  assert.deepStrictEqual(env, { A: "1" });
});
