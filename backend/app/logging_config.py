"""Safe logging for the backend.

  * REDACTION: every log message passes through `redact()` before it is formatted, so a password, bearer token, Google access / refresh token, JWT, private key or
    session / CSRF token that ends up in a message (by mistake, or inside a third-party library's text) is replaced by a marker. It is installed as the log-record
    factory, so it covers every logger (the application, uvicorn, httpx, SQLAlchemy ...), every handler and every destination.
  * DESTINATIONS (all optional, all from the environment):
        SPINCO_LOG_LEVEL   DEBUG | INFO | WARNING | ERROR   (default: unchanged)
        SPINCO_LOG_DIR     a folder that receives spinco-backend.log, rotated at 10 MB with 10 backups (default: no file)
  Document contents are never logged by the application; this module only protects against accidents.
"""

import logging
import logging.handlers
import os
import re
from pathlib import Path

_REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<redacted-private-key>"),
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(bearer|basic)\s+[^\s,;\"']+"), r"\1\2 <redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"), "<redacted-jwt>"),
    (re.compile(r"\bya29\.[A-Za-z0-9_.-]{20,}"), "<redacted-google-token>"),
    (re.compile(r"\b1//[A-Za-z0-9_-]{20,}"), "<redacted-google-refresh-token>"),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|client_secret|token|access_token|refresh_token|id_token|private_key|api[_-]?key|csrf[_-]?token|session[_-]?token|spinco_session)"
            r"([\"']?\s*[:=]\s*[\"']?)[^\s,;\"'&}\]]+"
        ),
        r"\1\2<redacted>",
    ),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


_installed = False


def install_redaction() -> None:
    """Idempotent: wraps the current log-record factory once."""
    global _installed
    if _installed:
        return
    _installed = True
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed log call must not become an error here
            return record
        redacted = redact(message)
        if redacted != message or record.args:
            record.msg, record.args = redacted, None
        return record

    logging.setLogRecordFactory(factory)


def configure_logging(env=None) -> None:
    """Applies SPINCO_LOG_LEVEL / SPINCO_LOG_DIR (see the module docstring) and installs redaction. Safe to call more than once."""
    env = os.environ if env is None else env
    install_redaction()
    level_name = str(env.get("SPINCO_LOG_LEVEL", "")).strip().upper()
    level = logging.getLevelName(level_name) if level_name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else None
    if isinstance(level, int):
        logging.getLogger().setLevel(level)
        logging.getLogger("app").setLevel(level)
    log_dir = str(env.get("SPINCO_LOG_DIR", "")).strip()
    if not log_dir:
        return
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = str(directory / "spinco-backend.log")
    names = ("", "uvicorn", "uvicorn.error", "uvicorn.access")  # uvicorn's loggers do not propagate to the root logger
    for name in names:
        logger = logging.getLogger(name)
        if any(getattr(h, "_spinco_file", None) == target for h in logger.handlers):
            continue
        handler = logging.handlers.RotatingFileHandler(target, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler._spinco_file = target  # type: ignore[attr-defined]
        logger.addHandler(handler)
    if logging.getLogger().level in (logging.NOTSET, logging.WARNING) and not isinstance(level, int):
        logging.getLogger().setLevel(logging.INFO)
