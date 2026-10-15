"""Payments: Stripe payment links, invoices, legal documents, API keys and GDPR self-service."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from ...core.cog import XCog, bus_event
from ...core.command_i18n import T
from ...core.db import dumps
from ...core.errors import NotFound, UserError
from ...core.logger import get_logger
from ...core.permissions import ensure_staff, is_staff, require_admin, require_staff
from ...core.router import ComponentContext, cid, component
from ...core.utils import format_money, truncate, ts
from ...services.apikeys import SCOPES, ApiKeyService
from ...services.customers import CustomerDataService
from ...services.invoices import InvoiceService
from ...services.legal import DOCUMENTS, load_document, strip_markdown
from ...services.orders import Order, OrderService
from ...services.payments import STRIPE_MIN_AMOUNT_CENTS, PaymentRepository, StripeService
from ...ui.builder import CV2, Bullet, button
from ...ui.layouts import build_confirm

if TYPE_CHECKING:
    from ..orders.cog import OrdersCog

_log = get_logger("payments")
SCOPE_PRESETS: dict[str, list[str]] = {
    "read": ["orders:read", "customers:read", "products:read", "invoices:read"],
    "full": list(SCOPES),
}


class PaymentsCog(XCog, name="Payments"):
    feature = "payments"

    apikey_group = app_commands.Group(name="apikey", description=T("apikey"), guild_only=True,
                                      default_permissions=discord.Permissions(administrator=True))
    privacy_group = app_commands.Group(name="privacy", description=T("privacy"), guild_only=True)

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.stripe = StripeService(bot.config, bot.secrets)
        self.payments = PaymentRepository(bot.db)
        self.orders = OrderService(bot.db, bot.config)
        self.invoices = InvoiceService(bot.db, bot.config, bot.i18n)
        self.keys = ApiKeyService(bot.db)
        self.customers = CustomerDataService(bot.db)

    async def on_unload(self) -> None:
        await self.stripe.close()

    @property
    def orders_cog(self) -> OrdersCog | None:
        return self.bot.get_cog("Orders")  # type: ignore[return-value]

    # ================================================================== payment links
    def _labels(self, order: Order) -> dict[str, str]:
        tr = self.bot.i18n.translator(order.locale)
        design = next((s for s in self.bot.config.shop.seasonal_designs if s.key == order.seasonal_key), None)
        return {
            "express": tr("orders.totals.express"),
            "seasonal": tr("orders.totals.seasonal", name=design.name(order.locale) if design else ""),
            "discount": tr("orders.totals.discount", code=order.coupon_code or ""),
            "order": tr("orders.number", number=order.number) + f" · {self.bot.config.bot.shop_name}",
        }

    async def payment_url_for(self, order: Order, *, force_new: bool = False) -> str | None:
        """Return a valid Stripe URL for the order (reusing an open one unless ``force_new``)."""
        if not self.stripe.available or order.total_cents < STRIPE_MIN_AMOUNT_CENTS:
            return None
        latest = await self.payments.latest_for_order(order.id)
        if latest is not None and latest.status == "paid":
            raise UserError("payments.errors.already_paid")
        if latest is not None and latest.usable and latest.amount_cents == order.total_cents and not force_new:
            return latest.url
        if latest is not None and latest.status == "open":
            await self.stripe.invalidate(latest)
            await self.payments.mark(latest.id, "cancelled")
        mode = self.bot.config.payments.stripe.mode
        try:
            if mode == "payment_link":
                url, external_id = await self.stripe.create_payment_link(order, self._labels(order))
                expires_at = None
            else:
                url, external_id, expires_at = await self.stripe.create_checkout(order, self._labels(order))
        except Exception as exc:  # noqa: BLE001 – stripe.StripeError and network errors
            _log.exception("Stripe request for order %s failed", order.id)
            raise UserError("payments.errors.stripe") from exc
        await self.payments.create(order, kind=mode if mode == "payment_link" else "checkout",
                                   external_id=external_id, url=url, expires_at=expires_at)
        guild = self.bot.get_guild(order.guild_id)
        gtr = self.gtr(order.guild_id)
        await self.bot.audit.log(
            guild, "payments", "payment.link", title=gtr("payments.log.link", number=order.number), emoji="payment",
            lines=[gtr("orders.log.customer", user=f"<@{order.customer_id}>"),
                   gtr("orders.log.total", amount=format_money(order.total_cents, order.currency,
                                                               self.bot.guild_locale(order.guild_id)))],
            target_id=order.customer_id, details={"order_id": order.id, "external_id": external_id, "mode": mode},
        )
        return url

    @app_commands.command(name="paylink", description=T("paylink"))
    @app_commands.describe(number=T("order.number"))
    @app_commands.guild_only()
    @require_staff()
    async def paylink_cmd(self, interaction: discord.Interaction[Any], number: int | None = None) -> None:
        if self.orders_cog is None:
            raise UserError("errors.feature_disabled")
        order = await (self.orders.get(number, interaction.guild_id) if number else self.orders_cog.order_here(interaction))
        if order.status == "draft":
            order = await self.orders_cog.apply_status(
                await self.orders.recalculate(order.id, self.orders_cog.shop), "awaiting_payment", interaction.user,
                notify=False,
            )
        if order.status != "awaiting_payment":
            raise UserError("orders.errors.not_awaiting_payment")
        await interaction.response.defer(thinking=True)
        url = await self.payment_url_for(order, force_new=True)
        if url is None:
            raise UserError("payments.errors.unavailable")
        from ..orders import views

        await interaction.followup.send(view=views.render_submitted(
            self.bot.theme, self.bot.i18n.translator(order.locale), order,
            lambda c: format_money(c, order.currency, order.locale), self.bot.config, url,
        ))

    # ================================================================== invoices
    @commands.Cog.listener()
    async def on_xs_order_status(self, order: Order, old_status: str) -> None:
        if order.status in ("cancelled", "refunded"):
            latest = await self.payments.latest_for_order(order.id)
            if latest is not None and latest.status == "open":
                await self.stripe.invalidate(latest)
                await self.payments.mark(latest.id, "cancelled")
            return
        first_payment = old_status in ("draft", "awaiting_payment") and order.paid_at is not None
        if not first_payment:
            return
        latest = await self.payments.latest_for_order(order.id)
        if latest is not None and latest.status == "open":
            # paid manually (/bezahlt) while a Stripe link was still open → prevent double payment
            await self.stripe.invalidate(latest)
            await self.payments.mark(latest.id, "cancelled")
        if not self.bot.config.payments.invoice.enabled:
            return
        invoice = await self._ensure_invoice(order)
        if not self.bot.config.payments.invoice.send_on_payment:
            return
        guild = self.bot.get_guild(order.guild_id)
        customer = guild.get_member(order.customer_id) if guild else None
        if customer is None:
            return
        data = await self.invoices.render_pdf(invoice, order)
        tr = self.bot.i18n.translator(order.locale)
        b = CV2(self.bot.theme)
        b.banner("payment")
        b.heading(tr("payments.invoice_dm.title"), emoji="invoice", subline=tr("orders.number", number=order.number))
        b.quote(tr("payments.invoice_dm.text", number=invoice.number))
        b.file(invoice.file_name)
        b.footer(tr("payments.invoice_dm.footer"))
        await self.bot.safe_dm(customer, b.build(), file=discord.File(io.BytesIO(data), filename=invoice.file_name))

    async def _ensure_invoice(self, order: Order) -> Any:
        paid = await self.payments.paid_for_order(order.id)
        name = paid.customer_name if paid else None
        if not name:
            user = self.bot.get_user(order.customer_id)
            name = str(user) if user else None
        return await self.invoices.ensure(
            order, customer_name=name, customer_email=paid.customer_email if paid else None,
            customer_address=paid.customer_address if paid else None,
        )

    @app_commands.command(name="invoice", description=T("invoice"))
    @app_commands.describe(number=T("order.number"))
    @app_commands.guild_only()
    async def invoice_cmd(self, interaction: discord.Interaction[Any], number: int | None = None) -> None:
        if number is not None:
            order = await self.orders.get(number, interaction.guild_id)
        elif self.orders_cog is not None:
            order = await self.orders_cog.order_here(interaction)
        else:
            raise NotFound("orders.errors.no_order_here")
        if order.customer_id != interaction.user.id:
            ensure_staff(self.bot, interaction.user)
        if order.paid_at is None:
            raise UserError("payments.errors.not_paid")
        await interaction.response.defer(ephemeral=True, thinking=True)
        invoice = await self._ensure_invoice(order)
        data = await self.invoices.render_pdf(invoice, order)
        tr = self.tr(interaction)
        b = CV2(self.bot.theme)
        b.heading(tr("payments.invoice_dm.title"), emoji="invoice", level=3, subline=invoice.number)
        b.file(invoice.file_name)
        await interaction.followup.send(view=b.build(), file=discord.File(io.BytesIO(data), filename=invoice.file_name),
                                        ephemeral=True)

    # ================================================================== legal
    @app_commands.command(name="legal", description=T("legal"))
    @app_commands.describe(document=T("legal.document"), public=T("legal.public"))
    @app_commands.choices(document=[app_commands.Choice(name=T(f"legal.doc.{d}"), value=d) for d in DOCUMENTS])
    async def legal_cmd(self, interaction: discord.Interaction[Any], document: str, public: bool = False) -> None:
        if public:
            ensure_staff(self.bot, interaction.user)
        locale = self.bot.guild_locale(interaction.guild_id) if public else self.bot.locale_for(interaction)
        tr = self.bot.i18n.translator(locale)
        text = await load_document(self.bot.config.payments.legal_dir, document, locale)
        if text is None:
            raise UserError("errors.not_configured", what=f"legal/{document}.{locale}.md")
        b = CV2(self.bot.theme)
        b.banner("legal")
        b.heading(tr(f"payments.legal.{document}"), emoji="legal")
        b.text(truncate(strip_markdown(text), 3200))
        if self.bot.api_mode() != "disabled":
            url = f"{self.bot.config.api.public_url.rstrip('/')}/legal/{document}?lang={locale}"
            b.separator()
            b.actions(button(self.bot.theme, tr("payments.legal.open"), emoji="link", url=url))
        await interaction.response.send_message(view=b.build(), ephemeral=not public)

    # ================================================================== API keys
    @apikey_group.command(name="create", description=T("apikey.create"))
    @app_commands.describe(name=T("apikey.name"), access=T("apikey.access"))
    @app_commands.choices(access=[app_commands.Choice(name=T("apikey.access.read"), value="read"),
                                  app_commands.Choice(name=T("apikey.access.full"), value="full")])
    @require_admin()
    async def apikey_create(self, interaction: discord.Interaction[Any], name: app_commands.Range[str, 1, 60],
                            access: str = "read") -> None:
        assert interaction.guild_id is not None
        plain, key = await self.keys.create(interaction.guild_id, name, SCOPE_PRESETS[access], interaction.user.id)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, kind="success", title=tr("payments.apikey.created", name=name),
                                      description=tr("payments.apikey.secret", key=plain,
                                                     scopes=", ".join(key.scopes)))
        await self.bot.audit.log(interaction.guild, "payments", "apikey.create", title=tr("payments.log.apikey_created"),
                                 emoji="lock", actor=interaction.user, lines=[f"`{key.key_prefix}…` · {name}"])

    @apikey_group.command(name="list", description=T("apikey.list"))
    @require_admin()
    async def apikey_list(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        keys = await self.keys.list_keys(interaction.guild_id)
        b = CV2(self.bot.theme)
        b.heading(tr("payments.apikey.list_title"), emoji="lock")
        if keys:
            b.bullets([
                Bullet(f"#{k.id} {k.name}", f"`{k.key_prefix}…`", emoji="toggle_off" if k.revoked_at else "toggle_on",
                       sub=[", ".join(k.scopes),
                            tr("payments.apikey.last_used", when=ts(k.last_used_at, "R") if k.last_used_at else "—")])
                for k in keys[:20]
            ])
        else:
            b.quote(tr("payments.apikey.none"))
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    @apikey_group.command(name="revoke", description=T("apikey.revoke"))
    @app_commands.describe(key_id=T("apikey.key_id"))
    @require_admin()
    async def apikey_revoke(self, interaction: discord.Interaction[Any], key_id: int) -> None:
        assert interaction.guild_id is not None
        if not await self.keys.revoke(interaction.guild_id, key_id):
            raise NotFound()
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("payments.apikey.revoked", id=key_id))
        await self.bot.audit.log(interaction.guild, "payments", "apikey.revoke", title=tr("payments.log.apikey_revoked"),
                                 emoji="lock", actor=interaction.user, lines=[f"#{key_id}"])

    @bus_event("payment.attention")
    async def on_payment_attention(self, guild_id: int | None, payload: dict[str, Any]) -> None:
        """A payment arrived that must be reviewed (duplicate payment or order already cancelled)."""
        guild = self.bot.get_guild(guild_id or 0)
        gtr = self.gtr(guild_id)
        amount = payload.get("amount_cents")
        await self.bot.audit.log(
            guild, "payments", "payment.attention", title=gtr("payments.log.attention", number=payload["order_id"]),
            emoji="warning",
            lines=[gtr(f"payments.attention.{payload.get('reason', 'duplicate')}"),
                   f"`{payload.get('payment_intent') or '—'}`",
                   *([format_money(int(amount), self.bot.config.shop.currency, self.bot.guild_locale(guild_id))]
                     if amount is not None else [])],
            details=payload,
        )

    @bus_event("customer.deleted")
    async def on_customer_deleted(self, guild_id: int | None, payload: dict[str, Any]) -> None:
        guild = self.bot.get_guild(guild_id or 0)
        gtr = self.gtr(guild_id)
        await self.bot.audit.log(guild, "payments", "privacy.delete_api", title=gtr("payments.log.privacy_delete"),
                                 emoji="shield", target_id=int(payload["user_id"]),
                                 lines=[f"`{k}`: {v}" for k, v in (payload.get("result") or {}).items() if v],
                                 details=payload)

    # ================================================================== GDPR self-service
    @privacy_group.command(name="export", description=T("privacy.export"))
    async def privacy_export(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await self.customers.export(interaction.guild_id, interaction.user.id)
        payload = json.dumps(json.loads(dumps(data)), ensure_ascii=False, indent=2).encode("utf-8")
        tr = self.tr(interaction)
        file_name = f"data-export-{interaction.user.id}.json"
        b = CV2(self.bot.theme)
        b.heading(tr("payments.privacy.export_title"), emoji="file", level=3)
        b.quote(tr("payments.privacy.export_text"))
        b.file(file_name)
        await interaction.followup.send(view=b.build(), file=discord.File(io.BytesIO(payload), filename=file_name),
                                        ephemeral=True)

    @privacy_group.command(name="delete", description=T("privacy.delete"))
    async def privacy_delete(self, interaction: discord.Interaction[Any]) -> None:
        tr = self.tr(interaction)
        await interaction.response.send_message(
            view=build_confirm(self.bot.theme, tr, title=tr("payments.privacy.delete_title"),
                               description=tr("payments.privacy.delete_text"),
                               confirm_id=cid("privacy.delete_ok", interaction.user.id), cancel_id=cid("dismiss"),
                               danger=True),
            ephemeral=True,
        )

    @component("privacy.delete_ok")
    async def on_privacy_delete(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        if ctx.int_arg(0) != interaction.user.id and not is_staff(self.bot, interaction.user):
            raise UserError("errors.not_your_component")
        assert interaction.guild_id is not None
        result = await self.customers.delete(interaction.guild_id, ctx.int_arg(0))
        tr = self.tr(interaction)
        await interaction.response.edit_message(view=self.bot.notice(
            kind="success", title=tr("payments.privacy.deleted"),
            description=tr("payments.privacy.retained", count=result.get("orders_retained", 0)),
        ))
        await self.bot.audit.log(interaction.guild, "payments", "privacy.delete", title=tr("payments.log.privacy_delete"),
                                 emoji="shield", target_id=ctx.int_arg(0), post=True,
                                 lines=[f"`{k}`: {v}" for k, v in result.items() if v], details=result)
