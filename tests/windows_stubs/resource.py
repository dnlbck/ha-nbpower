"""Windows shim for tests only.

``resource`` is POSIX-only; Home Assistant imports it in ``util.resource``
to raise the file-descriptor limit, which is irrelevant on Windows tests.
"""

RLIMIT_NOFILE = 7


def setrlimit(resource, limits):  # noqa: ARG001
    """No-op stand-in."""
    return None


def getrlimit(resource):  # noqa: ARG001
    """Report a generous limit."""
    return (4096, 4096)
