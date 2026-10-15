"""Base class for all XumariSystem cogs.

* ``feature`` links the cog to a feature flag (commands + components are gated per guild).
* Methods decorated with :func:`bot.core.router.component` are registered as component routes.
* Methods decorated with :func:`bus_event` are registered as handlers for DB events (API → bot).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

import discord
from discord.ext import commands

from .i18n import Translator

if TYPE_CHECKING:
    from .bot import XumariBot

F = TypeVar("F", bound=Callable[..., Awaitable[None]])


def bus_event(event_type: str) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        func.__xs_event__ = event_type  # type: ignore[attr-defined]
        return func

    return decorator


class XCog(commands.Cog):
    feature: ClassVar[str | None] = None

    def __init__(self, bot: XumariBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        for name in dir(type(self)):
            attr = getattr(type(self), name, None)
            if attr is None or not callable(attr):
                continue
            route = getattr(attr, "__xs_route__", None)
            if route:
                self.bot.routes.register(route, getattr(self, name), feature=self.feature, owner=self.qualified_name)
            event_type = getattr(attr, "__xs_event__", None)
            if event_type:
                self.bot.event_bridge.register(event_type, getattr(self, name), owner=self.qualified_name)
        await self.on_load()

    async def cog_unload(self) -> None:
        self.bot.routes.unregister_owner(self.qualified_name)
        self.bot.event_bridge.unregister_owner(self.qualified_name)
        await self.on_unload()

    async def on_load(self) -> None:
        """Hook for subclasses (start loops, register renderers). Default: nothing to do."""
        return None

    async def on_unload(self) -> None:
        """Hook for subclasses (stop loops). Default: nothing to do."""
        return None

    # convenience -------------------------------------------------------------------------
    def tr(self, interaction: discord.Interaction[Any]) -> Translator:
        return self.bot.translator_for(interaction)

    def gtr(self, guild_id: int | None) -> Translator:
        return self.bot.guild_translator(guild_id)
