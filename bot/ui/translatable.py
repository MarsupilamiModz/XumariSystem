"""Language switch ("English Version" / "Deutsche Version").

Public panels are rendered in the guild language. Their accessory button carries the custom ID
``xs:lang:<target_locale>:<renderer>:<args…>``; clicking it re-renders the same layout in the target
locale and sends it ephemerally. Cogs register their renderers via :meth:`RendererRegistry.register`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import discord
from discord import ui

from ..core.errors import NotFound

if TYPE_CHECKING:
    from ..core.bot import XumariBot

Renderer = Callable[[discord.Interaction[Any], str, tuple[str, ...]], Awaitable[ui.LayoutView]]


class RendererRegistry:
    def __init__(self) -> None:
        self._renderers: dict[str, Renderer] = {}

    def register(self, key: str, renderer: Renderer) -> None:
        self._renderers[key] = renderer

    def unregister(self, key: str) -> None:
        self._renderers.pop(key, None)

    async def render(
        self, interaction: discord.Interaction[Any], key: str, locale: str, args: tuple[str, ...]
    ) -> ui.LayoutView:
        renderer = self._renderers.get(key)
        if renderer is None:
            raise NotFound("errors.component_expired")
        return await renderer(interaction, locale, args)


async def handle_language_switch(interaction: discord.Interaction[Any], args: tuple[str, ...]) -> None:
    bot: XumariBot = interaction.client  # type: ignore[assignment]
    if len(args) < 2:
        raise NotFound("errors.component_expired")
    locale, key, rest = args[0], args[1], tuple(args[2:])
    view = await bot.renderers.render(interaction, key, bot.i18n.resolve_locale(locale), rest)
    await interaction.response.send_message(view=view, ephemeral=True)
