"""``/stats`` – statistics for tickets, designers, reviews, sales and customers."""

from __future__ import annotations

import datetime as dt
from typing import Any

import discord
from discord import app_commands

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.permissions import require_staff
from ...core.utils import format_money, utcnow
from ...services.orders import OrderService
from ...services.stats import StatsService
from . import views

PERIODS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90, "365d": 365, "all": 0}
PERIOD_CHOICES = [app_commands.Choice(name=T(f"period.{k}"), value=k) for k in PERIODS]


class StatsCog(XCog, name="Stats"):
    feature = "stats"

    group = app_commands.Group(name="stats", description=T("stats"), guild_only=True)

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.stats = StatsService(bot.db)
        self.orders = OrderService(bot.db, bot.config)

    @staticmethod
    def since(period: str) -> dt.datetime | None:
        days = PERIODS.get(period, 30)
        return utcnow() - dt.timedelta(days=days) if days else None

    def money(self, locale: str) -> Any:
        return lambda cents: format_money(cents, self.bot.config.shop.currency, locale)

    async def render(self, kind: str, guild_id: int, locale: str, period: str) -> discord.ui.LayoutView:
        """Shared by the slash command and the control panel."""
        tr = self.bot.i18n.translator(locale)
        since = self.since(period)
        label = tr(f"orders.stats.period.{period}")
        money = self.money(locale)
        if kind == "designers":
            return views.render_designers(self.bot.theme, tr, await self.stats.designers(guild_id, since), money, label)
        if kind == "tickets":
            return views.render_tickets(self.bot.theme, tr, await self.stats.ticket_categories(guild_id, since),
                                        await self.stats.top_supporters(guild_id, since), label)
        if kind == "reviews":
            return views.render_reviews(self.bot.theme, tr, await self.stats.reviews(guild_id, since), label)
        if kind == "sales":
            return views.render_sales(self.bot.theme, tr, await self.orders.stats(guild_id, since),
                                      await self.stats.top_products(guild_id, since), money, label)
        if kind == "customers":
            return views.render_customers(self.bot.theme, tr, await self.stats.customers(guild_id, since), money, label)
        return views.render_overview(self.bot.theme, tr, await self.stats.overview(guild_id, since), money, label)

    async def _send(self, interaction: discord.Interaction[Any], kind: str, period: str) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        view = await self.render(kind, interaction.guild_id, self.bot.locale_for(interaction), period)
        await interaction.followup.send(view=view, ephemeral=True)

    @group.command(name="overview", description=T("stats.overview"))
    @app_commands.describe(period=T("orderstats.period"))
    @app_commands.choices(period=PERIOD_CHOICES)
    @require_staff()
    async def overview(self, interaction: discord.Interaction[Any], period: str = "30d") -> None:
        await self._send(interaction, "overview", period)

    @group.command(name="designers", description=T("stats.designers"))
    @app_commands.describe(period=T("orderstats.period"))
    @app_commands.choices(period=PERIOD_CHOICES)
    @require_staff()
    async def designers(self, interaction: discord.Interaction[Any], period: str = "30d") -> None:
        await self._send(interaction, "designers", period)

    @group.command(name="tickets", description=T("stats.tickets"))
    @app_commands.describe(period=T("orderstats.period"))
    @app_commands.choices(period=PERIOD_CHOICES)
    @require_staff()
    async def tickets(self, interaction: discord.Interaction[Any], period: str = "30d") -> None:
        await self._send(interaction, "tickets", period)

    @group.command(name="reviews", description=T("stats.reviews"))
    @app_commands.describe(period=T("orderstats.period"))
    @app_commands.choices(period=PERIOD_CHOICES)
    @require_staff()
    async def reviews(self, interaction: discord.Interaction[Any], period: str = "all") -> None:
        await self._send(interaction, "reviews", period)

    @group.command(name="sales", description=T("stats.sales"))
    @app_commands.describe(period=T("orderstats.period"))
    @app_commands.choices(period=PERIOD_CHOICES)
    @require_staff()
    async def sales(self, interaction: discord.Interaction[Any], period: str = "30d") -> None:
        await self._send(interaction, "sales", period)

    @group.command(name="customers", description=T("stats.customers"))
    @app_commands.describe(period=T("orderstats.period"))
    @app_commands.choices(period=PERIOD_CHOICES)
    @require_staff()
    async def customers(self, interaction: discord.Interaction[Any], period: str = "30d") -> None:
        await self._send(interaction, "customers", period)
