#!/usr/bin/env node
'use strict';

const { loadDotEnv, buildConfig } = require('./src/config');
const { createServer } = require('./src/server');

loadDotEnv();
let cfg;
try {
  cfg = buildConfig();
} catch (err) {
  console.error(`TA Backend: ${err.message}`);
  process.exit(1);
}

const { server, store, syncer } = createServer(cfg);
server.listen(cfg.port, cfg.host, () => {
  const shown = cfg.host === '0.0.0.0' ? 'localhost' : cfg.host;
  console.log(`TA Backend listening on http://${shown}:${cfg.port}  (bind ${cfg.host})`);
  console.log(`  storage : ${cfg.storageDir}   (${store.stats().documents} document(s) already archived)`);
  console.log(`  auth    : ${cfg.apiKeys.length ? `API key required (${cfg.apiKeys.length} key(s))` : 'OFF (loopback only)'}`);
  console.log(`  limits  : ${Math.round(cfg.maxFileBytes / 1048576)} MB per file, ${Math.round(cfg.maxRequestBytes / 1048576)} MB per request`);
  if (cfg.syncFolder) {
    console.log(`  sync    : copying PDFs from ${cfg.syncFolder} every ${Math.round(cfg.syncIntervalMs / 1000)} s (source folder is only read)`);
    if (!require('node:fs').existsSync(cfg.syncFolder)) console.log('  sync    : WARNING - that folder does not exist (yet); it will be picked up when it appears');
    syncer.start();
  } else {
    console.log('  sync    : off (set SYNC_FOLDER to copy the desktop app\'s uploaded PDFs into this storage)');
  }
});

function shutdown(signal) {
  console.log(`\n${signal} received, finishing in-flight requests...`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 10000).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
