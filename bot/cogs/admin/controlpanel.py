"""``/controlpanel`` – central admin panel (Components V2) with pluggable sections."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands, ui

from ... import __version__
from ...config.settings import WEEKDAYS
from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.controlpanel import ControlSection
from ...core.errors import NotFound, UserError
from ...core.features import FEATURE_KEYS
from ...core.i18n import Translator
from ...core.permissions import ensure_admin, require_admin
from ...core.router import ComponentContext, cid, component
from ...core.utils import format_money, parse_date, parse_duration, parse_money, truncate, ts, utcnow
from ...services.orders import OrderService
from ...services.shop import ShopService
from ...ui.builder import CV2, Bullet, button, select_option, string_select
from ...ui.layouts import build_confirm
from ...ui.modals import FormModal, SelectField, TextField

if TYPE_CHECKING:
    from ..stats.cog import StatsCog
    from ..tickets.blacklist import BlacklistCog
    from ..tickets.cog import TicketsCog


class ControlPanelCog(XCog, name="ControlPanel"):
    feature = "admin"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.shop = ShopService(bot.db)
        self.orders = OrderService(bot.db, bot.config)

    async def on_load(self) -> None:
        for section in (
            ControlSection("home", "settings", 0, None, self._home),
            ControlSection("tickets", "ticket", 10, "tickets", self._tickets),
            ControlSection("products", "product", 20, "shop", self._products),
            ControlSection("coupons", "coupon", 30, "shop", self._coupons),
            ControlSection("hours", "clock", 40, "tickets", self._hours),
            ControlSection("features", "toggle_on", 80, None, self._features),
            ControlSection("stats", "stats", 90, "stats", self._stats),
        ):
            self.bot.control_sections.register(section)

    async def on_unload(self) -> None:
        for key in ("home", "tickets", "products", "coupons", "hours", "features", "stats"):
            self.bot.control_sections.unregister(key)

    def money(self, locale: str) -> Any:
        return lambda cents: format_money(cents, self.bot.config.shop.currency, locale)

    # ================================================================== frame
    async def build(self, interaction: discord.Interaction[Any], key: str = "home") -> ui.LayoutView:
        tr = self.tr(interaction)
        guild_id = interaction.guild_id

        def enabled(feature: str | None) -> bool:
            return self.bot.features.is_enabled(guild_id, feature)

        sections = self.bot.control_sections.available(enabled)
        section = next((s for s in sections if s.key == key), sections[0])
        b = CV2(self.bot.theme)
        b.banner("controlpanel")
        b.heading(tr(f"controlpanel.sections.{section.key}"), emoji=section.emoji,
                  subline=tr("controlpanel.subline", guild=interaction.guild.name if interaction.guild else ""))
        await section.render(interaction, b, tr)
        self._nav(b, tr, sections, section.key)
        return b.build()

    def _nav(self, b: CV2, tr: Translator, sections: list[ControlSection], current: str) -> None:
        other = tr.other()
        b.separator()
        b.actions(string_select(
            cid("cp.nav"),
            [select_option(self.bot.theme, tr(f"controlpanel.sections.{s.key}"), s.key,
                           description=other(f"controlpanel.sections.{s.key}"), emoji=s.emoji,
                           default=s.key == current) for s in sections],
            placeholder=tr("controlpanel.nav"),
        ))

    async def _show(self, interaction: discord.Interaction[Any], key: str) -> None:
        await interaction.response.edit_message(view=await self.build(interaction, key))

    def _back(self, tr: Translator, key: str) -> ui.Button[Any]:
        return button(self.bot.theme, tr("common.back"), emoji="back", custom_id=cid("cp.go", key))

    @app_commands.command(name="controlpanel", description=T("controlpanel"))
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @require_admin()
    async def controlpanel(self, interaction: discord.Interaction[Any]) -> None:
        await interaction.response.send_message(view=await self.build(interaction), ephemeral=True)

    @component("cp.nav")
    async def on_nav(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        await self._show(interaction, ctx.values[0] if ctx.values else "home")

    @component("cp.go")
    async def on_go(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        await self._show(interaction, ctx.arg(0) or "home")

    # ================================================================== home
    async def _home(self, interaction: discord.Interaction[Any], b: CV2, tr: Translator) -> None:
        guild_id = interaction.guild_id or 0
        open_tickets = await self.bot.db.fetchval(
            "SELECT count(*) FROM tickets WHERE guild_id = $1 AND status = 'open'", guild_id)
        queue = len(await self.orders.queue(guild_id))
        stats = await self.orders.stats(guild_id, utcnow() - dt.timedelta(days=30))
        active = sum(1 for k in FEATURE_KEYS if self.bot.features.is_enabled(guild_id, k))
        b.quote(tr("controlpanel.home.intro"))
        b.bullets([
            Bullet(tr("controlpanel.home.open_tickets"), str(open_tickets), emoji="ticket"),
            Bullet(tr("controlpanel.home.queue"), str(queue), emoji="queue"),
            Bullet(tr("controlpanel.home.revenue"), self.money(tr.locale)(stats.revenue_cents), emoji="money"),
            Bullet(tr("controlpanel.home.features"), f"{active} / {len(FEATURE_KEYS)}", emoji="toggle_on"),
            Bullet(tr("controlpanel.home.version"), f"`{__version__}` · API `{self.bot.api_mode()}`", emoji="info"),
        ])

    # ================================================================== tickets
    async def _tickets(self, interaction: discord.Interaction[Any], b: CV2, tr: Translator) -> None:
        guild_id = interaction.guild_id or 0
        rows = await self.bot.db.fetch(
            """
            SELECT t.code, t.channel_id, t.owner_id, t.created_at, c.name_de, c.name_en
              FROM tickets t JOIN ticket_categories c ON c.id = t.category_id
             WHERE t.guild_id = $1 AND t.status = 'open' ORDER BY t.created_at DESC LIMIT 10
            """,
            guild_id,
        )
        total_open = await self.bot.db.fetchval(
            "SELECT count(*) FROM tickets WHERE guild_id = $1 AND status = 'open'", guild_id)
        b.quote(tr("controlpanel.tickets.intro", count=total_open))
        if rows:
            b.text("\n".join(
                f"{self.bot.icons['ticket']} <#{r['channel_id']}> · `{r['code'].strip()}` · <@{r['owner_id']}> · "
                f"{r['name_de'] if tr.locale == 'de' else r['name_en']} · {ts(r['created_at'], 'R')}" for r in rows))
        b.separator()
        b.actions(ui.ChannelSelect(custom_id=cid("cp.tk.panel"), channel_types=[discord.ChannelType.text],
                                   placeholder=tr("controlpanel.tickets.send_panel")))
        b.actions(button(self.bot.theme, tr("controlpanel.tickets.blacklist"), emoji="blacklist",
                         custom_id=cid("cp.tk.blacklist")))

    @component("cp.tk.panel")
    async def on_ticket_panel(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        tickets: TicketsCog | None = self.bot.get_cog("Tickets")  # type: ignore[assignment]
        if tickets is None or interaction.guild is None or not ctx.resolved_values:
            raise UserError("errors.feature_disabled")
        channel = interaction.guild.get_channel(ctx.resolved_values[0].id)
        if not isinstance(channel, discord.TextChannel):
            raise UserError("errors.invalid_input")
        await channel.send(view=await tickets.render_panel(interaction.guild.id, self.bot.guild_locale(interaction.guild.id)))
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("tickets.panel_sent", channel=channel.mention))

    @component("cp.tk.blacklist")
    async def on_blacklist(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        cog: BlacklistCog | None = self.bot.get_cog("Blacklist")  # type: ignore[assignment]
        if cog is None:
            raise UserError("errors.feature_disabled")
        await interaction.response.send_message(view=await cog.render(interaction.guild_id or 0, self.tr(interaction)),
                                                ephemeral=True)

    # ================================================================== products
    async def _products(self, interaction: discord.Interaction[Any], b: CV2, tr: Translator) -> None:
        products = await self.shop.products(interaction.guild_id or 0, active_only=False)
        money = self.money(tr.locale)
        b.quote(tr("controlpanel.products.intro", count=len(products)))
        if products:
            b.text("\n".join(
                f"{self.bot.icons['toggle_on' if p.active else 'toggle_off']} `{p.sku}` **{p.name(tr.locale)}** · "
                f"{money(p.price_cents)}" for p in products[:20]))
            b.actions(string_select(
                cid("cp.prod.pick"),
                [select_option(self.bot.theme, truncate(p.name(tr.locale), 90), str(p.id),
                               description=f"{p.sku} · {money(p.price_cents)}", emoji=p.emoji or "product")
                 for p in products[:25]],
                placeholder=tr("controlpanel.products.pick"),
            ))
        b.actions(button(self.bot.theme, tr("controlpanel.products.new"), emoji="add", custom_id=cid("cp.prod.new"),
                         style=discord.ButtonStyle.success))

    async def _product_detail(self, interaction: discord.Interaction[Any], product_id: int) -> ui.LayoutView:
        tr = self.tr(interaction)
        p = await self.shop.product(interaction.guild_id or 0, product_id)
        money = self.money(tr.locale)
        b = CV2(self.bot.theme)
        b.banner("controlpanel")
        b.heading(p.name(tr.locale), emoji=p.emoji or "product", subline=f"`{p.sku}`")
        b.bullets([
            Bullet(tr("controlpanel.products.price"), money(p.price_cents), emoji="money"),
            Bullet(tr("controlpanel.products.status"), tr("common.enabled" if p.active else "common.disabled"),
                   emoji="toggle_on" if p.active else "toggle_off"),
            Bullet("DE", p.name_de, emoji="language", sub=[p.description_de] if p.description_de else ()),
            Bullet("EN", p.name_en, emoji="language", sub=[p.description_en] if p.description_en else ()),
        ])
        b.separator()
        b.actions(
            self._back(tr, "products"),
            button(self.bot.theme, tr("common.edit"), emoji="edit", custom_id=cid("cp.prod.edit", p.id)),
            button(self.bot.theme, tr("common.disabled" if p.active else "common.enabled"),
                   emoji="toggle_off" if p.active else "toggle_on", custom_id=cid("cp.prod.toggle", p.id)),
            button(self.bot.theme, tr("common.delete"), emoji="delete", custom_id=cid("cp.prod.del", p.id),
                   style=discord.ButtonStyle.danger),
        )
        return b.build()

    @component("cp.prod.pick")
    async def on_product_pick(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        await interaction.response.edit_message(view=await self._product_detail(interaction, int(ctx.values[0])))

    def _product_fields(self, tr: Translator, product: Any = None) -> list[Any]:
        fields: list[Any] = []
        if product is None:
            fields.append(TextField(key="sku", label=tr("controlpanel.products.sku"), max_length=40))
        fields += [
            TextField(key="name_de", label=tr("controlpanel.products.name_de"), max_length=80,
                      default=product.name_de if product else None),
            TextField(key="name_en", label=tr("controlpanel.products.name_en"), max_length=80,
                      default=product.name_en if product else None),
            TextField(key="price", label=tr("controlpanel.products.price"), max_length=12, placeholder="19,99",
                      default=f"{product.price_cents / 100:.2f}".replace(".", ",") if product else None),
            TextField(key="description_de", label=tr("controlpanel.products.description_de"), paragraph=True,
                      required=False, max_length=500, default=product.description_de if product else None),
        ]
        if product is not None:
            fields.append(TextField(key="description_en", label=tr("controlpanel.products.description_en"),
                                    paragraph=True, required=False, max_length=500, default=product.description_en))
        return fields

    @component("cp.prod.new")
    async def on_product_new(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            cents = parse_money(values["price"])
            if cents is None:
                raise UserError("errors.invalid_amount")
            await self.shop.create_product(modal_interaction.guild_id or 0, sku=values["sku"], name_de=values["name_de"],
                                           name_en=values["name_en"], price_cents=cents,
                                           description_de=values.get("description_de") or "")
            await modal_interaction.response.edit_message(view=await self.build(modal_interaction, "products"))
            await self.bot.audit.log(modal_interaction.guild, "orders", "product.add",
                                     title=self.gtr(modal_interaction.guild_id)("shop.log.product_added"),
                                     emoji="product", actor=modal_interaction.user,
                                     lines=[f"`{values['sku']}` · {values['name_de']}"])

        await interaction.response.send_modal(FormModal(self.bot, title=tr("controlpanel.products.new"),
                                                        fields=self._product_fields(tr), on_submit=submitted))

    @component("cp.prod.edit")
    async def on_product_edit(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        tr = self.tr(interaction)
        product = await self.shop.product(interaction.guild_id or 0, ctx.int_arg(0))

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            cents = parse_money(values["price"])
            if cents is None:
                raise UserError("errors.invalid_amount")
            await self.shop.update_product(modal_interaction.guild_id or 0, product.id, name_de=values["name_de"],
                                           name_en=values["name_en"], price_cents=cents,
                                           description_de=values.get("description_de") or "",
                                           description_en=values.get("description_en") or "")
            await modal_interaction.response.edit_message(view=await self._product_detail(modal_interaction, product.id))

        await interaction.response.send_modal(FormModal(self.bot, title=tr("common.edit"),
                                                        fields=self._product_fields(tr, product), on_submit=submitted))

    @component("cp.prod.toggle")
    async def on_product_toggle(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        product = await self.shop.product(interaction.guild_id or 0, ctx.int_arg(0))
        await self.shop.update_product(interaction.guild_id or 0, product.id, active=not product.active)
        await interaction.response.edit_message(view=await self._product_detail(interaction, product.id))

    @component("cp.prod.del")
    async def on_product_delete(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        tr = self.tr(interaction)
        product = await self.shop.product(interaction.guild_id or 0, ctx.int_arg(0))
        await interaction.response.edit_message(view=build_confirm(
            self.bot.theme, tr, title=tr("controlpanel.products.delete_title"),
            description=tr("controlpanel.products.delete_text", name=product.name(tr.locale)),
            confirm_id=cid("cp.prod.del_ok", product.id), cancel_id=cid("cp.go", "products"), danger=True))

    @component("cp.prod.del_ok")
    async def on_product_delete_ok(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        await self.shop.delete_product(interaction.guild_id or 0, ctx.int_arg(0))
        await self._show(interaction, "products")

    # ================================================================== coupons
    async def _coupons(self, interaction: discord.Interaction[Any], b: CV2, tr: Translator) -> None:
        coupons = await self.shop.coupons(interaction.guild_id or 0)
        money = self.money(tr.locale)
        b.quote(tr("controlpanel.coupons.intro", count=len(coupons)))
        if coupons:
            lines = []
            for c in coupons[:20]:
                state = "toggle_on" if c.active and not c.expired and not c.exhausted else "toggle_off"
                value = f"{c.value} %" if c.kind == "percent" else money(c.value)
                uses = f"{c.uses}/{c.max_uses if c.max_uses is not None else '∞'}"
                lines.append(f"{self.bot.icons[state]} `{c.code}` **{value}** · {uses}"
                             + (f" · {ts(c.expires_at, 'd')}" if c.expires_at else ""))
            b.text("\n".join(lines))
            b.actions(string_select(
                cid("cp.cpn.toggle"),
                [select_option(self.bot.theme, c.code, str(c.id), emoji="coupon",
                               description=tr("common.enabled" if c.active else "common.disabled")) for c in coupons[:25]],
                placeholder=tr("controlpanel.coupons.toggle"),
            ))
        b.actions(button(self.bot.theme, tr("controlpanel.coupons.new"), emoji="add", custom_id=cid("cp.cpn.new"),
                         style=discord.ButtonStyle.success))

    @component("cp.cpn.toggle")
    async def on_coupon_toggle(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        for value in ctx.values:
            coupon = await self.shop.coupon(interaction.guild_id or 0, int(value))
            await self.shop.set_coupon_active(interaction.guild_id or 0, coupon.id, not coupon.active)
        await self._show(interaction, "coupons")

    @component("cp.cpn.new")
    async def on_coupon_new(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            kind = (values.get("kind") or ["percent"])[0]
            raw_value = str(values["value"]).strip().rstrip("%")
            amount = int(raw_value) if kind == "percent" and raw_value.isdigit() else parse_money(raw_value)
            if not amount:
                raise UserError("errors.invalid_amount")
            expires_at = None
            if values.get("expires"):
                delta = parse_duration(values["expires"])
                day = parse_date(values["expires"]) if delta is None else None
                if delta is None and day is None:
                    raise UserError("errors.invalid_date")
                expires_at = utcnow() + delta if delta else dt.datetime.combine(day, dt.time(23, 59), tzinfo=dt.UTC)
            max_uses = int(values["max_uses"]) if str(values.get("max_uses") or "").isdigit() else None
            await self.shop.create_coupon(modal_interaction.guild_id or 0, code=values["code"], kind=kind, value=amount,
                                          max_uses=max_uses, expires_at=expires_at,
                                          created_by=modal_interaction.user.id)
            await modal_interaction.response.edit_message(view=await self.build(modal_interaction, "coupons"))

        await interaction.response.send_modal(FormModal(self.bot, title=tr("controlpanel.coupons.new"), fields=[
            TextField(key="code", label=tr("controlpanel.coupons.code"), max_length=40),
            SelectField(key="kind", label=tr("controlpanel.coupons.kind"), options=[
                select_option(self.bot.theme, tr("controlpanel.coupons.percent"), "percent", emoji="coupon", default=True),
                select_option(self.bot.theme, tr("controlpanel.coupons.fixed"), "fixed", emoji="money"),
            ]),
            TextField(key="value", label=tr("controlpanel.coupons.value"), max_length=12, placeholder="10"),
            TextField(key="max_uses", label=tr("controlpanel.coupons.max_uses"), required=False, max_length=7),
            TextField(key="expires", label=tr("controlpanel.coupons.expires"), required=False, max_length=12,
                      placeholder="30d / 31.12.2026"),
        ], on_submit=submitted))

    # ================================================================== opening hours
    async def _hours(self, interaction: discord.Interaction[Any], b: CV2, tr: Translator) -> None:
        tickets: TicketsCog | None = self.bot.get_cog("Tickets")  # type: ignore[assignment]
        if tickets is None:
            b.quote(tr("errors.feature_disabled"))
            return
        guild_id = interaction.guild_id or 0
        schedule = await tickets.hours.schedule(guild_id)
        holidays = await tickets.hours.holidays(guild_id)
        b.quote(tr("controlpanel.hours.intro"))
        b.bullets([Bullet(tr(f"tickets.hours.days_short.{d}"), schedule.get(d) or tr("tickets.hours.closed"),
                          emoji="calendar") for d in WEEKDAYS])
        upcoming = sorted((d, n) for d, n in holidays.items() if d >= dt.date.today())
        if upcoming:
            b.block_break()
            b.text(f"**{tr('tickets.hours.holidays')}**\n" + "\n".join(
                f"{self.bot.icons['holiday']} `{d:%d.%m.%Y}` {n}" for d, n in upcoming[:10]))
        b.actions(string_select(
            cid("cp.hours.day"),
            [select_option(self.bot.theme, tr(f"tickets.hours.days_short.{d}"), d, emoji="calendar",
                           description=schedule.get(d) or tr("tickets.hours.closed")) for d in WEEKDAYS],
            placeholder=tr("controlpanel.hours.pick_day"),
        ))
        if upcoming:
            b.actions(string_select(
                cid("cp.hours.hdel"),
                [select_option(self.bot.theme, f"{d:%d.%m.%Y}", d.isoformat(), description=truncate(n or "—", 90),
                               emoji="remove") for d, n in upcoming[:25]],
                placeholder=tr("controlpanel.hours.remove_holiday"),
            ))
        b.actions(
            button(self.bot.theme, tr("controlpanel.hours.add_holiday"), emoji="holiday", custom_id=cid("cp.hours.hadd")),
            button(self.bot.theme, tr("controlpanel.hours.reset"), emoji="refresh", custom_id=cid("cp.hours.reset")),
        )

    def _hours_service(self) -> Any:
        tickets: TicketsCog | None = self.bot.get_cog("Tickets")  # type: ignore[assignment]
        if tickets is None:
            raise UserError("errors.feature_disabled")
        return tickets.hours

    @component("cp.hours.day")
    async def on_hours_day(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        hours = self._hours_service()
        day = ctx.values[0]
        current = (await hours.schedule(interaction.guild_id or 0)).get(day)
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            raw = str(values["span"]).strip()
            try:
                await hours.set_day(modal_interaction.guild_id or 0, day,
                                    None if raw.lower() in {"", "off", "zu", "closed", "-"} else raw)
            except ValueError as exc:
                raise UserError("tickets.errors.invalid_span") from exc
            await modal_interaction.response.edit_message(view=await self.build(modal_interaction, "hours"))

        await interaction.response.send_modal(FormModal(self.bot, title=tr(f"tickets.hours.days_short.{day}"), fields=[
            TextField(key="span", label=tr("controlpanel.hours.span"), required=False, max_length=11,
                      default=current, placeholder="09:00-18:00", description=tr("controlpanel.hours.span_hint")),
        ], on_submit=submitted))

    @component("cp.hours.hadd")
    async def on_holiday_add(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        hours = self._hours_service()
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            day = parse_date(values["date"])
            if day is None:
                raise UserError("errors.invalid_date")
            await hours.add_holiday(modal_interaction.guild_id or 0, day, values["name"], modal_interaction.user.id)
            await modal_interaction.response.edit_message(view=await self.build(modal_interaction, "hours"))

        await interaction.response.send_modal(FormModal(self.bot, title=tr("controlpanel.hours.add_holiday"), fields=[
            TextField(key="date", label=tr("controlpanel.hours.date"), max_length=10, placeholder="24.12.2026"),
            TextField(key="name", label=tr("controlpanel.hours.name"), max_length=100),
        ], on_submit=submitted))

    @component("cp.hours.hdel")
    async def on_holiday_remove(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        hours = self._hours_service()
        for value in ctx.values:
            await hours.remove_holiday(interaction.guild_id or 0, dt.date.fromisoformat(value))
        await self._show(interaction, "hours")

    @component("cp.hours.reset")
    async def on_hours_reset(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        await self._hours_service().reset(interaction.guild_id or 0)
        await self._show(interaction, "hours")

    # ================================================================== feature flags
    async def _features(self, interaction: discord.Interaction[Any], b: CV2, tr: Translator) -> None:
        guild_id = interaction.guild_id or 0
        b.quote(tr("controlpanel.features.intro"))
        lines = []
        for key in FEATURE_KEYS:
            if not self.bot.features.loaded(key):
                lines.append(f"{self.bot.icons['lock']} **{tr(f'controlpanel.features.names.{key}')}** · "
                             f"{tr('controlpanel.features.config_off')}")
            else:
                on = self.bot.features.is_enabled(guild_id, key)
                lines.append(f"{self.bot.icons['toggle_on' if on else 'toggle_off']} "
                             f"**{tr(f'controlpanel.features.names.{key}')}**")
        b.text("\n".join(lines))
        loaded = [k for k in FEATURE_KEYS if self.bot.features.loaded(k) and k != "admin"]
        b.actions(string_select(
            cid("cp.feat.set"),
            [select_option(self.bot.theme, tr(f"controlpanel.features.names.{k}"), k,
                           description=tr.other()(f"controlpanel.features.names.{k}"),
                           default=self.bot.features.is_enabled(guild_id, k)) for k in loaded],
            placeholder=tr("controlpanel.features.placeholder"), min_values=0, max_values=len(loaded),
        ))

    @component("cp.feat.set")
    async def on_features(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        guild_id = interaction.guild_id or 0
        selected = set(ctx.values)
        changed = []
        for key in FEATURE_KEYS:
            if key == "admin" or not self.bot.features.loaded(key):
                continue
            enabled = key in selected
            if enabled != self.bot.features.is_enabled(guild_id, key):
                await self.bot.features.set_enabled(guild_id, key, enabled)
                changed.append(f"`{key}` → {'on' if enabled else 'off'}")
        await self._show(interaction, "features")
        if changed:
            await self.bot.audit.log(interaction.guild, "commands", "features.update",
                                     title=self.gtr(guild_id)("controlpanel.features.log"), emoji="toggle_on",
                                     actor=interaction.user, lines=changed)

    # ================================================================== statistics
    async def _stats(self, interaction: discord.Interaction[Any], b: CV2, tr: Translator) -> None:
        b.quote(tr("controlpanel.stats.intro"))
        kinds = ["overview", "sales", "designers", "tickets", "reviews", "customers"]
        if self.bot.get_cog("Downloads") is not None:
            kinds.append("downloads")
        b.actions(string_select(
            cid("cp.stats.kind"),
            [select_option(self.bot.theme, tr(f"controlpanel.stats.kinds.{k}"), k, emoji="stats",
                           description=tr.other()(f"controlpanel.stats.kinds.{k}")) for k in kinds],
            placeholder=tr("controlpanel.stats.pick"),
        ))

    @component("cp.stats.kind")
    async def on_stats(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_admin(self.bot, interaction.user)
        kind = ctx.values[0] if ctx.values else "overview"
        locale = self.bot.locale_for(interaction)
        if kind == "downloads":
            downloads = self.bot.get_cog("Downloads")
            if downloads is None:
                raise NotFound()
            view = await downloads.render_stats(interaction.guild_id or 0, locale)  # type: ignore[attr-defined]
        else:
            stats: StatsCog | None = self.bot.get_cog("Stats")  # type: ignore[assignment]
            if stats is None:
                raise UserError("errors.feature_disabled")
            view = await stats.render(kind, interaction.guild_id or 0, locale, "30d")
        await interaction.response.send_message(view=view, ephemeral=True)
