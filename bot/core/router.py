"""Persistent component router.

All long-living buttons and selects use custom IDs of the form ``xs:<route>:<arg1>:<arg2>…``.
A single :class:`discord.ui.DynamicItem` (registered via ``bot.add_dynamic_items``) matches these IDs
and dispatches to the handler registered for ``<route>``. Because the state lives in the custom ID
(and the database), every button keeps working after a restart – no in-memory view objects needed.

Cogs register handlers with the :func:`component` decorator (see :class:`bot.core.cog.XCog`)::

    @component("ticket.close")
    async def close_button(self, interaction: discord.Interaction, ctx: ComponentContext) -> None:
        ticket_id = ctx.args[0]
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

import discord
from discord import ui

if TYPE_CHECKING:
    from .bot import XumariBot

_log = logging.getLogger("xumari.router")

PREFIX = "xs"
MAX_CUSTOM_ID = 100
ROUTE_TEMPLATE = rf"{PREFIX}:(?P<route>[a-z0-9_.]+)(?::(?P<args>.*))?"

Handler = Callable[[discord.Interaction[Any], "ComponentContext"], Awaitable[None]]
F = TypeVar("F", bound=Callable[..., Awaitable[None]])


def cid(route: str, *args: object) -> str:
    """Build a routed custom ID: ``cid("ticket.close", "AB12CD34")`` → ``xs:ticket.close:AB12CD34``."""
    parts = [PREFIX, route, *(str(a) for a in args)]
    custom_id = ":".join(parts)
    if len(custom_id) > MAX_CUSTOM_ID:
        raise ValueError(f"custom_id too long ({len(custom_id)} > {MAX_CUSTOM_ID}): {custom_id}")
    return custom_id


def component(route: str) -> Callable[[F], F]:
    """Mark a cog method as handler for ``route``."""

    def decorator(func: F) -> F:
        func.__xs_route__ = route  # type: ignore[attr-defined]
        return func

    return decorator


@dataclass(slots=True)
class ComponentContext:
    route: str
    args: tuple[str, ...]
    item: ui.Item[Any]
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def values(self) -> list[str]:
        """Selected values of a string select (empty for buttons)."""
        values = getattr(self.item, "values", None)
        if not values:
            return []
        return [v if isinstance(v, str) else str(getattr(v, "id", v)) for v in values]

    @property
    def resolved_values(self) -> list[Any]:
        """Raw values (``Role``/``Member``/``AppCommandChannel`` objects for entity selects)."""
        return list(getattr(self.item, "values", []) or [])

    def arg(self, index: int, default: str | None = None) -> str | None:
        return self.args[index] if index < len(self.args) else default

    def int_arg(self, index: int, default: int = 0) -> int:
        try:
            return int(self.args[index])
        except (IndexError, ValueError):
            return default


@dataclass(slots=True)
class RouteEntry:
    handler: Handler
    feature: str | None
    owner: str


class RouteRegistry:
    def __init__(self) -> None:
        self._routes: dict[str, RouteEntry] = {}

    def register(self, route: str, handler: Handler, *, feature: str | None, owner: str) -> None:
        existing = self._routes.get(route)
        if existing is not None and existing.owner != owner:
            raise ValueError(f"route {route!r} already registered by {existing.owner}")
        self._routes[route] = RouteEntry(handler, feature, owner)

    def unregister_owner(self, owner: str) -> None:
        for route in [r for r, e in self._routes.items() if e.owner == owner]:
            del self._routes[route]

    def get(self, route: str) -> RouteEntry | None:
        return self._routes.get(route)

    def __len__(self) -> int:
        return len(self._routes)


class ComponentRouter(ui.DynamicItem[ui.Item[Any]], template=ROUTE_TEMPLATE):
    """The one dynamic item that dispatches every ``xs:`` component."""

    def __init__(self, item: ui.Item[Any], route: str, args: tuple[str, ...]) -> None:
        super().__init__(item)
        self.route = route
        self.args = args

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction[Any], item: ui.Item[Any], match: Any, /
    ) -> ComponentRouter:
        raw_args = match["args"]
        args = tuple(raw_args.split(":")) if raw_args else ()
        return cls(item, match["route"], args)

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        bot: XumariBot = interaction.client  # type: ignore[assignment]
        try:
            entry = bot.routes.get(self.route)
            if entry is None:
                from .errors import NotFound

                raise NotFound("errors.component_expired")
            if not bot.features.is_enabled(interaction.guild_id, entry.feature):
                from .errors import FeatureDisabled

                raise FeatureDisabled()
            await entry.handler(interaction, ComponentContext(self.route, self.args, self.item))
        except Exception as error:  # noqa: BLE001 – central handler decides what the user sees
            await bot.errors.handle(interaction, error)
