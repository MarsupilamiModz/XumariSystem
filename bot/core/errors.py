"""User-facing exceptions. Raise them anywhere in a command/component handler – the central error
handler translates them into an ephemeral, localised notice. Unexpected exceptions are logged with
full details while the user only receives a generic localised error message with a reference code.

The classes inherit from ``app_commands.AppCommandError`` so that discord.py forwards them unwrapped
from command callbacks *and* checks to the tree's error handler."""

from __future__ import annotations

from typing import Any

from discord import app_commands


class XumariError(app_commands.AppCommandError):
    """Base error with a translation key."""

    key: str = "errors.generic"

    def __init__(self, key: str | None = None, /, **kwargs: Any) -> None:
        self.key = key or type(self).key
        self.kwargs = kwargs
        super().__init__(self.key)


class UserError(XumariError):
    key = "errors.generic"


class PermissionDenied(UserError):
    key = "errors.no_permission"


class FeatureDisabled(UserError):
    key = "errors.feature_disabled"


class NotFound(UserError):
    key = "errors.not_found"


class CooldownActive(UserError):
    key = "errors.cooldown"


class ConfigurationError(UserError):
    key = "errors.not_configured"
