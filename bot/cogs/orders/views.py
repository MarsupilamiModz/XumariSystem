"""Layouts of the order module (wizard steps, cart, status messages, queue, todos, statistics)."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import discord
from discord import ui

from ...config.settings import AppConfig
from ...core.i18n import Translator
from ...core.router import cid
from ...core.utils import progress_bar, truncate, ts
from ...services.orders import Order, OrderStats, Todo
from ...ui.builder import CV2, Bullet, Theme, button, select_option, string_select

Money = Callable[[int], str]

STATUS_EMOJI_KEYS: dict[str, str] = {
    "draft": "cart",
    "awaiting_payment": "payment",
    "paid": "money",
    "queued": "queue",
    "in_progress": "designer",
    "pending_answer": "clock",
    "completed": "success",
    "cancelled": "cancel",
    "refunded": "money",
}


def item_lines(order: Order, money: Money) -> list[str]:
    return [f"`{i.quantity}×` **{i.name}** · {money(i.total_cents)}" for i in order.items]


def totals_bullets(tr: Translator, order: Order, money: Money, config: AppConfig) -> list[Bullet]:
    bullets = [Bullet(tr("orders.totals.subtotal"), money(order.subtotal_cents), emoji="cart")]
    if order.express_cents:
        bullets.append(Bullet(tr("orders.totals.express"), money(order.express_cents), emoji="express"))
    if order.seasonal_key:
        design = next((s for s in config.shop.seasonal_designs if s.key == order.seasonal_key), None)
        if design is not None:
            bullets.append(Bullet(tr("orders.totals.seasonal", name=design.name(order.locale)),
                                  money(order.seasonal_cents), emoji=design.emoji))
    if order.discount_cents:
        bullets.append(Bullet(tr("orders.totals.discount", code=order.coupon_code or ""),
                              f"-{money(order.discount_cents)}", emoji="coupon"))
    bullets.append(Bullet(tr("orders.totals.total"), f"**{money(order.total_cents)}**", emoji="money"))
    return bullets


def render_details(theme: Theme, tr: Translator, order: Order, money: Money, config: AppConfig) -> ui.LayoutView:
    shop = config.shop
    b = CV2(theme)
    b.banner("order")
    b.heading(tr("orders.details.title"), emoji="settings", subline=tr("orders.number", number=order.number))
    b.quote(tr("orders.details.subtitle"))
    b.bullets(totals_bullets(tr, order, money, config))
    b.separator()
    delivery_options = [
        select_option(theme, tr("orders.delivery.standard", days=shop.standard_delivery_days), "standard",
                      description=tr("orders.delivery.standard_desc"), emoji="standard",
                      default=order.delivery == "standard"),
    ]
    if shop.express.enabled:
        surcharge = []
        if shop.express.surcharge_percent:
            surcharge.append(f"+{shop.express.surcharge_percent:g} %")
        if shop.express.surcharge_fixed_cents:
            surcharge.append(f"+{money(shop.express.surcharge_fixed_cents)}")
        delivery_options.append(
            select_option(theme, tr("orders.delivery.express", days=shop.express.delivery_days), "express",
                          description=" ".join(surcharge) or tr("orders.delivery.express_desc"), emoji="express",
                          default=order.delivery == "express")
        )
    b.actions(string_select(cid("order.delivery", order.id), delivery_options,
                            placeholder=tr("orders.delivery.placeholder")))
    if shop.seasonal_designs:
        season_options = [
            select_option(theme, tr("orders.seasonal.none"), "none", emoji="cancel", default=order.seasonal_key is None)
        ]
        season_options.extend(
            select_option(theme, s.name(order.locale), s.key, emoji=s.emoji,
                          description=f"{s.name('en' if order.locale == 'de' else 'de')} · +{money(s.price_cents)}",
                          default=order.seasonal_key == s.key)
            for s in shop.seasonal_designs[:24]
        )
        b.actions(string_select(cid("order.season", order.id), season_options,
                                placeholder=tr("orders.seasonal.placeholder")))
    coupon_button = (
        button(theme, tr("orders.coupon.remove"), emoji="coupon", custom_id=cid("order.uncoupon", order.id))
        if order.coupon_id else
        button(theme, tr("orders.coupon.add"), emoji="coupon", custom_id=cid("order.coupon", order.id))
    )
    b.actions(
        button(theme, tr("common.back"), emoji="back", custom_id=cid("order.view", order.id, "products")),
        coupon_button,
        button(theme, tr("orders.summary.button"), emoji="next", custom_id=cid("order.view", order.id, "summary"),
               style=discord.ButtonStyle.success),
    )
    return b.build()


def render_summary(theme: Theme, tr: Translator, order: Order, money: Money, config: AppConfig) -> ui.LayoutView:
    b = CV2(theme)
    b.banner("order")
    b.heading(tr("orders.summary.title"), emoji="invoice", subline=tr("orders.number", number=order.number))
    b.quote(tr("orders.summary.subtitle"))
    b.text("\n".join(item_lines(order, money)) or tr("orders.cart.empty"))
    b.block_break()
    delivery = tr(f"orders.delivery.{order.delivery}_short")
    b.bullets([Bullet(tr("orders.summary.delivery"), delivery, emoji="express" if order.delivery == "express" else "standard"),
               *totals_bullets(tr, order, money, config)])
    b.footer(tr("orders.summary.footer"))
    b.separator()
    b.actions(
        button(theme, tr("common.back"), emoji="back", custom_id=cid("order.view", order.id, "details")),
        button(theme, tr("orders.summary.cancel"), emoji="cancel", custom_id=cid("order.cancel", order.id),
               style=discord.ButtonStyle.danger),
        button(theme, tr("orders.summary.submit"), emoji="payment", custom_id=cid("order.submit", order.id),
               style=discord.ButtonStyle.success, disabled=not order.items),
    )
    return b.build()


def render_submitted(
    theme: Theme, tr: Translator, order: Order, money: Money, config: AppConfig, payment_url: str | None
) -> ui.LayoutView:
    b = CV2(theme)
    b.banner("payment")
    b.heading(tr("orders.submitted.title"), emoji="payment", subline=tr("orders.number", number=order.number))
    b.quote(tr("orders.submitted.subtitle_link") if payment_url else tr("orders.submitted.subtitle_manual"))
    b.text("\n".join(item_lines(order, money)))
    b.block_break()
    b.bullets(totals_bullets(tr, order, money, config))
    b.footer(tr("orders.submitted.footer"))
    if payment_url:
        b.separator()
        b.actions(
            button(theme, tr("orders.submitted.pay", amount=money(order.total_cents)), emoji="payment", url=payment_url),
            button(theme, tr("orders.submitted.new_link"), emoji="refresh", custom_id=cid("order.paylink", order.id)),
        )
    return b.build()


def render_cart(theme: Theme, tr: Translator, order: Order, money: Money) -> ui.LayoutView:
    b = CV2(theme)
    b.heading(tr("orders.cart.title"), emoji="cart", level=3, subline=tr("orders.number", number=order.number))
    if order.items:
        b.text("\n".join(item_lines(order, money)))
        b.footer(tr("orders.cart.subtotal", amount=money(order.subtotal_cents)))
        b.separator()
        options = [
            select_option(theme, truncate(i.name, 90), str(i.id), description=f"{i.quantity}× · {money(i.total_cents)}",
                          emoji="remove")
            for i in order.items[:25]
        ]
        b.actions(string_select(cid("order.rm", order.id), options, placeholder=tr("orders.cart.remove_placeholder")))
        b.actions(button(theme, tr("orders.cart.clear"), emoji="delete", custom_id=cid("order.clear", order.id),
                         style=discord.ButtonStyle.danger))
    else:
        b.quote(tr("orders.cart.empty"))
    return b.build()


def render_status(
    theme: Theme,
    tr: Translator,
    order: Order,
    money: Money,
    *,
    position: int | None,
    mention: str | None = None,
    note: str | None = None,
    guild_name: str | None = None,
) -> ui.LayoutView:
    """Status update (ticket channel + customer DM)."""
    b = CV2(theme)
    if mention:
        b.mention(mention)
    b.banner("order")
    b.heading(tr(f"orders.status_title.{order.status}"), emoji=STATUS_EMOJI_KEYS.get(order.status, "info"),
              subline=tr("orders.number", number=order.number) + (f" · {guild_name}" if guild_name else ""))
    b.quote(tr(f"orders.status_text.{order.status}"))
    items = [Bullet(tr("orders.info.status"), tr(f"orders.status.{order.status}"), emoji="info")]
    if position is not None:
        items.append(Bullet(tr("orders.info.position"), f"#{position}", emoji="queue"))
    if order.designer_id:
        items.append(Bullet(tr("orders.info.designer"), f"<@{order.designer_id}>", emoji="designer"))
    if order.due_at and order.status not in ("completed", "cancelled", "refunded"):
        items.append(Bullet(tr("orders.info.due"), ts(order.due_at, "D"), emoji="calendar"))
    items.append(Bullet(tr("orders.totals.total"), money(order.total_cents), emoji="money"))
    b.bullets(items)
    if note:
        b.block_break()
        b.quote(note)
    b.footer(tr("orders.status_footer"))
    return b.build()


def render_queue(
    theme: Theme, tr: Translator, queue: Sequence[Order], working: Sequence[Order], money: Money, *, limit: int = 15
) -> ui.LayoutView:
    b = CV2(theme)
    b.banner("queue")
    b.heading(tr("orders.queue.title"), emoji="queue",
              subline=tr("orders.queue.subline", waiting=len(queue), working=len(working)))
    if queue:
        lines = []
        for index, order in enumerate(queue[:limit], start=1):
            express = f" {theme.emojis['express']}" if order.delivery == "express" else ""
            designer = f" · {theme.emojis['designer']} <@{order.designer_id}>" if order.designer_id else ""
            since = f" · {ts(order.paid_at, 'R')}" if order.paid_at else ""
            lines.append(f"`#{index:>2}` **{tr('orders.number', number=order.number)}**{express} · <@{order.customer_id}>"
                         f"{designer}{since}")
        b.text("\n".join(lines))
        if len(queue) > limit:
            b.small(tr("orders.queue.more", count=len(queue) - limit))
    else:
        b.quote(tr("orders.queue.empty"))
    if working:
        b.block_break()
        b.text(f"**{tr('orders.queue.working')}**")
        b.bullets(
            [
                f"**{tr('orders.number', number=o.number)}** · <@{o.customer_id}> · "
                f"{tr(f'orders.status.{o.status}')}" + (f" · <@{o.designer_id}>" if o.designer_id else "")
                for o in working[:10]
            ],
            default_emoji="designer",
        )
    b.footer(tr("orders.queue.footer"))
    return b.build()


def render_todos(theme: Theme, tr: Translator, order: Order, todos: Sequence[Todo]) -> ui.LayoutView:
    done = sum(1 for t in todos if t.done)
    b = CV2(theme)
    b.heading(tr("orders.todos.title"), emoji="todo", subline=tr("orders.number", number=order.number))
    b.quote(tr("orders.todos.subtitle"))
    if todos:
        b.text("\n".join(
            f"{theme.emojis['todo_done' if t.done else 'todo_open']} {'~~' + t.text + '~~' if t.done else t.text}"
            for t in todos
        ))
        b.footer(tr("orders.todos.progress", done=done, total=len(todos)) + "  " +
                 progress_bar(done, len(todos), 10, theme.emojis["bar_full"], theme.emojis["bar_empty"]))
        b.separator()
        b.actions(string_select(
            cid("todo.toggle", order.id),
            [select_option(theme, truncate(t.text, 90), str(t.id), emoji="todo_done" if t.done else "todo_open")
             for t in todos[:25]],
            placeholder=tr("orders.todos.toggle_placeholder"), max_values=min(25, len(todos)),
        ))
    else:
        b.text(tr("orders.todos.empty"))
        b.separator()
    b.actions(
        button(theme, tr("orders.todos.add"), emoji="add", custom_id=cid("todo.add", order.id)),
        button(theme, tr("orders.todos.remove"), emoji="remove", custom_id=cid("todo.rmmenu", order.id),
               disabled=not todos),
    )
    return b.build()


def render_order_info(
    theme: Theme, tr: Translator, order: Order, money: Money, config: AppConfig, *, position: int | None
) -> ui.LayoutView:
    b = CV2(theme)
    b.heading(tr("orders.number", number=order.number), emoji="order",
              subline=tr(f"orders.status.{order.status}"))
    info = [
        Bullet(tr("orders.info.customer"), f"<@{order.customer_id}>", emoji="user"),
        Bullet(tr("orders.info.created"), ts(order.created_at, "f"), emoji="calendar"),
    ]
    if order.paid_at:
        info.append(Bullet(tr("orders.info.paid"), ts(order.paid_at, "f"), emoji="money"))
    if order.designer_id:
        info.append(Bullet(tr("orders.info.designer"), f"<@{order.designer_id}>", emoji="designer"))
    if position:
        info.append(Bullet(tr("orders.info.position"), f"#{position}", emoji="queue"))
    if order.due_at:
        info.append(Bullet(tr("orders.info.due"), ts(order.due_at, "D"), emoji="clock"))
    b.bullets(info)
    b.separator()
    b.text("\n".join(item_lines(order, money)) or tr("orders.cart.empty"))
    b.block_break()
    b.bullets(totals_bullets(tr, order, money, config))
    if order.notes:
        b.block_break()
        b.quote(order.notes)
    return b.build()


def render_stats(
    theme: Theme, tr: Translator, stats: OrderStats, money: Money, period_label: str
) -> ui.LayoutView:
    b = CV2(theme)
    b.banner("stats")
    b.heading(tr("orders.stats.title"), emoji="stats", subline=period_label)
    b.bullets([
        Bullet(tr("orders.stats.revenue"), f"**{money(stats.revenue_cents)}**", emoji="money"),
        Bullet(tr("orders.stats.count"), str(stats.count), emoji="order"),
        Bullet(tr("orders.stats.average"), money(stats.average_cents), emoji="stats"),
        Bullet(tr("orders.stats.express"), f"{stats.express_share * 100:.0f} %", emoji="express"),
    ])
    if stats.by_status:
        b.block_break()
        b.text(f"**{tr('orders.stats.by_status')}**")
        b.text(" · ".join(f"{tr(f'orders.status.{k}')}: **{v}**" for k, v in sorted(stats.by_status.items())))
    if stats.designers:
        b.block_break()
        b.text(f"**{tr('orders.stats.designers')}**")
        b.bullets(
            [f"<@{d.designer_id}> · {d.orders}× · {money(d.revenue_cents)}" for d in stats.designers],
            default_emoji="designer",
        )
    return b.build()


def guide_view(theme: Theme, tr: Translator) -> ui.LayoutView:
    b = CV2(theme)
    b.heading(tr("orders.guide.title"), emoji="guide", level=3)
    b.bullets([Bullet(tr(f"orders.guide.step{n}_title"), tr(f"orders.guide.step{n}"), emoji="dot") for n in range(1, 5)])
    b.footer(tr("orders.guide.footer"))
    return b.build()

