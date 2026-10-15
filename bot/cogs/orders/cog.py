"""Orders: self-service order wizard in tickets, workflow commands, queue, todos and statistics."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

import discord
from discord import app_commands
from discord.ext import commands

from ...core.cog import XCog, bus_event
from ...core.command_i18n import T
from ...core.errors import NotFound, PermissionDenied, UserError
from ...core.logger import get_logger
from ...core.permissions import ensure_staff, is_staff, require_staff
from ...core.router import ComponentContext, cid, component
from ...core.utils import format_money, parse_money, truncate, utcnow
from ...services.orders import QUEUE_STATUSES, Order, OrderService
from ...services.shop import ShopService
from ...ui.builder import select_option, string_select
from ...ui.layouts import SelectGroup, build_confirm, build_order_create
from ...ui.modals import FormModal, TextField
from . import views

if TYPE_CHECKING:
    from ..tickets.cog import TicketsCog
    from ..tickets.service import Ticket

_log = get_logger("orders")
Step = Literal["products", "bundles", "details", "summary"]
STEPS: tuple[str, ...] = ("products", "bundles", "details", "summary")
PERIODS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90, "365d": 365, "all": 0}


class OrdersCog(XCog, name="Orders"):
    feature = "orders"

    order_group = app_commands.Group(name="order", description=T("order"), guild_only=True)
    todo_group = app_commands.Group(name="todo", description=T("todo"), guild_only=True)

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.shop = ShopService(bot.db)
        self.orders = OrderService(bot.db, bot.config)
        self._queue_tasks: dict[int, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[Any]] = set()

    async def on_unload(self) -> None:
        for task in [*self._queue_tasks.values(), *self._background]:
            task.cancel()

    # ================================================================== helpers
    @property
    def tickets(self) -> TicketsCog | None:
        return self.bot.get_cog("Tickets")  # type: ignore[return-value]

    def money(self, locale: str) -> Callable[[int], str]:
        currency = self.bot.config.shop.currency
        return lambda cents: format_money(cents, currency, locale)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _order(self, interaction: discord.Interaction[Any], order_id: int, *, editable: bool) -> Order:
        order = await self.orders.get(order_id, interaction.guild_id)
        if interaction.user.id != order.customer_id and not is_staff(self.bot, interaction.user):
            raise PermissionDenied("orders.errors.not_your_order")
        if editable and order.status != "draft":
            raise UserError("orders.errors.locked")
        return order

    async def _ticket_of(self, order: Order) -> Ticket | None:
        if order.ticket_id is None or self.tickets is None:
            return None
        return await self.tickets.service.by_id(order.ticket_id)

    async def order_here(self, interaction: discord.Interaction[Any]) -> Order:
        if self.tickets is None:
            raise NotFound("orders.errors.no_order_here")
        ticket = await self.tickets.service.by_channel(interaction.channel_id or 0)
        if ticket is None:
            raise NotFound("orders.errors.no_order_here")
        order = await self.orders.latest_for_ticket(ticket.id, include_draft=True)
        if order is None:
            raise NotFound("orders.errors.no_order_here")
        return order

    async def _resolve(self, interaction: discord.Interaction[Any], number: int | None) -> Order:
        if number is not None:
            return await self.orders.get(number, interaction.guild_id)
        return await self.order_here(interaction)

    # ================================================================== wizard rendering
    async def render_step(self, order: Order, step: str) -> discord.ui.LayoutView:
        tr = self.bot.i18n.translator(order.locale)
        money = self.money(order.locale)
        if step in ("products", "bundles"):
            return await self._render_products(order, "bundles" if step == "bundles" else "products")
        if step == "details":
            return views.render_details(self.bot.theme, tr, order, money, self.bot.config)
        return views.render_summary(self.bot.theme, tr, order, money, self.bot.config)

    async def _render_products(self, order: Order, mode: Literal["products", "bundles"]) -> discord.ui.LayoutView:
        tr = self.bot.i18n.translator(order.locale)
        money = self.money(order.locale)
        other = "en" if order.locale == "de" else "de"
        categories = {c.id: c for c in await self.shop.categories(order.guild_id)}
        groups: dict[int, list[discord.SelectOption]] = {}
        for product in await self.shop.products(order.guild_id):
            key = product.category_id if product.category_id in categories else 0
            groups.setdefault(key, []).append(
                select_option(
                    self.bot.theme, product.name(order.locale), str(product.id),
                    description=f"{product.name(other)} · {money(product.price_cents)}",
                    emoji=product.emoji or (categories[key].emoji if key in categories else None) or "product",
                )
            )
        ordered_keys = [c for c in categories if c in groups] + ([0] if 0 in groups else [])
        product_groups = [
            SelectGroup(
                key=str(key),
                placeholder=tr("orders.create.select_products",
                               category=categories[key].name(order.locale) if key in categories else tr("orders.create.other")),
                options=groups[key][:25],
                truncated=len(groups[key]) > 25,
            )
            for key in ordered_keys
        ]
        bundles = await self.shop.bundles(order.guild_id)
        bundle_group = SelectGroup(
            key="bundles",
            placeholder=tr("orders.create.select_bundles"),
            options=[
                select_option(self.bot.theme, b.name(order.locale), str(b.id),
                              description=f"{b.name(other)} · {money(b.price_cents)}", emoji=b.emoji or "bundle")
                for b in bundles[:25]
            ],
        )
        cart = tr("orders.create.cart_summary", count=order.item_count, amount=money(order.subtotal_cents)) \
            if order.items else tr("orders.create.cart_empty")
        return build_order_create(
            self.bot.theme, tr, order_id=order.id, mode=mode, product_groups=product_groups,
            bundle_group=bundle_group, cart_summary=cart, item_count=order.item_count,
        )

    async def refresh_wizard(self, order: Order, step: str = "products") -> None:
        if not order.wizard_channel_id or not order.wizard_message_id:
            return
        channel = self.bot.get_channel(order.wizard_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.get_partial_message(order.wizard_message_id).edit(view=await self.render_step(order, step))
        except discord.HTTPException:
            _log.debug("Could not refresh wizard of order %s", order.id)

    async def send_wizard(self, ticket: Ticket, channel: discord.TextChannel) -> Order:
        order = await self.orders.draft_for_ticket(ticket.id)
        if order is None:
            order = await self.orders.create_draft(
                guild_id=ticket.guild_id, customer_id=ticket.owner_id, ticket_id=ticket.id, locale=ticket.locale
            )
        message = await channel.send(view=await self.render_step(order, "products"))
        await self.orders.set_wizard_message(order, channel.id, message.id)
        return await self.orders.get(order.id)

    @commands.Cog.listener()
    async def on_xs_ticket_created(self, ticket: Ticket, channel: discord.TextChannel, member: discord.Member) -> None:
        if not self.bot.features.is_enabled(ticket.guild_id, "orders"):
            return
        category = self.bot.config.tickets.category(ticket.category_key)
        if category is None or not category.is_order:
            return
        try:
            await self.send_wizard(ticket, channel)
        except discord.HTTPException:
            _log.exception("Could not send order wizard for ticket %s", ticket.code)

    # ================================================================== wizard components
    @component("order.guide")
    async def on_guide(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        await interaction.response.send_message(view=views.guide_view(self.bot.theme, self.tr(interaction)), ephemeral=True)

    @component("order.view")
    async def on_view(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=True)
        step = ctx.arg(1) or "products"
        if step not in STEPS:
            step = "products"
        if step in ("details", "summary") and not order.items:
            raise UserError("orders.errors.empty_cart")
        order = await self.orders.recalculate(order.id, self.shop, money=self.money(order.locale))
        await interaction.response.edit_message(view=await self.render_step(order, step))

    @component("order.add")
    async def on_add_products(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=True)
        for value in ctx.values:
            product = await self.shop.product(order.guild_id, int(value))
            if product.active:
                await self.orders.add_product(order, product)
        order = await self.orders.recalculate(order.id, self.shop, money=self.money(order.locale))
        await interaction.response.edit_message(view=await self.render_step(order, "products"))

    @component("order.addb")
    async def on_add_bundles(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=True)
        for value in ctx.values:
            bundle = await self.shop.bundle(order.guild_id, int(value))
            if bundle.active:
                await self.orders.add_bundle(order, bundle)
        order = await self.orders.recalculate(order.id, self.shop, money=self.money(order.locale))
        await interaction.response.edit_message(view=await self.render_step(order, "bundles"))

    @component("order.cart")
    async def on_cart(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=False)
        await interaction.response.send_message(
            view=views.render_cart(self.bot.theme, self.bot.i18n.translator(order.locale), order, self.money(order.locale)),
            ephemeral=True,
        )

    @component("order.rm")
    async def on_remove_item(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=True)
        for value in ctx.values:
            await self.orders.remove_item(order, int(value))
        await self._after_cart_change(interaction, order)

    @component("order.clear")
    async def on_clear(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=True)
        await self.orders.clear(order)
        await self._after_cart_change(interaction, order)

    async def _after_cart_change(self, interaction: discord.Interaction[Any], order: Order) -> None:
        order = await self.orders.recalculate(order.id, self.shop, money=self.money(order.locale))
        tr = self.bot.i18n.translator(order.locale)
        await interaction.response.edit_message(
            view=views.render_cart(self.bot.theme, tr, order, self.money(order.locale))
        )
        await self.refresh_wizard(order, "products")

    @component("order.delivery")
    async def on_delivery(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=True)
        if ctx.values:
            await self.orders.set_delivery(order, ctx.values[0])
        order = await self.orders.recalculate(order.id, self.shop, money=self.money(order.locale))
        await interaction.response.edit_message(view=await self.render_step(order, "details"))

    @component("order.season")
    async def on_season(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=True)
        if ctx.values:
            value = ctx.values[0]
            await self.orders.set_seasonal(order, None if value == "none" else value)
        order = await self.orders.recalculate(order.id, self.shop, money=self.money(order.locale))
        await interaction.response.edit_message(view=await self.render_step(order, "details"))

    @component("order.coupon")
    async def on_coupon(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=True)
        tr = self.bot.i18n.translator(order.locale)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            current = await self._order(modal_interaction, order.id, editable=True)
            money = self.money(current.locale)
            coupon = await self.orders.apply_coupon(current, str(values["code"]), self.shop, money=money)
            updated = await self.orders.recalculate(current.id, self.shop, money=money)
            await modal_interaction.response.edit_message(view=await self.render_step(updated, "details"))
            await modal_interaction.followup.send(
                view=self.bot.notice(kind="success", title=tr("orders.coupon.applied", code=coupon.code,
                                                              amount=money(updated.discount_cents))),
                ephemeral=True,
            )

        await interaction.response.send_modal(
            FormModal(self.bot, title=tr("orders.coupon.add"),
                      fields=[TextField(key="code", label=tr("orders.coupon.field"), max_length=40)],
                      on_submit=submitted)
        )

    @component("order.uncoupon")
    async def on_uncoupon(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=True)
        await self.orders.set_coupon(order, None)
        order = await self.orders.recalculate(order.id, self.shop, money=self.money(order.locale))
        await interaction.response.edit_message(view=await self.render_step(order, "details"))

    @component("order.cancel")
    async def on_cancel(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=False)
        if order.status not in ("draft", "awaiting_payment"):
            raise UserError("orders.errors.locked")
        tr = self.tr(interaction)
        await interaction.response.send_message(
            view=build_confirm(self.bot.theme, tr, title=tr("orders.cancel.title"),
                               description=tr("orders.cancel.description", number=order.number),
                               confirm_id=cid("order.cancel_ok", order.id), cancel_id=cid("dismiss"), danger=True),
            ephemeral=True,
        )

    @component("order.cancel_ok")
    async def on_cancel_confirm(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=False)
        if order.status not in ("draft", "awaiting_payment"):
            raise UserError("orders.errors.locked")
        tr = self.tr(interaction)
        await interaction.response.edit_message(view=self.bot.notice(kind="info", title=tr("orders.cancel.done")))
        await self.apply_status(order, "cancelled", interaction.user, note=None)

    @component("order.submit")
    async def on_submit(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=True)
        if not order.items:
            raise UserError("orders.errors.empty_cart")
        await interaction.response.defer()
        order = await self.orders.recalculate(order.id, self.shop, money=self.money(order.locale))
        order = await self.apply_status(order, "awaiting_payment", interaction.user, notify=False)
        if order.total_cents == 0:
            # fully discounted – nothing to pay, goes straight into the queue
            order = await self.apply_status(order, "paid", interaction.user)
            await interaction.edit_original_response(view=views.render_status(
                self.bot.theme, self.bot.i18n.translator(order.locale), order, self.money(order.locale),
                position=await self.orders.queue_position(order)))
            return
        payment_url = await self.payment_url(order)
        await interaction.edit_original_response(view=views.render_submitted(
            self.bot.theme, self.bot.i18n.translator(order.locale), order, self.money(order.locale),
            self.bot.config, payment_url,
        ))

    @component("order.paylink")
    async def on_new_paylink(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        order = await self._order(interaction, ctx.int_arg(0), editable=False)
        if order.status != "awaiting_payment":
            raise UserError("orders.errors.not_awaiting_payment")
        await interaction.response.defer()
        payment_url = await self.payment_url(order, force_new=True)
        await interaction.edit_original_response(view=views.render_submitted(
            self.bot.theme, self.bot.i18n.translator(order.locale), order, self.money(order.locale),
            self.bot.config, payment_url,
        ))

    async def payment_url(self, order: Order, *, force_new: bool = False) -> str | None:
        """Ask the payments module (if loaded) for a Stripe checkout / payment link."""
        payments = self.bot.get_cog("Payments")
        if payments is None or not self.bot.features.is_enabled(order.guild_id, "payments"):
            return None
        return await payments.payment_url_for(order, force_new=force_new)  # type: ignore[attr-defined]

    # ================================================================== workflow core
    async def apply_status(
        self,
        order: Order,
        status: str,
        actor: discord.abc.User | None,
        *,
        designer_id: int | None = None,
        note: str | None = None,
        notify: bool = True,
    ) -> Order:
        """Single place for every status change: DB + history, ticket message, channel name, DM, log, queue."""
        if order.status == status:
            raise UserError("orders.errors.same_status", status=self.bot.i18n.t(f"orders.status.{status}", order.locale))
        if order.status in ("cancelled", "refunded") and status not in ("cancelled", "refunded"):
            raise UserError("orders.errors.closed_order")
        if order.status == "draft":
            order = await self.orders.recalculate(order.id, self.shop, money=self.money(order.locale))
        products = {p.id: p for p in await self.shop.products(order.guild_id, active_only=False)}
        old_status = order.status
        order = await self.orders.set_status(
            order, status, changed_by=actor.id if actor else None, note=note, designer_id=designer_id,
            delivery_days=self.orders.delivery_days(order, products),
        )
        await self.after_status_change(order, old_status, actor, note=note, notify=notify)
        return order

    async def after_status_change(
        self, order: Order, old_status: str, actor: discord.abc.User | None, *, note: str | None = None,
        notify: bool = True,
    ) -> None:
        """Side effects of a status change (also triggered by the API / Stripe webhook via the event bus)."""
        status = order.status
        guild = self.bot.get_guild(order.guild_id)
        if guild is None:
            return
        tr = self.bot.i18n.translator(order.locale)
        money = self.money(order.locale)
        position = await self.orders.queue_position(order)
        first_payment = old_status in ("draft", "awaiting_payment") and status in (*QUEUE_STATUSES, "in_progress")

        if first_payment:
            await self._grant_customer_role(guild, order.customer_id)
            await self.orders.ensure_default_todos(order)

        ticket = await self._ticket_of(order)
        channel = guild.get_channel(ticket.channel_id or 0) if ticket else None
        if isinstance(channel, discord.TextChannel) and ticket is not None:
            # Discord errors are logged, not raised: a retry of the outbox event would otherwise repeat
            # the side effects that already succeeded (status message, DM, todo panel)
            try:
                await channel.send(
                    view=views.render_status(self.bot.theme, tr, order, money, position=position,
                                             mention=f"<@{order.customer_id}>" if notify else None, note=note),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                if first_payment:
                    await self._post_todo_panel(order, channel)
            except discord.HTTPException:
                _log.warning("Could not post status of order %s in its ticket", order.id, exc_info=True)
            self._schedule_rename(guild, channel, ticket, status)

        if notify and status != "awaiting_payment":
            customer = guild.get_member(order.customer_id)
            if customer is not None:
                await self.bot.safe_dm(customer, views.render_status(
                    self.bot.theme, tr, order, money, position=position, note=note, guild_name=guild.name))

        gtr = self.gtr(guild.id)
        guild_locale = self.bot.guild_locale(guild.id)
        await self.bot.audit.log(
            guild, "orders", f"order.{status}",
            title=gtr("orders.log.status", number=order.number, status=gtr(f"orders.status.{status}")),
            emoji=views.STATUS_EMOJI_KEYS.get(status, "order"), actor=actor,
            lines=[
                gtr("orders.log.customer", user=f"<@{order.customer_id}>"),
                gtr("orders.log.transition", old=gtr(f"orders.status.{old_status}"), new=gtr(f"orders.status.{status}")),
                gtr("orders.log.total", amount=format_money(order.total_cents, order.currency, guild_locale)),
                *([gtr("orders.log.designer", user=f"<@{order.designer_id}>")] if order.designer_id else []),
                *([gtr("orders.log.note", note=note)] if note else []),
            ],
            target_id=order.customer_id, channel_id=ticket.channel_id if ticket else None,
            details={"order_id": order.id, "old": old_status, "new": status},
        )
        self.schedule_queue_update(guild.id)
        self.bot.dispatch("xs_order_status", order, old_status)

    async def _grant_customer_role(self, guild: discord.Guild, user_id: int) -> None:
        role_id = self.bot.config.roles.customer
        member = guild.get_member(user_id)
        role = guild.get_role(role_id) if role_id else None
        if member is not None and role is not None and role not in member.roles:
            try:
                await member.add_roles(role, reason="First paid order")
            except discord.HTTPException:
                _log.warning("Could not assign customer role to %s", user_id)

    def _schedule_rename(self, guild: discord.Guild, channel: discord.TextChannel, ticket: Ticket, status: str) -> None:
        if self.tickets is None:
            return
        emoji = self.bot.config.shop.status_emojis.get(status)
        category = self.bot.config.tickets.category(ticket.category_key)
        owner = guild.get_member(ticket.owner_id)
        name = self.tickets.channel_name(category.channel_prefix if category else "ticket",
                                         owner.name if owner else str(ticket.owner_id), emoji)
        if channel.name != name:
            self._spawn(self._rename(channel, name))

    @staticmethod
    async def _rename(channel: discord.TextChannel, name: str) -> None:
        try:
            await channel.edit(name=name, reason="Order status changed")
        except discord.HTTPException:
            _log.info("Channel rename of %s skipped (rate limit or permissions)", channel.id)

    # ================================================================== workflow commands
    async def _workflow(
        self, interaction: discord.Interaction[Any], status: str, *, designer: discord.Member | None = None,
        note: str | None = None,
    ) -> None:
        order = await self.order_here(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        designer_id = designer.id if designer else (interaction.user.id if status == "in_progress" and not order.designer_id else None)
        order = await self.apply_status(order, status, interaction.user, designer_id=designer_id, note=note)
        tr = self.tr(interaction)
        await interaction.followup.send(
            view=self.bot.notice(kind="success", title=tr("orders.workflow.done", number=order.number,
                                                          status=tr(f"orders.status.{status}"))),
            ephemeral=True,
        )

    @app_commands.command(name="bezahlt", description=T("bezahlt"))
    @app_commands.describe(note=T("workflow.note"))
    @app_commands.guild_only()
    @require_staff()
    async def paid_cmd(self, interaction: discord.Interaction[Any], note: str | None = None) -> None:
        await self._workflow(interaction, "paid", note=note)

    @app_commands.command(name="bearbeitung", description=T("bearbeitung"))
    @app_commands.describe(designer=T("bearbeitung.designer"), note=T("workflow.note"))
    @app_commands.guild_only()
    @require_staff()
    async def progress_cmd(self, interaction: discord.Interaction[Any], designer: discord.Member | None = None,
                           note: str | None = None) -> None:
        await self._workflow(interaction, "in_progress", designer=designer, note=note)

    @app_commands.command(name="pendinganswer", description=T("pendinganswer"))
    @app_commands.describe(note=T("workflow.note"))
    @app_commands.guild_only()
    @require_staff()
    async def pending_cmd(self, interaction: discord.Interaction[Any], note: str | None = None) -> None:
        await self._workflow(interaction, "pending_answer", note=note)

    @app_commands.command(name="backinqueue", description=T("backinqueue"))
    @app_commands.describe(note=T("workflow.note"))
    @app_commands.guild_only()
    @require_staff()
    async def queue_back_cmd(self, interaction: discord.Interaction[Any], note: str | None = None) -> None:
        await self._workflow(interaction, "queued", note=note)

    @app_commands.command(name="fertiggestellt", description=T("fertiggestellt"))
    @app_commands.describe(note=T("workflow.note"))
    @app_commands.guild_only()
    @require_staff()
    async def done_cmd(self, interaction: discord.Interaction[Any], note: str | None = None) -> None:
        await self._workflow(interaction, "completed", note=note)

    @bus_event("order.status_changed")
    async def on_bus_status_changed(self, guild_id: int | None, payload: dict[str, Any]) -> None:
        """Status was changed by the API or the Stripe webhook – run the Discord side effects."""
        order = await self.orders.get(int(payload["order_id"]))
        actor = self.bot.get_user(int(payload["actor_id"])) if payload.get("actor_id") else None
        await self.after_status_change(order, str(payload.get("old_status") or "draft"), actor,
                                       note=payload.get("note"))

    @bus_event("order.created")
    async def on_bus_order_created(self, guild_id: int | None, payload: dict[str, Any]) -> None:
        order = await self.orders.get(int(payload["order_id"]))
        guild = self.bot.get_guild(order.guild_id)
        if guild is None:
            return
        gtr = self.gtr(guild.id)
        await self.bot.audit.log(
            guild, "orders", "order.created_api", title=gtr("orders.log.created_api", number=order.number),
            emoji="order", lines=[gtr("orders.log.customer", user=f"<@{order.customer_id}>"),
                                  gtr("orders.log.total", amount=format_money(order.total_cents, order.currency,
                                                                               self.bot.guild_locale(guild.id)))],
            target_id=order.customer_id, details={"order_id": order.id, "source": order.source},
        )
        self.schedule_queue_update(guild.id)

    # ================================================================== queue
    def schedule_queue_update(self, guild_id: int) -> None:
        if not self.bot.config.shop.queue_channel_id:
            return
        task = self._queue_tasks.get(guild_id)
        if task is not None and not task.done():
            return
        self._queue_tasks[guild_id] = asyncio.create_task(self._delayed_queue_update(guild_id))

    async def _delayed_queue_update(self, guild_id: int) -> None:
        await asyncio.sleep(3)
        await self.update_queue_board(guild_id)

    async def update_queue_board(self, guild_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(self.bot.config.shop.queue_channel_id or 0) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return
        tr = self.gtr(guild_id)
        locale = self.bot.guild_locale(guild_id)
        view = views.render_queue(self.bot.theme, tr, await self.orders.queue(guild_id),
                                  await self.orders.in_progress(guild_id), self.money(locale))
        settings = await self.bot.db.fetchval("SELECT settings FROM guild_settings WHERE guild_id = $1", guild_id) or {}
        message_id = settings.get("queue_message_id")
        if message_id:
            try:
                await channel.get_partial_message(int(message_id)).edit(view=view)
                return
            except discord.NotFound:
                _log.info("Queue board message was deleted – posting a new one")
            except discord.HTTPException:
                _log.warning("Could not update queue board in %s", channel.id)
                return
        message = await channel.send(view=view)
        await self.bot.db.execute(
            "UPDATE guild_settings SET settings = settings || jsonb_build_object('queue_message_id', $2::text) "
            "WHERE guild_id = $1",
            guild_id, str(message.id),
        )

    @app_commands.command(name="queue", description=T("queue"))
    @app_commands.guild_only()
    async def queue_cmd(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        view = views.render_queue(self.bot.theme, tr, await self.orders.queue(interaction.guild_id),
                                  await self.orders.in_progress(interaction.guild_id),
                                  self.money(self.bot.locale_for(interaction)))
        await interaction.response.send_message(view=view, ephemeral=True)

    # ================================================================== /order …
    @order_group.command(name="info", description=T("order.info"))
    @app_commands.describe(number=T("order.number"))
    async def info_cmd(self, interaction: discord.Interaction[Any], number: int | None = None) -> None:
        order = await self._resolve(interaction, number)
        if interaction.user.id != order.customer_id:
            ensure_staff(self.bot, interaction.user)
        locale = self.bot.locale_for(interaction)
        await interaction.response.send_message(
            view=views.render_order_info(self.bot.theme, self.tr(interaction), order, self.money(locale),
                                         self.bot.config, position=await self.orders.queue_position(order)),
            ephemeral=True,
        )

    @order_group.command(name="position", description=T("order.position"))
    async def position_cmd(self, interaction: discord.Interaction[Any]) -> None:
        tr = self.tr(interaction)
        orders = [o for o in await self.orders.for_customer(interaction.guild_id, interaction.user.id)
                  if o.status in ("awaiting_payment", "paid", "queued", "in_progress", "pending_answer")]
        lines = []
        for order in orders[:10]:
            position = await self.orders.queue_position(order)
            suffix = f" · {tr('orders.info.position')} **#{position}**" if position else ""
            lines.append(f"**{tr('orders.number', number=order.number)}** · {tr(f'orders.status.{order.status}')}{suffix}")
        await self.bot.respond_notice(interaction, kind="info", title=tr("orders.position.title"),
                                      description="\n".join(lines) if lines else tr("orders.position.none"))

    @order_group.command(name="assign", description=T("order.assign"))
    @app_commands.describe(designer=T("order.assign.designer"), number=T("order.number"))
    @require_staff()
    async def assign_cmd(self, interaction: discord.Interaction[Any], designer: discord.Member,
                         number: int | None = None) -> None:
        order = await self._resolve(interaction, number)
        await self.orders.set_designer(order, designer.id)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("orders.assigned", number=order.number, user=designer.mention))
        gtr = self.gtr(interaction.guild_id)
        await self.bot.audit.log(interaction.guild, "orders", "order.assign",
                                 title=gtr("orders.log.assigned", number=order.number), emoji="designer",
                                 actor=interaction.user, target_id=designer.id,
                                 lines=[gtr("orders.log.designer", user=designer.mention)])
        self.schedule_queue_update(order.guild_id)

    @order_group.command(name="cancel", description=T("order.cancel"))
    @app_commands.describe(reason=T("order.cancel.reason"), number=T("order.number"))
    @require_staff()
    async def cancel_cmd(self, interaction: discord.Interaction[Any], reason: str | None = None,
                         number: int | None = None) -> None:
        order = await self._resolve(interaction, number)
        await interaction.response.defer(ephemeral=True, thinking=True)
        order = await self.apply_status(order, "cancelled", interaction.user, note=reason)
        tr = self.tr(interaction)
        await interaction.followup.send(view=self.bot.notice(kind="success", title=tr("orders.cancel.done")),
                                        ephemeral=True)

    @order_group.command(name="new", description=T("order.new"))
    @require_staff()
    async def new_cmd(self, interaction: discord.Interaction[Any]) -> None:
        if self.tickets is None:
            raise NotFound("orders.errors.no_order_here")
        ticket = await self.tickets.service.by_channel(interaction.channel_id or 0)
        channel = interaction.channel
        if ticket is None or not isinstance(channel, discord.TextChannel):
            raise NotFound("tickets.errors.not_a_ticket")
        await interaction.response.defer(ephemeral=True, thinking=True)
        order = await self.send_wizard(ticket, channel)
        tr = self.tr(interaction)
        await interaction.followup.send(view=self.bot.notice(kind="success",
                                                             title=tr("orders.wizard_sent", number=order.number)),
                                        ephemeral=True)

    @order_group.command(name="custom", description=T("order.custom"))
    @app_commands.describe(name=T("order.custom.name"), price=T("order.custom.price"), quantity=T("order.custom.quantity"))
    @require_staff()
    async def custom_cmd(self, interaction: discord.Interaction[Any], name: app_commands.Range[str, 1, 100],
                         price: str, quantity: app_commands.Range[int, 1, 100] = 1) -> None:
        order = await self.order_here(interaction)
        if order.status != "draft":
            raise UserError("orders.errors.locked")
        cents = parse_money(price)
        if cents is None:
            raise UserError("errors.invalid_amount")
        await self.orders.add_custom_item(order, name, cents, quantity)
        order = await self.orders.recalculate(order.id, self.shop, money=self.money(order.locale))
        await self.refresh_wizard(order, "products")
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("orders.custom_added", name=name,
                                                            amount=self.money(order.locale)(cents * quantity)))

    @order_group.command(name="note", description=T("order.note"))
    @app_commands.describe(text=T("order.note.text"))
    @require_staff()
    async def note_cmd(self, interaction: discord.Interaction[Any], text: app_commands.Range[str, 1, 1000]) -> None:
        order = await self.order_here(interaction)
        await self.orders.set_notes(order, text)
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("common.saved"))

    # ================================================================== todos
    async def _post_todo_panel(self, order: Order, channel: discord.TextChannel) -> None:
        todos = await self.orders.todos(order.id)
        message = await channel.send(view=views.render_todos(self.bot.theme, self.bot.i18n.translator(order.locale),
                                                             order, todos))
        await self.orders.set_todo_message(order, message.id)

    async def _refresh_todo_panel(self, order: Order) -> None:
        ticket = await self._ticket_of(order)
        if ticket is None or not order.todo_message_id:
            return
        channel = self.bot.get_channel(ticket.channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            return
        view = views.render_todos(self.bot.theme, self.bot.i18n.translator(order.locale), order,
                                  await self.orders.todos(order.id))
        try:
            await channel.get_partial_message(order.todo_message_id).edit(view=view)
        except discord.HTTPException:
            _log.debug("Could not refresh todo panel of order %s", order.id)

    @todo_group.command(name="show", description=T("todo.show"))
    async def todo_show(self, interaction: discord.Interaction[Any]) -> None:
        order = await self.order_here(interaction)
        if interaction.user.id != order.customer_id:
            ensure_staff(self.bot, interaction.user)
        channel = interaction.channel
        assert isinstance(channel, discord.TextChannel)
        await interaction.response.defer(ephemeral=True)
        await self._post_todo_panel(order, channel)
        await interaction.followup.send(view=self.bot.notice(kind="success", title=self.tr(interaction)("common.saved")),
                                        ephemeral=True)

    @todo_group.command(name="add", description=T("todo.add"))
    @app_commands.describe(text=T("todo.add.text"))
    @require_staff()
    async def todo_add_cmd(self, interaction: discord.Interaction[Any], text: app_commands.Range[str, 1, 200]) -> None:
        order = await self.order_here(interaction)
        await self.orders.add_todo(order.id, text)
        await self._refresh_todo_panel(order)
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("orders.todos.added", text=text))

    @component("todo.toggle")
    async def on_todo_toggle(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_staff(self.bot, interaction.user)
        order = await self.orders.get(ctx.int_arg(0), interaction.guild_id)
        for value in ctx.values:
            await self.orders.toggle_todo(order.id, int(value), interaction.user.id)
        await interaction.response.edit_message(view=views.render_todos(
            self.bot.theme, self.bot.i18n.translator(order.locale), order, await self.orders.todos(order.id)))

    @component("todo.add")
    async def on_todo_add(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_staff(self.bot, interaction.user)
        order = await self.orders.get(ctx.int_arg(0), interaction.guild_id)
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            await self.orders.add_todo(order.id, str(values["text"]))
            await modal_interaction.response.edit_message(view=views.render_todos(
                self.bot.theme, self.bot.i18n.translator(order.locale), order, await self.orders.todos(order.id)))

        await interaction.response.send_modal(FormModal(
            self.bot, title=tr("orders.todos.add"),
            fields=[TextField(key="text", label=tr("orders.todos.field"), max_length=200)], on_submit=submitted))

    @component("todo.rmmenu")
    async def on_todo_remove_menu(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_staff(self.bot, interaction.user)
        order = await self.orders.get(ctx.int_arg(0), interaction.guild_id)
        todos = await self.orders.todos(order.id)
        tr = self.tr(interaction)
        select = string_select(
            cid("todo.rm", order.id),
            [select_option(self.bot.theme, truncate(t.text, 90), str(t.id), emoji="remove") for t in todos[:25]],
            placeholder=tr("orders.todos.remove_placeholder"), max_values=min(25, len(todos)) or 1,
        )
        await interaction.response.send_message(
            view=self.bot.notice(kind="info", title=tr("orders.todos.remove"), rows=[[select]]), ephemeral=True
        )

    @component("todo.rm")
    async def on_todo_remove(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_staff(self.bot, interaction.user)
        order = await self.orders.get(ctx.int_arg(0), interaction.guild_id)
        for value in ctx.values:
            await self.orders.remove_todo(order.id, int(value))
        tr = self.tr(interaction)
        await interaction.response.edit_message(view=self.bot.notice(kind="success", title=tr("common.saved")))
        await self._refresh_todo_panel(order)

    # ================================================================== statistics
    @app_commands.command(name="orderstats", description=T("orderstats"))
    @app_commands.describe(period=T("orderstats.period"))
    @app_commands.choices(period=[app_commands.Choice(name=T(f"period.{k}"), value=k) for k in PERIODS])
    @app_commands.guild_only()
    @require_staff()
    async def stats_cmd(self, interaction: discord.Interaction[Any], period: str = "30d") -> None:
        assert interaction.guild_id is not None
        days = PERIODS.get(period, 30)
        since = utcnow() - dt.timedelta(days=days) if days else None
        tr = self.tr(interaction)
        stats = await self.orders.stats(interaction.guild_id, since)
        await interaction.response.send_message(
            view=views.render_stats(self.bot.theme, tr, stats, self.money(self.bot.locale_for(interaction)),
                                    tr(f"orders.stats.period.{period}")),
            ephemeral=True,
        )
