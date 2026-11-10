"""Read-only CONFIGURATION CHECK for the central backend: what is set, what is missing, what is unsafe - before the backend is started.

    python tools/check_central_config.py            # reads the environment of this process (tools/Start-SpincoBackend.ps1 -CheckOnly loads the settings file first)
    python tools/check_central_config.py --connect  # ... and also opens ONE connection to the configured database (SELECT 1) and checks that its schema is ready

It starts nothing, creates nothing, changes nothing, makes NO call to Google and never prints a secret: for credentials it only says whether the file the setting names exists.
Output: one line per item - PASS, WARN (works, but look at it) or FAIL (the backend would refuse to start, or would not do what production needs). Exit code 1 if any FAIL.
"""

import argparse
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

ROWS: list[tuple[str, str, str]] = []


def row(status: str, item: str, detail: str = "") -> None:
    ROWS.append((status, item, detail))


def _dir_writable(raw: str) -> tuple[bool, str]:
    path = Path(raw)
    if path.is_dir():
        return (os.access(path, os.W_OK), "exists")
    parent = path.parent
    if parent.is_dir() and os.access(parent, os.W_OK):
        return True, "does not exist yet (its parent is writable, the backend creates it)"
    return False, "neither the folder nor a writable parent exists"


def run(connect: bool) -> int:
    env = os.environ

    # 1. environment name ------------------------------------------------------------------------------------------------------------
    name = str(env.get("SPINCO_ENVIRONMENT", "")).strip().lower()
    if name in ("dev", "staging", "production"):
        row("PASS", "SPINCO_ENVIRONMENT", name)
    else:
        row("FAIL", "SPINCO_ENVIRONMENT", "must be dev, staging or production")
    production = name == "production"

    # 2. network -----------------------------------------------------------------------------------------------------------------------
    try:
        from app.services import central_storage, hardening, startup_checks
    except Exception as exc:  # noqa: BLE001 - e.g. the database settings are unusable: say so without a traceback (the text of DatabaseConfigError is secret-free)
        reason = str(exc) if exc.__class__.__name__ == "DatabaseConfigError" else "the application settings could not be loaded"
        print(f"FAIL  database  {reason}\n\n0 PASS, 0 WARN, 1 FAIL (the checks after this one could not run)")
        return 1

    host = startup_checks.bind_host(env)
    port = str(env.get("SPINCO_BACKEND_PORT", "8001")).strip()
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        row("FAIL", "SPINCO_BACKEND_PORT", "must be a port number")
    else:
        row("PASS", "bind address / port", f"{host or '127.0.0.1'}:{port}" + (" (network-reachable)" if startup_checks.network_exposed(env) else " (this machine only - put the HTTPS proxy in front)"))
    hosts = startup_checks.allowed_hosts(env)
    if hosts:
        ip_like = [h for h in hosts if h.replace(".", "").isdigit()]
        row("WARN" if ip_like else "PASS", "SPINCO_ALLOWED_HOSTS", ("contains an IP address - use host names" if ip_like else f"{len(hosts)} host name(s)"))
    else:
        row("FAIL" if production else "WARN", "SPINCO_ALLOWED_HOSTS", "not set: any Host header is accepted")
    try:
        origins = hardening.cors_origins(env)
        row("PASS", "SPINCO_CORS_ORIGINS", f"{len(origins)} origin(s)" if origins else "none (same-origin only)")
    except hardening.CorsConfigError as exc:
        row("FAIL", "SPINCO_CORS_ORIGINS", str(exc))

    # 3. HTTPS / admin ----------------------------------------------------------------------------------------------------------------
    secure = str(env.get("SPINCO_COOKIE_SECURE", "")).strip().lower() == "true"
    insecure_ack = str(env.get("SPINCO_ALLOW_INSECURE_HTTP", "")).strip().lower() == "true"
    if secure:
        row("PASS", "SPINCO_COOKIE_SECURE", "true (HTTPS cookies)")
    elif production:
        row("FAIL", "SPINCO_COOKIE_SECURE", "must be true in production")
    elif startup_checks.network_exposed(env):
        row("WARN", "SPINCO_COOKIE_SECURE", "not set: plain-HTTP acknowledgement needed" + (" (given)" if insecure_ack else " (MISSING: the backend will refuse to start)"))
    else:
        row("PASS", "SPINCO_COOKIE_SECURE", "not needed on a loopback-only backend")
    if insecure_ack:
        row("FAIL" if production else "WARN", "SPINCO_ALLOW_INSECURE_HTTP", "set: DEVELOPMENT / synthetic documents only" + (" - refused in production" if production else ""))
    pw = env.get("SPINCO_ADMIN_INITIAL_PASSWORD")
    if pw is None or pw == "":
        row("WARN", "SPINCO_ADMIN_INITIAL_PASSWORD", "not set (fine if the administrator already exists; required for a network-reachable / production backend)")
    elif len(pw) < 12 or pw == startup_checks.DEFAULT_ADMIN_PASSWORD:
        row("FAIL", "SPINCO_ADMIN_INITIAL_PASSWORD", "must be at least 12 characters and not the development default")
    else:
        row("PASS", "SPINCO_ADMIN_INITIAL_PASSWORD", "set (value not shown)")

    # 4. database ---------------------------------------------------------------------------------------------------------------------
    try:
        from app import database

        postgres = not database.IS_SQLITE
        if postgres:
            row("PASS", "database", "PostgreSQL (credentials not shown)")
        else:
            row("FAIL" if production and not str(env.get("SPINCO_ALLOW_SQLITE_IN_PRODUCTION", "")).strip().lower() == "true" else "WARN", "database", f"SQLite file {database.DB_PATH.name}: fine for a pilot, not for many concurrent users")
        if connect:
            from sqlalchemy import text

            try:
                with database.engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                row("PASS", "database connection", "SELECT 1 succeeded")
                problems = central_storage.schema_problems()
                row("PASS" if not problems else "FAIL", "database schema", "ready for central storage" if not problems else "; ".join(problems) + " - create/migrate it with tools/init_central_staging_db.py or alembic")
            except Exception:  # noqa: BLE001 - the driver text can contain the host and user name
                row("FAIL", "database connection", "could not connect with the configured URL")
        else:
            row("PASS", "database connection", "not tested (use --connect)")
    except Exception as exc:  # noqa: BLE001
        row("FAIL", "database", exc.__class__.__name__ + ": " + (str(exc) if exc.__class__.__name__ == "DatabaseConfigError" else "invalid settings"))

    # 5. folders ----------------------------------------------------------------------------------------------------------------------
    dist = str(env.get("SPINCO_FRONTEND_DIST", "")).strip()
    if dist and (Path(dist) / "index.html").is_file():
        row("PASS", "SPINCO_FRONTEND_DIST", "index.html found")
    else:
        row("FAIL", "SPINCO_FRONTEND_DIST", "must be the folder that contains the built frontend's index.html")
    for setting, required in (("SPINCO_STORAGE_ROOT", True), ("SPINCO_GDRIVE_SPOOL_DIR", production or str(env.get("SPINCO_CENTRAL_STORAGE_MODE", "")).strip().lower() in ("serve", "shadow")), ("SPINCO_LOG_DIR", False)):
        raw = str(env.get(setting, "")).strip()
        if not raw:
            row("WARN" if required else "PASS", setting, "not set" + (" (a default inside the application folder is used)" if required else " (console logging only)"))
            continue
        ok, detail = _dir_writable(raw)
        row("PASS" if ok else "FAIL", setting, detail if ok else "is not writable: " + detail)

    # 6. central storage & Google -------------------------------------------------------------------------------------------------
    central = central_storage.config(env)
    if central.error:
        row("FAIL", "central storage settings", central.error)
    row("PASS" if central.mode == central_storage.SERVE else ("FAIL" if production else "WARN"), "SPINCO_CENTRAL_STORAGE_MODE", f"{central.mode}" + ("" if central.mode == central_storage.SERVE else " (employees' uploads would NOT be authoritative in Google Drive)"))
    if central.active:
        from app.services.drive import config as drive_config

        try:
            cfg = drive_config.load_runtime_config(dict(env))
            row("PASS", "Google Drive configuration", f"auth mode {cfg.auth_mode}; environment {cfg.environment}; expected Shared Drive configured; credential file(s) exist (not opened)")
        except drive_config.DriveConfigError as exc:
            row("FAIL", "Google Drive configuration", str(exc))
        if str(env.get("SPINCO_GDRIVE_ENABLED", "")).strip().lower() not in ("1", "true", "yes", "on"):
            row("FAIL", "SPINCO_GDRIVE_ENABLED", "must be true when central storage is on")
    elif production:
        row("FAIL", "Google Drive configuration", "central storage is off")

    # 7. production rule summary ----------------------------------------------------------------------------------------------------
    if production:
        problems = startup_checks.production_problems(env)
        row("PASS" if not problems else "FAIL", "production start-up rules", "all satisfied" if not problems else "; ".join(problems))

    width = max(len(r[1]) for r in ROWS)
    for status, item, detail in ROWS:
        print(f"{status:<5} {item:<{width}}  {detail}")
    failed = sum(1 for r in ROWS if r[0] == "FAIL")
    warned = sum(1 for r in ROWS if r[0] == "WARN")
    print(f"\n{len(ROWS) - failed - warned} PASS, {warned} WARN, {failed} FAIL")
    return 1 if failed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--connect", action="store_true", help="also open one database connection and check the schema")
    args = parser.parse_args(argv)
    return run(args.connect)


if __name__ == "__main__":
    raise SystemExit(main())
