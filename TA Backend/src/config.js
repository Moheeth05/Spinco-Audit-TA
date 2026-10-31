'use strict';

const fs = require('node:fs');
const path = require('node:path');

const APP_DIR = path.resolve(__dirname, '..');
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1']);

/** Tiny .env reader (KEY=value, # comments). Real environment variables always win. */
function loadDotEnv(file = path.join(APP_DIR, '.env')) {
  if (!fs.existsSync(file)) return;
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const i = line.indexOf('=');
    if (i < 1) continue;
    const key = line.slice(0, i).trim();
    let value = line.slice(i + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    if (!(key in process.env)) process.env[key] = value;
  }
}

function numberFrom(env, key, fallback) {
  const v = env[key];
  if (v === undefined || v === '') return fallback;
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) throw new Error(`${key} must be a positive number (got "${v}")`);
  return n;
}

/** Builds and validates the configuration. Throws with a clear message when it is unsafe or invalid. */
function buildConfig(env = process.env) {
  const host = env.HOST || '127.0.0.1';
  const apiKeys = (env.TA_API_KEY || '').split(',').map((k) => k.trim()).filter(Boolean);
  const cfg = {
    host,
    port: numberFrom(env, 'PORT', 4000),
    storageDir: path.resolve(APP_DIR, env.STORAGE_DIR || 'storage'),
    apiKeys,
    maxFileBytes: Math.floor(numberFrom(env, 'MAX_FILE_MB', 25) * 1024 * 1024),
    maxRequestBytes: Math.floor(numberFrom(env, 'MAX_REQUEST_MB', 150) * 1024 * 1024),
    corsOrigin: env.CORS_ORIGIN || '',
    local: LOOPBACK.has(host),
    // Optional: copy every PDF found in this folder (e.g. the desktop app's documents folder) into the archive.
    syncFolder: env.SYNC_FOLDER ? path.resolve(env.SYNC_FOLDER) : '',
    syncIntervalMs: Math.floor(numberFrom(env, 'SYNC_INTERVAL_SECONDS', 30) * 1000),
  };
  if (cfg.syncFolder && (cfg.syncFolder === cfg.storageDir || cfg.syncFolder.startsWith(cfg.storageDir + path.sep) || cfg.storageDir.startsWith(cfg.syncFolder + path.sep))) {
    throw new Error('SYNC_FOLDER and STORAGE_DIR must not be the same folder or contain each other.');
  }
  if (!cfg.local && apiKeys.length === 0) {
    throw new Error(`Refusing to listen on ${host} without TA_API_KEY: an open upload server must never face a network. Set TA_API_KEY (16+ characters).`);
  }
  if (apiKeys.some((k) => k.length < 16)) throw new Error('Every TA_API_KEY must be at least 16 characters.');
  if (cfg.maxFileBytes > cfg.maxRequestBytes) throw new Error('MAX_FILE_MB must not exceed MAX_REQUEST_MB.');
  return cfg;
}

module.exports = { APP_DIR, loadDotEnv, buildConfig };
