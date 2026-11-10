"use strict";

/**
 * Pure (no Electron imports) connection-mode resolution for the desktop shell,
 * so it can be unit tested with plain Node (see electron/test/).
 *
 * Resolution order, evaluated at startup with no rebuild required:
 *   1. env var SPINCO_REMOTE_BACKEND_URL
 *   2. JSON file <userData>/central-backend.json:
 *        { "backendUrl": "https://host:port", "allowInsecureDevelopment": true }
 *   3. neither present -> local (standalone) mode, today's behaviour.
 *
 * Result shapes:
 *   { mode: "local" }
 *   { mode: "remote", url, host, insecure, source }   (url = bare origin, no trailing slash)
 *   { mode: "invalid", reason }
 *
 * Security notes:
 *   - This file never reads or accepts secrets. URLs containing credentials
 *     are rejected, and no `reason` string ever echoes the raw URL or the file
 *     contents, so nothing sensitive can reach a dialog or a log through here.
 *   - Plain http is accepted only for loopback hosts (traffic never leaves the
 *     machine) or when the config FILE sets allowInsecureDevelopment: true
 *     (flagged insecure:true so the shell can warn). The env var alone can
 *     never enable non-loopback http.
 */

const path = require("node:path");

const ENV_VAR = "SPINCO_REMOTE_BACKEND_URL";
const CONFIG_FILE_NAME = "central-backend.json";

const BLOCKED_ENV_PREFIX = "SPINCO_GDRIVE_";
const BLOCKED_ENV_EXACT = "SPINCO_CENTRAL_STORAGE_MODE";

function isLoopbackHostname(hostname) {
  const h = String(hostname || "").toLowerCase();
  if (h === "localhost") return true;
  if (h === "[::1]" || h === "::1") return true;
  // Whole 127.0.0.0/8 block. WHATWG URL already normalises odd numeric forms
  // (e.g. 0x7f.1) to dotted-quad, so a strict dotted-quad match is sufficient.
  const m = /^127\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
  return !!m && [m[1], m[2], m[3]].every((n) => Number(n) <= 255);
}

function invalid(reason) {
  return { mode: "invalid", reason };
}

function validateBackendUrl(raw, { allowInsecureDevelopment = false, source }) {
  if (typeof raw !== "string" || raw.trim() === "") {
    return invalid("The backend URL must be a non-empty string.");
  }
  const text = raw.trim();

  // Credentials: reject on the raw text too, so "https://@host" or odd
  // encodings can't slip past the parsed username/password check.
  if (/^[a-z][a-z0-9+.-]*:\/\/[^/?#]*@/i.test(text)) {
    return invalid("The backend URL must not contain credentials (user:password@).");
  }

  let parsed;
  try {
    parsed = new URL(text);
  } catch {
    return invalid("The backend URL is not a valid URL.");
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return invalid("The backend URL must use http or https.");
  }
  if (parsed.username || parsed.password) {
    return invalid("The backend URL must not contain credentials (user:password@).");
  }
  if (!parsed.hostname) {
    return invalid("The backend URL must include a host name.");
  }
  if (text.includes("?") || text.includes("#") || parsed.search || parsed.hash) {
    return invalid("The backend URL must not contain a query string or fragment.");
  }
  if (parsed.pathname !== "/") {
    return invalid("The backend URL must be a bare origin (scheme, host, optional port) with no path.");
  }

  let insecure = false;
  if (parsed.protocol === "http:" && !isLoopbackHostname(parsed.hostname)) {
    if (allowInsecureDevelopment === true) {
      insecure = true;
    } else {
      return invalid("HTTPS is required for a non-local backend.");
    }
  }

  return {
    mode: "remote",
    url: parsed.origin, // scheme://host[:port], default port elided, no trailing slash
    host: parsed.host,
    insecure,
    source,
  };
}

const ENVIRONMENTS = ["development", "staging", "production"];

/** Reads and validates ONE config file. Returns null when the file does not exist, otherwise a mode result. */
function readConfigFile(filePath, label, readFile) {
  let text;
  try {
    text = readFile(filePath);
  } catch (err) {
    if (err && err.code === "ENOENT") return null;
    return invalid(`${label} exists but could not be read.`);
  }

  let data;
  try {
    data = JSON.parse(String(text).replace(/^\uFEFF/, "")); // tolerate a UTF-8 BOM (PowerShell 5 writes one)
  } catch {
    return invalid(`${label} is not valid JSON.`);
  }
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    return invalid(`${label} must contain a JSON object.`);
  }
  if (data.discoverDemoHost === true && data.backendUrl === undefined) {
    return { mode: "discover", source: "file", configPath: filePath }; // LAN demo: look for a demo host on the local network (see lan-discovery.js)
  }
  if (typeof data.backendUrl !== "string") {
    return invalid(`${label} must contain a "backendUrl" string.`);
  }
  if (data.allowInsecureDevelopment !== undefined && typeof data.allowInsecureDevelopment !== "boolean") {
    return invalid(`"allowInsecureDevelopment" in ${label} must be true or false.`);
  }
  let environment = null;
  if (data.environment !== undefined) {
    environment = typeof data.environment === "string" ? data.environment.trim().toLowerCase() : "";
    if (!ENVIRONMENTS.includes(environment)) {
      return invalid(`"environment" in ${label} must be one of: ${ENVIRONMENTS.join(", ")}.`);
    }
  }

  const result = validateBackendUrl(data.backendUrl, {
    allowInsecureDevelopment: data.allowInsecureDevelopment === true,
    source: "file",
  });
  if (result.mode !== "remote") return result;
  if (environment === "production" && result.insecure) {
    return invalid("A production server must use HTTPS (plain HTTP is for development with synthetic data only).");
  }
  return { ...result, environment, configPath: filePath };
}

