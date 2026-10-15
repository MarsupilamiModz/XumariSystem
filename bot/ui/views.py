"""Live (in-memory) interactive layouts for short-lived flows such as wizards, games and paginated
lists. Persistent components use the router instead (see :mod:`bot.core.router`)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import discord
from discord import ui

_log = logging.getLogger("xumari.views")

if TYPE_CHECKING:
    from ..core.bot import XumariBot

Callback = Callable[[discord.Interaction[Any]], Awaitable[None]]


class InteractiveLayout(ui.LayoutView):
    """LayoutView bound to an owner with timeout handling and central error reporting."""

    def __init__(self, bot: XumariBot, *, owner_id: int | None, timeout: float | None = 180.0) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.owner_id = owner_id
        self.message: discord.Message | discord.InteractionMessage | None = None
        self._interaction: discord.Interaction[Any] | None = None

    def bind(self, interaction: discord.Interaction[Any]) -> None:
        """Remember the interaction so ephemeral messages can be edited on timeout."""
        self._interaction = interaction

    async def interaction_check(self, interaction: discord.Interaction[Any]) -> bool:
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            tr = self.bot.translator_for(interaction)
            await interaction.response.send_message(
                view=self.bot.notice(kind="error", title=tr("errors.not_your_component")), ephemeral=True
            )
            return False
        return True

    async def on_error(self, interaction: discord.Interaction[Any], error: Exception, item: ui.Item[Any]) -> None:
        await self.bot.errors.handle(interaction, error)

    def disable_all(self) -> None:
        for child in self.walk_children():
            if isinstance(child, (ui.Button, ui.Select, ui.ChannelSelect, ui.RoleSelect, ui.UserSelect)):
                if isinstance(child, ui.Button) and child.url:
                    continue
                child.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all()
        try:
            if self._interaction is not None:
                await self._interaction.edit_original_response(view=self)
            elif self.message is not None:
                await self.message.edit(view=self)
        except discord.HTTPException:
            _log.debug("Could not disable components after timeout", exc_info=True)


def attach(item: ui.Button[Any] | ui.Select[Any], callback: Callback) -> ui.Button[Any] | ui.Select[Any]:
    """Assign a coroutine callback to a dynamically created component."""

    async def _cb(interaction: discord.Interaction[Any]) -> None:
        await callback(interaction)

    item.callback = _cb  # type: ignore[method-assign]
    return item
