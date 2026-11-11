"""Pacing of portal sign-ins after one has failed.

A sign-in can fail without the credentials being wrong, and up to 0.12
every failed one stopped the integration until the password was typed in
again. Two cases, paced differently:

* The sign-in could not be made, or led nowhere: the portal has its login
  form switched off for scheduled maintenance (seen live for 29 hours),
  answers with an error, or the new session brings no data. That says
  nothing about the credentials. It is tried again after short, growing
  waits, for as long as it takes.
* The portal answers the sign-in with its login page again. A wrong
  password gets that, but it is not proof of one (a sign-in posted during
  maintenance got it too, before the client could tell the form was off):

  - it is retried after growing waits covering a day;
  - then the caller is told to ask for the credentials, and the sign-in
    is still tried daily for a week in case the portal comes round;
  - after that it gives up until the credentials are entered again.

Neither is retried at Home Assistant's own pace (every few seconds to
minutes while a setup is failing), which would post the credentials over
and over.

This module must not import ``homeassistant``; the coordinator persists the
guard with :meth:`SignInGuard.as_dict` and applies its verdicts.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from .const import (
    SIGN_IN_DAILY_RETRIES,
    SIGN_IN_ERROR_DELAYS,
    SIGN_IN_EVENTS_KEPT,
    SIGN_IN_RETRY_DELAYS,
)

# Verdicts of SignInGuard.rejected().
RETRY = "retry"  # wait, then try again
ASK = "ask"  # ask for the credentials, and keep trying meanwhile
GIVE_UP = "give_up"  # stop until the credentials are entered again

_DAILY = timedelta(days=1)
_DETAIL_LENGTH = 300


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


class SignInGuard:
    """What recent sign-ins came to, and when the next one may be tried."""

    def __init__(self, stored: Mapping[str, Any] | None = None) -> None:
        stored = stored or {}
        self.rejections = int(stored.get("rejections") or 0)
        self.rejected_since = _parse(stored.get("rejected_since"))
        self.errors = int(stored.get("errors") or 0)
        self.retry_at = _parse(stored.get("retry_at"))
        self.events: list[dict[str, Any]] = [
            dict(event)
            for event in (stored.get("events") or [])[-SIGN_IN_EVENTS_KEPT:]
            if isinstance(event, Mapping)
        ]

    def as_dict(self) -> dict[str, Any]:
        """The guard as stored (and as shown in the diagnostics)."""
        return {
            "rejections": self.rejections,
            "rejected_since": (
                self.rejected_since.isoformat() if self.rejected_since else None
            ),
            "errors": self.errors,
            "retry_at": self.retry_at.isoformat() if self.retry_at else None,
            "events": list(self.events),
        }

    @property
    def last_failure(self) -> str | None:
        """What the most recent failed sign-in came to, if the last one failed."""
        if self.events and self.events[-1]["result"] != "ok":
            return self.events[-1].get("detail")
        return None

    def wait_until(self, now: datetime) -> datetime | None:
        """When a sign-in may be tried again; None if it may be tried now."""
        if self.retry_at is None or self.retry_at <= now:
            return None
        # A clock set back must not stretch a wait past the longest one.
        return min(self.retry_at, now + _DAILY)

    def succeeded(self, now: datetime, detail: str | None = None) -> timedelta | None:
        """Note an accepted sign-in; return how long refusals had lasted."""
        lasted = now - self.rejected_since if self.rejected_since else None
        self.rejections = 0
        self.rejected_since = None
        self.errors = 0
        self.retry_at = None
        self._record(now, "ok", detail)
        return lasted

    def rejected(self, now: datetime, detail: str) -> str:
        """Note a refused sign-in; return RETRY, ASK or GIVE_UP."""
        self.errors = 0
        self.rejections += 1
        self.rejected_since = self.rejected_since or now
        self._record(now, "rejected", detail)
        if self.rejections <= len(SIGN_IN_RETRY_DELAYS):
            self.retry_at = now + SIGN_IN_RETRY_DELAYS[self.rejections - 1]
            return RETRY
        if self.rejections <= len(SIGN_IN_RETRY_DELAYS) + SIGN_IN_DAILY_RETRIES:
            self.retry_at = now + _DAILY
            return ASK
        # Nothing scheduled: a reload or restart may try once more.
        self.retry_at = None
        return GIVE_UP

    def failed(self, now: datetime, detail: str) -> None:
        """Note a sign-in that could not be made or led nowhere (the login
        form is off, the portal answered with an error, no data came)."""
        self.errors += 1
        delay = SIGN_IN_ERROR_DELAYS[min(self.errors, len(SIGN_IN_ERROR_DELAYS)) - 1]
        self.retry_at = now + delay
        self._record(now, "error", detail)

    def _record(self, now: datetime, result: str, detail: str | None) -> None:
        self.events.append(
            {
                "at": now.isoformat(timespec="seconds"),
                "result": result,
                "detail": detail[:_DETAIL_LENGTH] if detail else None,
            }
        )
        del self.events[:-SIGN_IN_EVENTS_KEPT]
