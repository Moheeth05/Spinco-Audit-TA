"""Single source of truth for whether a hosted provider (NVIDIA OCR / NVIDIA
text fallback / NVIDIA document classifier / DeepSeek) may be used at all.

Strictly opt-in: a provider is enabled only when its API key is configured
AND its enable flag is explicitly set to a truthy value. A missing flag, an
empty flag, an unrecognised value ("maybe", "tru") and an explicit false all
mean DISABLED - an API key alone never enables anything.

Every provider module calls these helpers both from its own is_available()
and again at the top of its network-calling function, so a direct call to
the function enforces the gate independently of whatever the caller checked.

Nothing here ever returns, logs or formats a secret value.
"""

import os
from urllib.parse import urlsplit

_TRUTHY = frozenset({"true", "1", "yes", "on"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def flag_enabled(flag_var: str) -> bool:
    value = os.environ.get(flag_var)
    return value is not None and value.strip().lower() in _TRUTHY


def provider_enabled(key_var: str, flag_var: str) -> bool:
    key = os.environ.get(key_var)
    return bool(key and key.strip()) and flag_enabled(flag_var)


def is_safe_endpoint(url: str | None) -> bool:
    """HTTPS only (the request carries a Bearer credential), except plain
    HTTP to a loopback host so a local mock server can stand in during
    tests. Anything else - missing scheme, other schemes, no host - is
    treated as invalid configuration and no request is made."""
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and host in _LOOPBACK_HOSTS
