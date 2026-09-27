"""Windows shim for tests only.

``fcntl`` is POSIX-only; ``homeassistant.runner`` imports it at module level
but only uses it to guard against concurrent instances, which never happens
in tests. Loaded via PYTHONPATH so pytest-homeassistant-custom-component can
import Home Assistant on Windows.
"""

LOCK_EX = 2
LOCK_SH = 1
LOCK_NB = 4
LOCK_UN = 8


def flock(fd, operation):  # noqa: ARG001
    """No-op stand-in for POSIX advisory locking."""
    return None
