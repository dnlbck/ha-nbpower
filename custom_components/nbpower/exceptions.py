"""Exceptions for the NB Power integration."""

from __future__ import annotations


class NBPowerError(Exception):
    """Base exception for NB Power errors."""


class NBPowerAuthError(NBPowerError):
    """Raised when logging in to the portal fails or the session expires."""


class NBPowerSignInRejected(NBPowerAuthError):
    """The portal answered a sign-in with its login page again.

    That is what a wrong password gets, but on its own it is not proof of
    one: until 0.13 a sign-in posted while the portal had its login form
    switched off for maintenance came back the same way (that case is now
    NBPowerSignInClosed). ``reason`` is what the page said in answer, if
    anything, ``notice`` what the login page was already announcing before
    the sign-in, and ``challenged`` whether the answer showed a word
    verification (CAPTCHA).
    """

    def __init__(
        self,
        reason: str | None = None,
        *,
        notice: str | None = None,
        challenged: bool = False,
    ) -> None:
        self.reason = reason
        self.notice = notice
        self.challenged = challenged
        message = "NB Power turned the sign-in away"
        if reason:
            message += f", saying {reason!r}"
        elif notice:
            message += f" (its login page carries the notice {notice!r})"
        if challenged:
            message += " (its login page asks for a word verification)"
        super().__init__(message)


class NBPowerApiError(NBPowerError):
    """Raised when the portal or WidgetAPI returns an unexpected response."""


class NBPowerSignInClosed(NBPowerApiError):
    """The portal has its login form switched off.

    It does that for scheduled maintenance: the username, password and
    Login controls are rendered disabled, and the page says from when
    until when (seen live 2026-11-10/11, a 29-hour window). Nothing is
    posted then, and nothing is learned about the credentials. ``notice``
    is what the login page says, if anything.
    """

    def __init__(self, notice: str | None = None) -> None:
        self.notice = notice
        message = "NB Power is not taking sign-ins"
        if notice:
            message += f", saying {notice!r}"
        super().__init__(message)