/**
 * Resolution order (first match wins; a file that exists but is unusable is an ERROR, never a silent fall-through):
 *   1. env var SPINCO_REMOTE_BACKEND_URL
 *   2. per-user file   <userData>/central-backend.json
 *   3. machine-wide file <machineDataDir>/central-backend.json   (e.g. %PROGRAMDATA%\Spinco TA Audit - IT can deploy it to every PC)
 *   4. none of them -> local (standalone) mode
 */
function resolveBackendMode({ env, userDataDir, machineDataDir, demoDefaultsPath, readFile } = {}) {
  const envValue = env && env[ENV_VAR];
  if (typeof envValue === "string" && envValue.trim() !== "") {
    return validateBackendUrl(envValue, { allowInsecureDevelopment: false, source: "env" });
  }

  if (typeof readFile !== "function") return { mode: "local" };

  if (userDataDir) {
    const userResult = readConfigFile(path.join(userDataDir, CONFIG_FILE_NAME), `${CONFIG_FILE_NAME}`, readFile);
    if (userResult) return userResult;
  }
  if (machineDataDir) {
    const machineResult = readConfigFile(path.join(machineDataDir, CONFIG_FILE_NAME), `The machine-wide ${CONFIG_FILE_NAME}`, readFile);
    if (machineResult) return { ...machineResult, source: machineResult.mode === "remote" ? "machine-file" : machineResult.source };
  }
  // Lowest priority: a file bundled ONLY into the LAN-demo build. Every explicit setting above wins; the company build does not contain it.
  if (demoDefaultsPath) {
    let text = null;
    try {
      text = readFile(demoDefaultsPath);
    } catch (err) {
      if (!(err && err.code === "ENOENT")) return invalid("The bundled demo defaults file could not be read.");
    }
    if (text !== null) {
      try {
        if (JSON.parse(String(text).replace(/^﻿/, "")).discoverDemoHost === true) return { mode: "discover", source: "demo-defaults" };
      } catch {
        return invalid("The bundled demo defaults file is not valid JSON.");
      }
    }
  }
  return { mode: "local" };
}
// Windows environment variable names are case-insensitive, and a spread copy
// of process.env keeps whatever case was originally set, so match on the
// upper-cased name.
function isBlockedEnvName(name) {
  const upper = String(name).toUpperCase();
  return upper.startsWith(BLOCKED_ENV_PREFIX) || upper === BLOCKED_ENV_EXACT;
}

/** Returns a copy of `env` without Google Drive / central-storage variables. */
function sanitizeChildEnv(env) {
  const out = {};
  for (const [key, value] of Object.entries(env || {})) {
    if (!isBlockedEnvName(key)) out[key] = value;
  }
  return out;
}

/** Deletes the blocked variables from `env` in place (e.g. process.env). */
function scrubEnvInPlace(env) {
  for (const key of Object.keys(env)) {
    if (isBlockedEnvName(key)) delete env[key];
  }
  return env;
}

module.exports = {
  ENV_VAR,
  CONFIG_FILE_NAME,
  ENVIRONMENTS,
  resolveBackendMode,
  validateBackendUrl,
  isLoopbackHostname,
  isBlockedEnvName,
  sanitizeChildEnv,
  scrubEnvInPlace,
};
