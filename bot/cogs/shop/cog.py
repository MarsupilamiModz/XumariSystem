"""Shop: public panels (shop, FAQ, partnership), showcases and catalogue management commands."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import NotFound, UserError
from ...core.i18n import Translator
from ...core.permissions import require_admin, require_staff
from ...core.router import ComponentContext, cid, component
from ...core.utils import format_money, is_url, parse_date, parse_duration, parse_money, truncate, utcnow
from ...services.shop import ShopService
from ...ui.builder import CV2, Bullet, button
from ...ui.layouts import language_button

if TYPE_CHECKING:
    from ..tickets.cog import TicketsCog


class ShopCog(XCog, name="Shop"):
    feature = "shop"

    shop_group = app_commands.Group(name="shop", description=T("shop"), guild_only=True)
    product_group = app_commands.Group(name="product", description=T("product"), guild_only=True,
                                       default_permissions=discord.Permissions(manage_guild=True))
    category_group = app_commands.Group(name="category", description=T("product.category"), parent=product_group)
    bundle_group = app_commands.Group(name="bundle", description=T("bundle"), guild_only=True,
                                      default_permissions=discord.Permissions(manage_guild=True))
    coupon_group = app_commands.Group(name="coupon", description=T("coupon"), guild_only=True,
                                      default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.shop = ShopService(bot.db)

    async def on_load(self) -> None:
        self.bot.renderers.register("shop.panel", self._render_shop_for)
        self.bot.renderers.register("shop.faq", self._render_faq_for)
        self.bot.renderers.register("shop.partner", self._render_partner_for)

    async def on_unload(self) -> None:
        for key in ("shop.panel", "shop.faq", "shop.partner"):
            self.bot.renderers.unregister(key)

    def money(self, locale: str) -> Any:
        currency = self.bot.config.shop.currency
        return lambda cents: format_money(cents, currency, locale)

    @property
    def tickets(self) -> TicketsCog | None:
        return self.bot.get_cog("Tickets")  # type: ignore[return-value]

    def _order_category(self) -> str | None:
        return next((c.key for c in self.bot.config.tickets.categories if c.is_order), None)

    # ================================================================== panels
    async def render_shop(self, guild_id: int, locale: str) -> discord.ui.LayoutView:
        tr = self.bot.i18n.translator(locale)
        money = self.money(locale)
        shop_cfg = self.bot.config.shop
        categories = await self.shop.categories(guild_id)
        products = await self.shop.products(guild_id)
        bundles = await self.shop.bundles(guild_id)
        b = CV2(self.bot.theme)
        b.banner("shop")
        b.heading(tr("shop.panel.title"), emoji="shop", accessory=language_button(self.bot.theme, tr, "shop.panel"))
        b.quote(tr("shop.panel.subtitle"))
        by_category: dict[int | None, list[Any]] = {}
        known = {c.id for c in categories}
        for product in products:
            by_category.setdefault(product.category_id if product.category_id in known else None, []).append(product)
        for category in [*categories, None]:
            items = by_category.get(category.id if category else None)
            if not items:
                continue
            b.block_break()
            title = category.name(locale) if category else tr("orders.create.other")
            icon = self.bot.icons.resolve(category.emoji) if category and category.emoji else self.bot.icons["product"]
            b.text(f"### {icon} {title}")
            b.bullets([
                Bullet(p.name(locale), money(p.price_cents), emoji=p.emoji or "product", separator=" –",
                       sub=[truncate(p.description(locale), 120)] if p.description(locale) else ())
                for p in items
            ])
        if bundles:
            b.block_break()
            b.text(f"### {self.bot.icons['bundle']} {tr('shop.panel.bundles')}")
            b.bullets([
                Bullet(bd.name(locale), money(bd.price_cents), emoji=bd.emoji or "bundle", separator=" –",
                       sub=[", ".join(f"{i.quantity}× {i.name_de if locale == 'de' else i.name_en}" for i in bd.items)]
                       if bd.items else ())
                for bd in bundles
            ])
        if not products and not bundles:
            b.text(tr("shop.panel.empty"))
        footer = [tr("shop.panel.delivery", days=shop_cfg.standard_delivery_days)]
        if shop_cfg.express.enabled:
            footer.append(tr("shop.panel.express", days=shop_cfg.express.delivery_days,
                             percent=f"{shop_cfg.express.surcharge_percent:g}"))
        b.footer("\n".join(footer))
        order_category = self._order_category()
        if order_category:
            b.separator()
            b.actions(button(self.bot.theme, tr("shop.panel.order_button"), emoji="cart",
                             custom_id=cid("shop.ticket", order_category, locale), style=discord.ButtonStyle.success))
        return b.build()

    def render_faq(self, locale: str) -> discord.ui.LayoutView:
        tr = self.bot.i18n.translator(locale)
        de = locale == "de"
        b = CV2(self.bot.theme)
        b.banner("faq")
        b.heading(tr("shop.faq.title"), emoji="faq", accessory=language_button(self.bot.theme, tr, "shop.faq"))
        b.quote(tr("shop.faq.subtitle"))
        entries = self.bot.config.shop.faq
        if entries:
            b.bullets([
                Bullet(e.question_de if de else e.question_en, emoji=e.emoji, sub=[e.answer_de if de else e.answer_en])
                for e in entries
            ])
        else:
            b.text(tr("shop.faq.empty"))
        b.footer(tr("shop.faq.footer"))
        support = self.bot.config.shop.faq_ticket_category
        if support and self.bot.config.tickets.category(support):
            b.separator()
            b.actions(button(self.bot.theme, tr("shop.faq.ask"), emoji="question",
                             custom_id=cid("shop.ticket", support, locale)))
        return b.build()

    def render_partnership(self, locale: str) -> discord.ui.LayoutView:
        tr = self.bot.i18n.translator(locale)
        cfg = self.bot.config.shop.partnership
        requirements = cfg.requirements_de if locale == "de" else cfg.requirements_en
        b = CV2(self.bot.theme)
        b.banner("partnership")
        b.heading(tr("shop.partner.title"), emoji="partnership",
                  accessory=language_button(self.bot.theme, tr, "shop.partner"))
        b.quote(tr("shop.partner.subtitle"))
        if requirements:
            b.text(f"**{tr('shop.partner.requirements')}**")
            b.bullets(requirements, default_emoji="success")
        b.footer(tr("shop.partner.footer"))
        if cfg.ticket_category and self.bot.config.tickets.category(cfg.ticket_category):
            b.separator()
            b.actions(button(self.bot.theme, tr("shop.partner.button"), emoji="partnership",
                             custom_id=cid("shop.ticket", cfg.ticket_category, locale),
                             style=discord.ButtonStyle.success))
        return b.build()

    async def _render_shop_for(self, interaction: discord.Interaction[Any], locale: str, args: tuple[str, ...]) -> Any:
        return await self.render_shop(interaction.guild_id or 0, locale)

    async def _render_faq_for(self, interaction: discord.Interaction[Any], locale: str, args: tuple[str, ...]) -> Any:
        return self.render_faq(locale)

    async def _render_partner_for(self, interaction: discord.Interaction[Any], locale: str,
                                  args: tuple[str, ...]) -> Any:
        return self.render_partnership(locale)

    @component("shop.ticket")
    async def on_open_ticket(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        if self.tickets is None or not self.bot.features.is_enabled(interaction.guild_id, "tickets"):
            raise UserError("errors.feature_disabled")
        locale = self.bot.i18n.resolve_locale(ctx.arg(1))
        await self.tickets.start_ticket(interaction, ctx.arg(0) or "", locale)

    async def _post_panel(self, interaction: discord.Interaction[Any], channel: discord.TextChannel | None,
                          view: discord.ui.LayoutView, kind: str) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise UserError("errors.invalid_input")
        message = await target.send(view=view)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("shop.panel_sent", channel=target.mention))
        await self.bot.audit.log(interaction.guild, "orders", f"panel.{kind}", title=tr("shop.log.panel", kind=kind),
                                 emoji="shop", actor=interaction.user, lines=[message.jump_url])

    @shop_group.command(name="panel", description=T("shop.panel"))
    @app_commands.describe(channel=T("shop.channel"))
    @require_admin()
    async def shop_panel(self, interaction: discord.Interaction[Any], channel: discord.TextChannel | None = None) -> None:
        assert interaction.guild_id is not None
        view = await self.render_shop(interaction.guild_id, self.bot.guild_locale(interaction.guild_id))
        await self._post_panel(interaction, channel, view, "shop")

    @shop_group.command(name="faq", description=T("shop.faq"))
    @app_commands.describe(channel=T("shop.channel"))
    @require_admin()
    async def faq_panel(self, interaction: discord.Interaction[Any], channel: discord.TextChannel | None = None) -> None:
        await self._post_panel(interaction, channel, self.render_faq(self.bot.guild_locale(interaction.guild_id)), "faq")

    @shop_group.command(name="partnership", description=T("shop.partnership"))
    @app_commands.describe(channel=T("shop.channel"))
    @require_admin()
    async def partner_panel(self, interaction: discord.Interaction[Any],
                            channel: discord.TextChannel | None = None) -> None:
        await self._post_panel(interaction, channel,
                               self.render_partnership(self.bot.guild_locale(interaction.guild_id)), "partnership")

    # ================================================================== showcase
    @shop_group.command(name="showcase", description=T("shop.showcase"))
    @app_commands.describe(image=T("shop.showcase.image"), customer=T("shop.showcase.customer"),
                           title=T("shop.showcase.title"), description=T("shop.showcase.description"),
                           designer=T("shop.showcase.designer"), order=T("order.number"))
    @require_staff()
    async def showcase(
        self,
        interaction: discord.Interaction[Any],
        image: discord.Attachment,
        title: app_commands.Range[str, 1, 100],
        customer: discord.Member | None = None,
        description: app_commands.Range[str, 1, 1000] | None = None,
        designer: discord.Member | None = None,
        order: int | None = None,
    ) -> None:
        assert interaction.guild is not None
        tr_user = self.tr(interaction)
        if not (image.content_type or "").startswith("image/"):
            raise UserError("shop.errors.not_an_image")
        channel = interaction.guild.get_channel(self.bot.config.shop.showcase_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            raise UserError("errors.not_configured", what="shop.showcase_channel_id")
        await interaction.response.defer(ephemeral=True, thinking=True)
        tr = self.gtr(interaction.guild_id)
        file = await image.to_file()
        designer_member = designer or interaction.user
        b = CV2(self.bot.theme)
        b.banner("showcase")
        b.heading(title, emoji="showcase", subline=tr("shop.showcase.subline", shop=self.bot.config.bot.shop_name))
        b.quote(description)
        info = [Bullet(tr("shop.showcase.designer_label"), designer_member.mention, emoji="designer")]
        if customer:
            info.append(Bullet(tr("shop.showcase.customer_label"), customer.mention, emoji="user"))
        b.bullets(info)
        b.separator()
        b.media(f"attachment://{file.filename}")
        b.footer(tr("shop.showcase.footer"))
        message = await channel.send(view=b.build(), file=file, allowed_mentions=discord.AllowedMentions.none())
        await self.bot.db.execute(
            """
            INSERT INTO showcases (guild_id, order_id, customer_id, designer_id, title, description, channel_id,
                                   message_id, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            interaction.guild.id, order, customer.id if customer else None, designer_member.id, title, description,
            channel.id, message.id, interaction.user.id,
        )
        await interaction.followup.send(view=self.bot.notice(kind="success", title=tr_user(
            "shop.showcase.posted", channel=channel.mention)), ephemeral=True)

    # ================================================================== autocomplete
    async def _product_choices(self, interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[int]]:
        locale = self.bot.locale_for(interaction)
        money = self.money(locale)
        products = await self.shop.products(interaction.guild_id or 0, active_only=False)
        current = current.lower()
        return [
            app_commands.Choice(name=truncate(f"{p.name(locale)} ({p.sku}) · {money(p.price_cents)}", 100), value=p.id)
            for p in products if current in p.name(locale).lower() or current in p.sku.lower()
        ][:25]

    async def _bundle_choices(self, interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[int]]:
        locale = self.bot.locale_for(interaction)
        bundles = await self.shop.bundles(interaction.guild_id or 0, active_only=False)
        return [
            app_commands.Choice(name=truncate(f"{b.name(locale)} ({b.sku})", 100), value=b.id)
            for b in bundles if current.lower() in b.name(locale).lower() or current.lower() in b.sku.lower()
        ][:25]

    async def _category_choices(self, interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[str]]:
        locale = self.bot.locale_for(interaction)
        categories = await self.shop.categories(interaction.guild_id or 0, active_only=False)
        return [
            app_commands.Choice(name=truncate(f"{c.name(locale)} ({c.key})", 100), value=c.key)
            for c in categories if current.lower() in c.key or current.lower() in c.name(locale).lower()
        ][:25]

    async def _coupon_choices(self, interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[int]]:
        coupons = await self.shop.coupons(interaction.guild_id or 0)
        return [app_commands.Choice(name=c.code, value=c.id) for c in coupons if current.upper() in c.code][:25]

    async def _category_id(self, guild_id: int, key: str | None) -> int | None:
        if not key:
            return None
        category = next((c for c in await self.shop.categories(guild_id, active_only=False) if c.key == key), None)
        if category is None:
            raise NotFound("shop.errors.category_not_found")
        return category.id

    @staticmethod
    def _price(value: str) -> int:
        cents = parse_money(value)
        if cents is None:
            raise UserError("errors.invalid_amount")
        return cents

    # ================================================================== products
    @product_group.command(name="add", description=T("product.add"))
    @app_commands.describe(sku=T("product.sku"), name_de=T("product.name_de"), name_en=T("product.name_en"),
                           price=T("product.price"), category=T("product.category_key"),
                           description_de=T("product.description_de"), description_en=T("product.description_en"),
                           emoji=T("product.emoji"), delivery_days=T("product.delivery_days"),
                           image_url=T("product.image_url"))
    @require_admin()
    async def product_add(
        self, interaction: discord.Interaction[Any], sku: app_commands.Range[str, 1, 40],
        name_de: app_commands.Range[str, 1, 80], name_en: app_commands.Range[str, 1, 80], price: str,
        category: str | None = None, description_de: str | None = None, description_en: str | None = None,
        emoji: str | None = None, delivery_days: app_commands.Range[int, 1, 365] | None = None,
        image_url: str | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        if image_url and not is_url(image_url):
            raise UserError("errors.invalid_url")
        product = await self.shop.create_product(
            interaction.guild_id, sku=sku.strip(), name_de=name_de, name_en=name_en, price_cents=self._price(price),
            category_id=await self._category_id(interaction.guild_id, category),
            description_de=description_de or "", description_en=description_en or "", emoji=emoji,
            delivery_days=delivery_days, image_url=image_url,
        )
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("shop.product.added", name=product.name(tr.locale)))
        await self.bot.audit.log(interaction.guild, "orders", "product.add", title=tr("shop.log.product_added"),
                                 emoji="product", actor=interaction.user,
                                 lines=[f"`{product.sku}` · {product.name_de} · {self.money('de')(product.price_cents)}"])

    product_add.autocomplete("category")(_category_choices)

    @product_group.command(name="edit", description=T("product.edit"))
    @app_commands.describe(product=T("product.product"), name_de=T("product.name_de"), name_en=T("product.name_en"),
                           price=T("product.price"), category=T("product.category_key"),
                           description_de=T("product.description_de"), description_en=T("product.description_en"),
                           emoji=T("product.emoji"), delivery_days=T("product.delivery_days"),
                           image_url=T("product.image_url"), active=T("product.active"))
    @require_admin()
    async def product_edit(
        self, interaction: discord.Interaction[Any], product: int, name_de: str | None = None,
        name_en: str | None = None, price: str | None = None, category: str | None = None,
        description_de: str | None = None, description_en: str | None = None, emoji: str | None = None,
        delivery_days: app_commands.Range[int, 1, 365] | None = None, image_url: str | None = None,
        active: bool | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        values: dict[str, Any] = {
            "name_de": name_de, "name_en": name_en, "description_de": description_de,
            "description_en": description_en, "emoji": emoji, "delivery_days": delivery_days,
            "image_url": image_url, "active": active,
        }
        if price is not None:
            values["price_cents"] = self._price(price)
        if category is not None:
            values["category_id"] = await self._category_id(interaction.guild_id, category)
        updated = await self.shop.update_product(interaction.guild_id, product,
                                                 **{k: v for k, v in values.items() if v is not None})
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("shop.product.updated", name=updated.name(tr.locale)))

    product_edit.autocomplete("product")(_product_choices)
    product_edit.autocomplete("category")(_category_choices)

    @product_group.command(name="remove", description=T("product.remove"))
    @app_commands.describe(product=T("product.product"))
    @require_admin()
    async def product_remove(self, interaction: discord.Interaction[Any], product: int) -> None:
        assert interaction.guild_id is not None
        item = await self.shop.product(interaction.guild_id, product)
        await self.shop.delete_product(interaction.guild_id, product)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("shop.product.removed", name=item.name(tr.locale)))
        await self.bot.audit.log(interaction.guild, "orders", "product.remove", title=tr("shop.log.product_removed"),
                                 emoji="delete", actor=interaction.user, lines=[f"`{item.sku}` · {item.name_de}"])

    product_remove.autocomplete("product")(_product_choices)

    @product_group.command(name="list", description=T("product.list"))
    @require_staff()
    async def product_list(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        money = self.money(tr.locale)
        products = await self.shop.products(interaction.guild_id, active_only=False)
        b = CV2(self.bot.theme)
        b.heading(tr("shop.product.list_title"), emoji="product", subline=tr("shop.product.count", count=len(products)))
        if products:
            b.text("\n".join(
                f"{self.bot.icons['toggle_on' if p.active else 'toggle_off']} `{p.sku}` **{p.name(tr.locale)}** · "
                f"{money(p.price_cents)}" for p in products[:60]
            ))
        else:
            b.quote(tr("shop.panel.empty"))
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    @category_group.command(name="add", description=T("product.category.add"))
    @app_commands.describe(key=T("product.category.key"), name_de=T("product.name_de"), name_en=T("product.name_en"),
                           emoji=T("product.emoji"), sort_order=T("product.category.sort"))
    @require_admin()
    async def category_add(self, interaction: discord.Interaction[Any], key: app_commands.Range[str, 1, 24],
                           name_de: str, name_en: str, emoji: str | None = None, sort_order: int = 0) -> None:
        assert interaction.guild_id is not None
        category = await self.shop.upsert_category(interaction.guild_id, key.lower().strip(), name_de, name_en, emoji,
                                                   sort_order)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("shop.category.saved", name=category.name(tr.locale)))

    @category_group.command(name="remove", description=T("product.category.remove"))
    @app_commands.describe(key=T("product.category.key"))
    @require_admin()
    async def category_remove(self, interaction: discord.Interaction[Any], key: str) -> None:
        assert interaction.guild_id is not None
        if not await self.shop.delete_category(interaction.guild_id, key):
            raise NotFound("shop.errors.category_not_found")
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("shop.category.removed", key=key))

    category_remove.autocomplete("key")(_category_choices)

    # ================================================================== bundles
    @bundle_group.command(name="create", description=T("bundle.create"))
    @app_commands.describe(sku=T("product.sku"), name_de=T("product.name_de"), name_en=T("product.name_en"),
                           price=T("product.price"), description_de=T("product.description_de"),
                           description_en=T("product.description_en"), emoji=T("product.emoji"))
    @require_admin()
    async def bundle_create(self, interaction: discord.Interaction[Any], sku: app_commands.Range[str, 1, 40],
                            name_de: str, name_en: str, price: str, description_de: str | None = None,
                            description_en: str | None = None, emoji: str | None = None) -> None:
        assert interaction.guild_id is not None
        bundle = await self.shop.create_bundle(
            interaction.guild_id, sku=sku.strip(), name_de=name_de, name_en=name_en, price_cents=self._price(price),
            description_de=description_de or "", description_en=description_en or "", emoji=emoji,
        )
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("shop.bundle.created", name=bundle.name(tr.locale)),
                                      description=tr("shop.bundle.hint"))

    @bundle_group.command(name="item", description=T("bundle.item"))
    @app_commands.describe(bundle=T("bundle.bundle"), product=T("product.product"), quantity=T("bundle.quantity"))
    @require_admin()
    async def bundle_item(self, interaction: discord.Interaction[Any], bundle: int, product: int,
                          quantity: app_commands.Range[int, 0, 100] = 1) -> None:
        assert interaction.guild_id is not None
        await self.shop.set_bundle_item(interaction.guild_id, bundle, product, quantity)
        updated = await self.shop.bundle(interaction.guild_id, bundle)
        tr = self.tr(interaction)
        content = ", ".join(f"{i.quantity}× {i.name_de if tr.locale == 'de' else i.name_en}" for i in updated.items)
        await self.bot.respond_notice(interaction, title=tr("shop.bundle.updated", name=updated.name(tr.locale)),
                                      description=content or tr("shop.bundle.empty"))

    bundle_item.autocomplete("bundle")(_bundle_choices)
    bundle_item.autocomplete("product")(_product_choices)

    @bundle_group.command(name="edit", description=T("bundle.edit"))
    @app_commands.describe(bundle=T("bundle.bundle"), price=T("product.price"), active=T("product.active"),
                           name_de=T("product.name_de"), name_en=T("product.name_en"))
    @require_admin()
    async def bundle_edit(self, interaction: discord.Interaction[Any], bundle: int, price: str | None = None,
                          active: bool | None = None, name_de: str | None = None, name_en: str | None = None) -> None:
        assert interaction.guild_id is not None
        values: dict[str, Any] = {"active": active, "name_de": name_de, "name_en": name_en}
        if price is not None:
            values["price_cents"] = self._price(price)
        updated = await self.shop.update_bundle(interaction.guild_id, bundle,
                                                **{k: v for k, v in values.items() if v is not None})
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("shop.bundle.updated", name=updated.name(tr.locale)))

    bundle_edit.autocomplete("bundle")(_bundle_choices)

    @bundle_group.command(name="remove", description=T("bundle.remove"))
    @app_commands.describe(bundle=T("bundle.bundle"))
    @require_admin()
    async def bundle_remove(self, interaction: discord.Interaction[Any], bundle: int) -> None:
        assert interaction.guild_id is not None
        await self.shop.delete_bundle(interaction.guild_id, bundle)
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("shop.bundle.removed"))

    bundle_remove.autocomplete("bundle")(_bundle_choices)

    @bundle_group.command(name="list", description=T("bundle.list"))
    @require_staff()
    async def bundle_list(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        money = self.money(tr.locale)
        bundles = await self.shop.bundles(interaction.guild_id, active_only=False)
        b = CV2(self.bot.theme)
        b.heading(tr("shop.panel.bundles"), emoji="bundle")
        if bundles:
            b.bullets([
                Bullet(f"{bd.name(tr.locale)} (`{bd.sku}`)", money(bd.price_cents), emoji="bundle" if bd.active else "toggle_off",
                       sub=[", ".join(f"{i.quantity}× {i.name_de if tr.locale == 'de' else i.name_en}" for i in bd.items)
                            or tr("shop.bundle.empty")])
                for bd in bundles[:20]
            ])
        else:
            b.quote(tr("shop.bundle.none"))
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    # ================================================================== coupons
    def _render_coupons(self, tr: Translator, coupons: list[Any]) -> discord.ui.LayoutView:
        money = self.money(tr.locale)
        b = CV2(self.bot.theme)
        b.heading(tr("shop.coupon.list_title"), emoji="coupon", subline=tr("shop.coupon.count", count=len(coupons)))
        if not coupons:
            b.quote(tr("shop.coupon.none"))
            return b.build()
        lines = []
        for c in coupons[:30]:
            value = f"{c.value} %" if c.kind == "percent" else money(c.value)
            uses = f"{c.uses}/{c.max_uses}" if c.max_uses is not None else f"{c.uses}/∞"
            state = "toggle_on" if c.active and not c.expired and not c.exhausted else "toggle_off"
            expiry = f" · {tr('shop.coupon.expires', date=discord.utils.format_dt(c.expires_at, 'd'))}" if c.expires_at else ""
            bound = f" · {tr('shop.coupon.bound', count=len(c.product_ids))}" if c.product_ids else ""
            lines.append(f"{self.bot.icons[state]} `{c.code}` **{value}** · {uses}{expiry}{bound}")
        b.text("\n".join(lines))
        return b.build()

    @coupon_group.command(name="create", description=T("coupon.create"))
    @app_commands.describe(code=T("coupon.code"), kind=T("coupon.kind"), value=T("coupon.value"),
                           expires=T("coupon.expires"), max_uses=T("coupon.max_uses"),
                           min_order=T("coupon.min_order"), products=T("coupon.products"))
    @app_commands.choices(kind=[app_commands.Choice(name=T("coupon.kind.percent"), value="percent"),
                                app_commands.Choice(name=T("coupon.kind.fixed"), value="fixed")])
    @require_admin()
    async def coupon_create(
        self, interaction: discord.Interaction[Any], code: app_commands.Range[str, 3, 40], kind: str, value: str,
        expires: str | None = None, max_uses: app_commands.Range[int, 1, 100000] | None = None,
        min_order: str | None = None, products: str | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        if kind == "percent":
            try:
                amount = int(value.strip().rstrip("%"))
            except ValueError as exc:
                raise UserError("errors.invalid_amount") from exc
        else:
            amount = self._price(value)
        expires_at: dt.datetime | None = None
        if expires:
            delta = parse_duration(expires)
            day = parse_date(expires) if delta is None else None
            if delta is not None:
                expires_at = utcnow() + delta
            elif day is not None:
                expires_at = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=dt.UTC)
            else:
                raise UserError("errors.invalid_date")
        product_ids: list[int] = []
        for sku in [s.strip() for s in (products or "").split(",") if s.strip()]:
            product_ids.append((await self.shop.product_by_sku(interaction.guild_id, sku)).id)
        coupon = await self.shop.create_coupon(
            interaction.guild_id, code=code, kind=kind, value=amount,
            min_order_cents=self._price(min_order) if min_order else 0, max_uses=max_uses, expires_at=expires_at,
            product_ids=product_ids, created_by=interaction.user.id,
        )
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("shop.coupon.created", code=coupon.code))
        await self.bot.audit.log(interaction.guild, "orders", "coupon.create", title=tr("shop.log.coupon_created"),
                                 emoji="coupon", actor=interaction.user, lines=[f"`{coupon.code}` · {kind} · {value}"])

    @coupon_group.command(name="toggle", description=T("coupon.toggle"))
    @app_commands.describe(coupon=T("coupon.coupon"))
    @require_admin()
    async def coupon_toggle(self, interaction: discord.Interaction[Any], coupon: int) -> None:
        assert interaction.guild_id is not None
        item = await self.shop.coupon(interaction.guild_id, coupon)
        await self.shop.set_coupon_active(interaction.guild_id, coupon, not item.active)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("shop.coupon.toggled", code=item.code,
                                                            state=tr("common.disabled" if item.active else "common.enabled")))

    coupon_toggle.autocomplete("coupon")(_coupon_choices)

    @coupon_group.command(name="delete", description=T("coupon.delete"))
    @app_commands.describe(coupon=T("coupon.coupon"))
    @require_admin()
    async def coupon_delete(self, interaction: discord.Interaction[Any], coupon: int) -> None:
        assert interaction.guild_id is not None
        item = await self.shop.coupon(interaction.guild_id, coupon)
        await self.shop.delete_coupon(interaction.guild_id, coupon)
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("shop.coupon.deleted", code=item.code))

    coupon_delete.autocomplete("coupon")(_coupon_choices)

    @coupon_group.command(name="list", description=T("coupon.list"))
    @require_staff()
    async def coupon_list(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        await interaction.response.send_message(
            view=self._render_coupons(self.tr(interaction), await self.shop.coupons(interaction.guild_id)),
            ephemeral=True,
        )

