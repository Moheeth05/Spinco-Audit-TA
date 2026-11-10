"""Start-up safety checks for the backend when it can be reached over a network (central backend mode).

The packaged desktop app keeps binding to 127.0.0.1 and none of this applies. When the backend is told to listen on another address (SPINCO_BIND_HOST) it becomes a
SHARED server, and a shared server must not run with development shortcuts:

  1. The initial administrator password must be set explicitly (SPINCO_ADMIN_INITIAL_PASSWORD) and must not be the development default; an existing account that still has
     the development default password stops the start-up (it must be changed first).
  2. Plain HTTP is refused unless an operator acknowledges it (SPINCO_ALLOW_INSECURE_HTTP=true). The acknowledgement is allowed ONLY for development / synthetic documents:
     the status endpoint then reports a security warning to administrators. HTTPS (a reverse proxy or a TLS-terminating listener) with SPINCO_COOKIE_SECURE=true is required
     before any real account or document is used.
  3. SPINCO_ALLOWED_HOSTS (host NAMES, not IP addresses) restricts the Host header; without it a warning is reported.

  4. PRODUCTION (SPINCO_ENVIRONMENT=production) is stricter and applies even behind a reverse proxy on loopback: HTTPS cookies are mandatory, the plain-HTTP acknowledgement
     is refused, the allowed host names must be set, central storage must be `serve`, and SQLite is refused unless SPINCO_ALLOW_SQLITE_IN_PRODUCTION=true is set on purpose.

Nothing here prints or logs a password, token or key. A failed check raises StartupSecurityError and the application does not start (fail closed).
"""

import os

from sqlalchemy import select

LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}
DEFAULT_ADMIN_PASSWORD = "ChangeMe123!"  # the development default in app/seed.py; it must never operate on a reachable server


class StartupSecurityError(RuntimeError):
    pass


def bind_host(env=None) -> str:
    env = os.environ if env is None else env
    return str(env.get("SPINCO_BIND_HOST", "127.0.0.1")).strip()


def network_exposed(env=None) -> bool:
    return bind_host(env).lower() not in LOOPBACK


def _flag(env, name: str) -> bool:
    return str(env.get(name, "")).strip().lower() == "true"


def is_production(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("SPINCO_ENVIRONMENT", "")).strip().lower() == "production"


def uses_postgres(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(str(env.get("SPINCO_DATABASE_URL", "")).strip() or str(env.get("SPINCO_DATABASE_URL_FILE", "")).strip())


def production_problems(env=None) -> list[str]:
    """What is wrong for a PRODUCTION backend, as setting names only (never a value). Empty = acceptable."""
    env = os.environ if env is None else env
    problems: list[str] = []
    if not _flag(env, "SPINCO_COOKIE_SECURE"):
        problems.append("SPINCO_COOKIE_SECURE=true is required in production (serve the backend over HTTPS)")
    if _flag(env, "SPINCO_ALLOW_INSECURE_HTTP"):
        problems.append("SPINCO_ALLOW_INSECURE_HTTP must not be set in production")
    if not allowed_hosts(env):
        problems.append("SPINCO_ALLOWED_HOSTS must list the server host name(s) in production")
    from app.services import central_storage

    if central_storage.config(env).mode != central_storage.SERVE:
        problems.append("SPINCO_CENTRAL_STORAGE_MODE must be serve in production (Google Drive is the authoritative store)")
    if not uses_postgres(env) and not _flag(env, "SPINCO_ALLOW_SQLITE_IN_PRODUCTION"):
        problems.append("production needs PostgreSQL (SPINCO_DATABASE_URL_FILE); SQLite is refused unless SPINCO_ALLOW_SQLITE_IN_PRODUCTION=true is set on purpose")
    return problems


def allowed_hosts(env=None) -> list[str]:
    env = os.environ if env is None else env
    return [h.strip() for h in str(env.get("SPINCO_ALLOWED_HOSTS", "")).split(",") if h.strip()]


def warnings(env=None) -> list[str]:
    """What an administrator is shown on the status page. Empty on a loopback-only (local) backend."""
    env = os.environ if env is None else env
    out: list[str] = []
    if not network_exposed(env):
        return out
    if not _flag(env, "SPINCO_COOKIE_SECURE"):
        out.append("INSECURE: this backend is reachable over a network without HTTPS. Use synthetic / test documents only until HTTPS is in place.")
    if not allowed_hosts(env):
        out.append("SPINCO_ALLOWED_HOSTS is not set: any Host header is accepted. Set it to the server's host name.")
    for host in allowed_hosts(env):
        if host.replace(".", "").isdigit():
            out.append("SPINCO_ALLOWED_HOSTS contains an IP address; use the host name so the address can change without a client update.")
            break
    return out


def run_checks(db=None, env=None) -> None:
    """Raises StartupSecurityError when a network-reachable backend would start with a development shortcut. Does nothing for a loopback-only backend."""
    env = os.environ if env is None else env
    production = is_production(env)
    if not network_exposed(env) and not production:
        return
    password = env.get("SPINCO_ADMIN_INITIAL_PASSWORD")
    if not password or password == DEFAULT_ADMIN_PASSWORD or len(password) < 12:
        raise StartupSecurityError(
            "This backend listens on a network address (SPINCO_BIND_HOST). Set SPINCO_ADMIN_INITIAL_PASSWORD to an explicit password of at least 12 characters "
            "(not the development default) before starting it."
        )
    if production:
        problems = production_problems(env)
        if problems:
            raise StartupSecurityError("This backend is configured as PRODUCTION but is not safe to start: " + "; ".join(problems) + ".")
    elif not _flag(env, "SPINCO_COOKIE_SECURE") and not _flag(env, "SPINCO_ALLOW_INSECURE_HTTP"):
        raise StartupSecurityError(
            "This backend listens on a network address without HTTPS. Serve it over HTTPS and set SPINCO_COOKIE_SECURE=true; for DEVELOPMENT with synthetic documents "
            "only, set SPINCO_ALLOW_INSECURE_HTTP=true to acknowledge the risk."
        )
    if db is not None:
        from app.models.user import User
        from app.services.auth import verify_password

        for user in db.execute(select(User).where(User.password_hash.is_not(None))).scalars():
            try:
                if verify_password(DEFAULT_ADMIN_PASSWORD, user.password_hash):
                    raise StartupSecurityError("An existing account still has the development default password. Change it before starting a network-reachable backend.")
            except StartupSecurityError:
                raise
            except Exception:  # noqa: BLE001 - an unreadable hash is not the default password
                continue
