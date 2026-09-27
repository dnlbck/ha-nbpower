"""Exceptions for the NB Power integration."""

from __future__ import annotations


class NBPowerError(Exception):
    """Base exception for NB Power errors."""


class NBPowerAuthError(NBPowerError):
    """Raised when logging in to the portal fails or the session expires."""


class NBPowerApiError(NBPowerError):
    """Raised when the portal or WidgetAPI returns an unexpected response."""
