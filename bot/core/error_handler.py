"""Central error handling for slash commands, components and modals.

Users always get an ephemeral, localised notice. Expected errors (:class:`XumariError`) are shown
with their translation key; unexpected errors are logged with a short reference code that is also
shown to the user so staff can find the stack trace in the logs."""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from .errors import XumariError
from .utils import generate_code, utcnow

if TYPE_CHECKING:
    from .bot import XumariBot

_log = logging.getLogger("xumari.errors")


class ErrorHandler:
    def __init__(self, bot: XumariBot) -> None:
        self.bot = bot

    @staticmethod
    def _unwrap(error: BaseException) -> BaseException:
        while isinstance(error, (app_commands.CommandInvokeError, app_commands.TransformerError)) and error.__cause__:
            error = error.__cause__
        if isinstance(error, app_commands.CommandInvokeError):
            return error.original
        return error

    def describe(self, interaction: discord.Interaction[Any], error: BaseException) -> tuple[str, str]:
        """Return ``(title, description)`` for the user and log as appropriate."""
        tr = self.bot.translator_for(interaction)
        original = self._unwrap(error)

        if isinstance(original, XumariError):
            return tr("errors.title"), tr(original.key, **original.kwargs)
        if isinstance(original, app_commands.CommandOnCooldown):
            until = utcnow() + dt.timedelta(seconds=original.retry_after)
            return tr("errors.title"), tr("errors.cooldown", until=discord.utils.format_dt(until, "R"))
        if isinstance(original, app_commands.NoPrivateMessage):
            return tr("errors.title"), tr("errors.guild_only")
        if isinstance(original, app_commands.BotMissingPermissions) or isinstance(original, discord.Forbidden):
            _log.warning("Missing bot permissions: %s", original)
            return tr("errors.title"), tr("errors.bot_missing_permissions")
        if isinstance(original, (app_commands.MissingPermissions, app_commands.MissingRole,
                                 app_commands.MissingAnyRole, app_commands.CheckFailure)):
            return tr("errors.title"), tr("errors.no_permission")
        if isinstance(original, app_commands.TransformerError):
            return tr("errors.title"), tr("errors.invalid_input")

        reference = generate_code(6)
        _log.error(
            "Unhandled error [%s] (user=%s guild=%s command=%s)",
            reference,
            interaction.user.id if interaction.user else None,
            interaction.guild_id,
            getattr(interaction.command, "qualified_name", None) or (interaction.data or {}).get("custom_id"),
            exc_info=original,
        )
        return tr("errors.title"), tr("errors.unexpected", ref=reference)

    async def handle(self, interaction: discord.Interaction[Any], error: BaseException) -> None:
        title, description = self.describe(interaction, error)
        view = self.bot.notice(kind="error", title=title, description=description)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(view=view, ephemeral=True)
            else:
                await interaction.response.send_message(view=view, ephemeral=True)
        except discord.HTTPException:
            _log.debug("Could not deliver error message to user", exc_info=True)
