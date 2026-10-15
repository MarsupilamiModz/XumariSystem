"""Integrated review system: DM with 1–5 star buttons after an order is completed, feedback modal,
public review post (reference layout "Bewertung") with automatically counted ticket statistics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import PermissionDenied, UserError
from ...core.logger import get_logger
from ...core.router import ComponentContext, component
from ...services.orders import Order, OrderService
from ...ui.layouts import build_review, build_review_request, stars
from ...ui.modals import FormModal, TextField
from .service import TicketService

_log = get_logger("reviews")


@dataclass(slots=True)
class Review:
    id: int
    order_id: int
    rating: int
    feedback: str | None


class ReviewsCog(XCog, name="Reviews"):
    feature = "tickets"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.orders = OrderService(bot.db, bot.config)
        self.tickets = TicketService(bot)

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.tickets.reviews

    async def has_review(self, order_id: int) -> bool:
        return bool(await self.bot.db.fetchval("SELECT 1 FROM reviews WHERE order_id = $1", order_id))

    # ------------------------------------------------------------------ request
    @commands.Cog.listener()
    async def on_xs_order_status(self, order: Order, old_status: str) -> None:
        if order.status == "completed" and self.cfg.dm_on_completion:
            await self.request_review(order)

    async def request_review(self, order: Order) -> bool:
        if await self.has_review(order.id):
            return False
        guild = self.bot.get_guild(order.guild_id)
        member = guild.get_member(order.customer_id) if guild else None
        if guild is None or member is None:
            return False
        tr = self.bot.i18n.translator(order.locale)
        return await self.bot.safe_dm(member, build_review_request(
            self.bot.theme, tr, order_id=order.id, order_number=order.number, guild_name=guild.name))

    @app_commands.command(name="review", description=T("review"))
    @app_commands.describe(number=T("order.number"))
    @app_commands.guild_only()
    async def review_cmd(self, interaction: discord.Interaction[Any], number: int | None = None) -> None:
        tr = self.tr(interaction)
        if number is not None:
            order = await self.orders.get(number, interaction.guild_id)
        else:
            candidates = [o for o in await self.orders.for_customer(interaction.guild_id, interaction.user.id)
                          if o.status == "completed" and not await self.has_review(o.id)]
            if not candidates:
                raise UserError("reviews.errors.nothing_to_review")
            order = candidates[0]
        self._check(interaction.user, order)
        await interaction.response.send_message(
            view=build_review_request(self.bot.theme, tr, order_id=order.id, order_number=order.number,
                                      guild_name=interaction.guild.name if interaction.guild else ""),
            ephemeral=True,
        )

    def _check(self, user: discord.abc.User, order: Order) -> None:
        if user.id != order.customer_id:
            raise PermissionDenied("orders.errors.not_your_order")
        if order.status != "completed":
            raise UserError("reviews.errors.not_completed")

    # ------------------------------------------------------------------ rating
    @component("review.rate")
    async def on_rate(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self.orders.get(ctx.int_arg(0))
        rating = max(1, min(5, ctx.int_arg(1, 5)))
        self._check(interaction.user, order)
        if await self.has_review(order.id):
            raise UserError("reviews.errors.already")
        tr = self.bot.i18n.translator(order.locale)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            review = await self.save_review(order, modal_interaction.user, rating, str(values.get("feedback") or ""))
            guild = self.bot.get_guild(order.guild_id)
            await modal_interaction.response.edit_message(view=build_review_request(
                self.bot.theme, tr, order_id=order.id, order_number=order.number,
                guild_name=guild.name if guild else "", rating=review.rating))
            await modal_interaction.followup.send(
                view=self.bot.notice(kind="success", title=tr("reviews.thanks.title"),
                                     description=tr("reviews.thanks.text", stars=stars(self.bot.theme, rating))),
                ephemeral=modal_interaction.guild_id is not None,
            )

        await interaction.response.send_modal(FormModal(
            self.bot,
            title=tr("reviews.modal.title", rating=rating),
            fields=[TextField(key="feedback", label=tr("reviews.modal.field"), paragraph=True, required=False,
                              max_length=1000, placeholder=tr("reviews.modal.placeholder"))],
            on_submit=submitted,
        ))

    async def save_review(self, order: Order, user: discord.abc.User, rating: int, feedback: str) -> Review:
        stats = await self.tickets.stats(order.ticket_id) if order.ticket_id else None
        try:
            row = await self.bot.db.fetchrow(
                """
                INSERT INTO reviews (guild_id, order_id, ticket_id, user_id, designer_id, rating, feedback,
                                     stat_messages, stat_participants, stat_attachments)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id
                """,
                order.guild_id, order.id, order.ticket_id, user.id, order.designer_id, rating, feedback or None,
                stats.messages if stats else 0, stats.participants if stats else 0, stats.attachments if stats else 0,
            )
        except asyncpg.UniqueViolationError as exc:
            raise UserError("reviews.errors.already") from exc
        assert row is not None
        review = Review(row["id"], order.id, rating, feedback or None)

        guild = self.bot.get_guild(order.guild_id)
        if guild is None:
            return review
        tr = self.gtr(guild.id)
        channel = guild.get_channel(self.cfg.channel_id or 0)
        if isinstance(channel, discord.TextChannel):
            view = build_review(
                self.bot.theme, tr, user_mention=user.mention, user_name=getattr(user, "display_name", user.name),
                order_number=order.number, rating=rating, messages=stats.messages if stats else 0,
                participants=stats.participants if stats else 0, attachments=stats.attachments if stats else 0,
                feedback=feedback or None,
            )
            message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
            try:
                await message.add_reaction(discord.PartialEmoji.from_str(self.cfg.react_emoji))
            except discord.HTTPException:
                _log.warning("Could not react with %r on review %s", self.cfg.react_emoji, review.id)
            await self.bot.db.execute("UPDATE reviews SET channel_id = $2, message_id = $3 WHERE id = $1",
                                      review.id, channel.id, message.id)
        await self.bot.audit.log(
            guild, "orders", "review.create", title=tr("reviews.log.created", number=order.number), emoji="review",
            actor=user, lines=[f"{stars(self.bot.theme, rating)} ({rating}/5)", *( [f"> {feedback[:500]}"] if feedback else [])],
            details={"order_id": order.id, "rating": rating},
        )
        self.bot.dispatch("xs_review_created", review, order)
        return review
