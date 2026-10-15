"""Statistic layouts (shared by ``/stats`` and the control panel)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from discord import ui

from ...core.i18n import Translator
from ...core.utils import format_number, progress_bar, truncate
from ...services.orders import OrderStats
from ...services.stats import CategoryRow, DesignerRow, Overview, ReviewStats
from ...ui.builder import CV2, Bullet, Theme
from ...ui.layouts import stars

Money = Callable[[int], str]


def _hours(tr: Translator, value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 48:
        return tr("stats.days", value=f"{value / 24:.1f}")
    return tr("stats.hours", value=f"{value:.1f}")


def render_overview(theme: Theme, tr: Translator, data: Overview, money: Money, period: str) -> ui.LayoutView:
    b = CV2(theme)
    b.banner("stats")
    b.heading(tr("stats.overview.title"), emoji="stats", subline=period)
    b.text(f"**{tr('stats.section.tickets')}**")
    b.bullets([
        Bullet(tr("stats.tickets.created"), str(data.tickets_created), emoji="ticket"),
        Bullet(tr("stats.tickets.open"), str(data.tickets_open), emoji="unlock"),
        Bullet(tr("stats.tickets.closed"), str(data.tickets_closed), emoji="lock"),
        Bullet(tr("stats.tickets.avg_close"), _hours(tr, data.avg_close_hours), emoji="clock"),
    ])
    b.block_break()
    b.text(f"**{tr('stats.section.sales')}**")
    b.bullets([
        Bullet(tr("stats.sales.revenue"), f"**{money(data.revenue_cents)}**", emoji="money"),
        Bullet(tr("stats.sales.orders"), str(data.orders_paid), emoji="order"),
        Bullet(tr("stats.customers.paying"), str(data.customers), emoji="users"),
        Bullet(tr("stats.customers.repeat"), str(data.repeat_customers), emoji="refresh"),
    ])
    b.block_break()
    b.text(f"**{tr('stats.section.reviews')}**")
    rating = f"{stars(theme, round(data.avg_rating))} ({data.avg_rating:.2f})" if data.avg_rating else "—"
    items = [Bullet(tr("stats.reviews.count"), str(data.reviews), emoji="review"),
             Bullet(tr("stats.reviews.average"), rating, emoji="star")]
    if data.download_clicks is not None:
        items.append(Bullet(tr("stats.downloads.clicks"), format_number(data.download_clicks, tr.locale),
                            emoji="download"))
    b.bullets(items)
    return b.build()


def render_designers(theme: Theme, tr: Translator, rows: Sequence[DesignerRow], money: Money, period: str) -> ui.LayoutView:
    b = CV2(theme)
    b.banner("stats")
    b.heading(tr("stats.designers.title"), emoji="designer", subline=period)
    if not rows:
        b.quote(tr("stats.empty"))
        return b.build()
    b.bullets([
        Bullet(f"<@{r.designer_id}>", emoji="designer", separator="", sub=[
            tr("stats.designers.line", completed=r.completed, active=r.active, revenue=money(r.revenue_cents)),
            tr("stats.designers.line2",
               rating=f"{r.avg_rating:.2f} {theme.emojis['star']}" if r.avg_rating else "—",
               lead=_hours(tr, r.avg_lead_hours)),
        ])
        for r in rows[:15]
    ])
    return b.build()


def render_tickets(
    theme: Theme, tr: Translator, rows: Sequence[CategoryRow], supporters: Sequence[tuple[int, int]], period: str
) -> ui.LayoutView:
    b = CV2(theme)
    b.banner("stats")
    b.heading(tr("stats.tickets.title"), emoji="ticket", subline=period)
    if rows:
        b.bullets([
            Bullet(r.name_de if tr.locale == "de" else r.name_en, emoji="category", sub=[
                tr("stats.tickets.line", created=r.created, closed=r.closed, avg=_hours(tr, r.avg_close_hours))
            ])
            for r in rows[:15]
        ])
    else:
        b.quote(tr("stats.empty"))
    if supporters:
        b.block_break()
        b.text(f"**{tr('stats.tickets.supporters')}**")
        b.text("\n".join(f"`#{i}` <@{uid}> · {tr('stats.tickets.messages', count=count)}"
                         for i, (uid, count) in enumerate(supporters, start=1)))
    return b.build()


def render_reviews(theme: Theme, tr: Translator, data: ReviewStats, period: str) -> ui.LayoutView:
    b = CV2(theme)
    b.banner("review")
    b.heading(tr("stats.reviews.title"), emoji="review", subline=period)
    if not data.count:
        b.quote(tr("stats.empty"))
        return b.build()
    b.text(tr("stats.reviews.summary", average=f"{data.average:.2f}", count=data.count,
              stars=stars(theme, round(data.average or 0))))
    b.block_break()
    top = max(data.distribution.values()) or 1
    b.text("\n".join(
        f"`{n}` {theme.emojis['star']} {progress_bar(data.distribution[n], top, 10, theme.emojis['bar_full'], theme.emojis['bar_empty'])} "
        f"**{data.distribution[n]}**"
        for n in range(5, 0, -1)
    ))
    if data.latest:
        b.block_break()
        b.text(f"**{tr('stats.reviews.latest')}**")
        b.bullets([
            Bullet(f"<@{r['user_id']}> · {stars(theme, r['rating'])}", emoji="message", separator="",
                   sub=[truncate(r["feedback"], 180)])
            for r in data.latest
        ])
    return b.build()


def render_sales(
    theme: Theme, tr: Translator, stats: OrderStats, products: Sequence[dict[str, Any]], money: Money, period: str
) -> ui.LayoutView:
    b = CV2(theme)
    b.banner("stats")
    b.heading(tr("stats.sales.title"), emoji="money", subline=period)
    b.bullets([
        Bullet(tr("stats.sales.revenue"), f"**{money(stats.revenue_cents)}**", emoji="money"),
        Bullet(tr("stats.sales.orders"), str(stats.count), emoji="order"),
        Bullet(tr("stats.sales.average"), money(stats.average_cents), emoji="stats"),
        Bullet(tr("stats.sales.express"), f"{stats.express_share * 100:.0f} %", emoji="express"),
    ])
    if products:
        b.block_break()
        b.text(f"**{tr('stats.sales.top_products')}**")
        b.text("\n".join(f"`#{i}` **{p['name']}** · {p['qty']}× · {money(int(p['revenue']))}"
                         for i, p in enumerate(products, start=1)))
    return b.build()


def render_customers(theme: Theme, tr: Translator, data: dict[str, Any], money: Money, period: str) -> ui.LayoutView:
    b = CV2(theme)
    b.banner("stats")
    b.heading(tr("stats.customers.title"), emoji="users", subline=period)
    b.bullets([Bullet(tr("stats.customers.new"), str(data["new"]), emoji="add")])
    if data["top"]:
        b.block_break()
        b.text(f"**{tr('stats.customers.top')}**")
        b.text("\n".join(f"`#{i}` <@{c['customer_id']}> · {c['orders']}× · **{money(int(c['ltv']))}**"
                         for i, c in enumerate(data["top"], start=1)))
    return b.build()
