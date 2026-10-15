"""Reusable layouts. Every message the bot sends is created by one of these builders so the whole
system looks consistent. Module specific layouts (tickets, reviews, downloads, …) live here too."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import discord
from discord import ui

from ..core.i18n import Translator
from ..core.router import cid
from .builder import CV2, Bullet, Theme, button, select_option, string_select

NoticeKind = Literal["success", "error", "warning", "info"]


def language_button(theme: Theme, tr: Translator, renderer: str, *args: object) -> ui.Button[Any]:
    """Accessory "English Version" / "Deutsche Version" button – re-renders the message ephemerally
    in the other language (see :mod:`bot.ui.translatable`)."""
    target = tr.other().locale
    return button(
        theme,
        tr("common.language_switch"),
        emoji="language",
        custom_id=cid("lang", target, renderer, *args),
    )


def build_panel(
    theme: Theme,
    *,
    title: str,
    emoji: str | None = None,
    banner_key: str | None = None,
    subtitle: str | None = None,
    subline: str | None = None,
    body: str | Sequence[str] | None = None,
    bullets: Iterable[Bullet | str] | None = None,
    footer: str | None = None,
    accessory: ui.Item[Any] | None = None,
    rows: Sequence[Sequence[ui.Item[Any]]] | None = None,
    mention: str | None = None,
    media: Sequence[str] | None = None,
    view: ui.LayoutView | None = None,
    accent: int | None = None,
) -> ui.LayoutView:
    """Generic panel following the 8-step design structure."""
    b = CV2(theme, view=view, accent=accent)
    if mention:
        b.mention(mention)
    b.banner(banner_key)
    b.heading(title, emoji=emoji, subline=subline, accessory=accessory)
    b.quote(subtitle)
    if isinstance(body, str):
        b.text(body)
    elif body:
        for paragraph in body:
            b.text(paragraph)
    if bullets:
        b.bullets(bullets)
    if media:
        b.separator()
        b.media(*media)
    b.footer(footer)
    if rows:
        b.separator()
        b.action_rows(rows)
    return b.build()


def build_notice(
    theme: Theme,
    *,
    kind: NoticeKind = "info",
    title: str,
    description: str | None = None,
    bullets: Iterable[Bullet | str] | None = None,
    footer: str | None = None,
    rows: Sequence[Sequence[ui.Item[Any]]] | None = None,
    banner_key: str | None = None,
    view: ui.LayoutView | None = None,
) -> ui.LayoutView:
    """Short status message (mostly ephemeral): success / error / warning / info."""
    b = CV2(theme, view=view)
    b.banner(banner_key)
    b.heading(title, emoji=kind, level=3)
    b.quote(description)
    if bullets:
        b.bullets(bullets)
    b.footer(footer)
    if rows:
        b.separator()
        b.action_rows(rows)
    return b.build()


def build_log_entry(
    theme: Theme,
    *,
    title: str,
    emoji: str = "info",
    lines: Sequence[str] = (),
    footer: str | None = None,
) -> ui.LayoutView:
    """Uniform log message for all log channels."""
    b = CV2(theme)
    b.heading(title, emoji=emoji, level=3)
    if lines:
        b.text("\n".join(lines))
    b.footer(footer)
    return b.build()


def build_confirm(
    theme: Theme,
    tr: Translator,
    *,
    title: str,
    description: str,
    confirm_id: str,
    cancel_id: str,
    danger: bool = False,
) -> ui.LayoutView:
    confirm = button(
        theme,
        tr("common.confirm"),
        emoji="confirm",
        custom_id=confirm_id,
        style=discord.ButtonStyle.danger if danger else discord.ButtonStyle.success,
    )
    cancel = button(theme, tr("common.cancel"), emoji="cancel", custom_id=cancel_id)
    return build_notice(theme, kind="warning", title=title, description=description, rows=[[confirm, cancel]])


# ====================================================================================== tickets
@dataclass(slots=True)
class CategoryEntry:
    key: str
    emoji: str
    name: str
    other_name: str
    description: str


def build_ticket_panel(
    theme: Theme,
    tr: Translator,
    *,
    categories: Sequence[CategoryEntry],
    select_id: str,
    hours_text: str | None,
    show_ai_hint: bool,
) -> ui.LayoutView:
    """Reference layout "Ticket-Panel"."""
    options = [
        select_option(theme, c.name, c.key, description=c.other_name, emoji=c.emoji) for c in categories
    ]
    select = string_select(select_id, options, placeholder=tr("tickets.panel.select_placeholder"))
    footer_lines = []
    if hours_text:
        footer_lines.append(tr("tickets.panel.hours", hours=hours_text))
    if show_ai_hint:
        footer_lines.append(tr("tickets.panel.ai_hint"))
    return build_panel(
        theme,
        banner_key="tickets",
        title=tr("tickets.panel.title"),
        emoji="ticket",
        accessory=language_button(theme, tr, "ticket.panel"),
        subtitle=tr("tickets.panel.subtitle"),
        bullets=[Bullet(c.name, emoji=c.emoji, sub=[c.description] if c.description else ()) for c in categories],
        footer="\n".join(footer_lines) or None,
        rows=[[select]],
    )


def build_ticket_welcome(
    theme: Theme,
    tr: Translator,
    *,
    mention: str,
    category_name: str,
    category_emoji: str,
    code: str,
    hours_text: str | None,
    answers: Sequence[tuple[str, str]],
    outside_hours_until: str | None,
    close_id: str,
    delete_id: str,
) -> ui.LayoutView:
    """Reference layout "Ticket-Begrüßung" (pinned in the ticket channel)."""
    b = CV2(theme)
    b.mention(mention)
    b.banner("ticket_welcome")
    b.heading(tr("tickets.welcome.title", category=category_name), emoji=category_emoji)
    b.text(tr("tickets.welcome.body"))
    if answers:
        b.block_break()
        b.bullets([Bullet(q, emoji="question", sub=[a or tr("common.none")]) for q, a in answers])
    if outside_hours_until:
        b.block_break()
        b.quote(tr("tickets.welcome.outside_hours", until=outside_hours_until))
    footer = []
    if hours_text:
        footer.append(tr("tickets.welcome.footer_hours", hours=hours_text))
    footer.append(tr("tickets.welcome.footer_meta", code=code, category=category_name))
    b.footer(" ".join(footer))
    b.separator()
    b.actions(
        button(theme, tr("tickets.buttons.close"), emoji="close", custom_id=close_id),
        button(theme, tr("tickets.buttons.delete"), emoji="delete", custom_id=delete_id,
               style=discord.ButtonStyle.danger),
    )
    return b.build()


def build_ticket_closed(
    theme: Theme,
    tr: Translator,
    *,
    closed_by: str,
    reason: str | None,
    transcript_url: str | None,
    reopen_id: str | None,
    delete_id: str | None,
    code: str,
) -> ui.LayoutView:
    b = CV2(theme)
    b.heading(tr("tickets.closed.title"), emoji="lock")
    b.quote(tr("tickets.closed.description"))
    items = [Bullet(tr("tickets.closed.by"), closed_by, emoji="user")]
    if reason:
        items.append(Bullet(tr("tickets.closed.reason"), emoji="message", sub=[reason]))
    b.bullets(items)
    b.footer(tr("tickets.closed.footer", code=code))
    buttons: list[ui.Item[Any]] = []
    if reopen_id:
        buttons.append(button(theme, tr("tickets.buttons.reopen"), emoji="reopen", custom_id=reopen_id,
                              style=discord.ButtonStyle.success))
    if delete_id:
        buttons.append(button(theme, tr("tickets.buttons.delete"), emoji="delete", custom_id=delete_id,
                              style=discord.ButtonStyle.danger))
    if transcript_url:
        buttons.append(button(theme, tr("tickets.buttons.transcript"), emoji="transcript", url=transcript_url))
    if buttons:
        b.separator()
        b.actions(*buttons)
    return b.build()


# ====================================================================================== orders
@dataclass(slots=True)
class SelectGroup:
    """One select menu: ``key`` is appended to the custom ID, options are pre-built."""

    key: str
    placeholder: str
    options: list[discord.SelectOption]
    truncated: bool = False


def build_order_create(
    theme: Theme,
    tr: Translator,
    *,
    order_id: int,
    mode: Literal["products", "bundles"],
    product_groups: Sequence[SelectGroup],
    bundle_group: SelectGroup | None,
    cart_summary: str,
    item_count: int,
) -> ui.LayoutView:
    """Reference layout "Bestellung erstellen" (posted right after the welcome in order tickets)."""
    b = CV2(theme)
    b.banner("order")
    b.heading(
        tr("orders.create.title"),
        emoji="order",
        accessory=button(theme, tr("orders.create.guide"), emoji="guide", custom_id=cid("order.guide", order_id)),
    )
    b.text(tr("orders.create.body"))
    b.footer(cart_summary)
    b.separator()
    if mode == "products":
        if not product_groups:
            b.quote(tr("orders.create.no_products"))
        if len(product_groups) > 5 or any(g.truncated for g in product_groups):
            b.small(tr("orders.create.truncated"))
        for group in product_groups[:5]:
            b.actions(
                string_select(cid("order.add", order_id, group.key), group.options, placeholder=group.placeholder,
                              max_values=len(group.options))
            )
    elif bundle_group is not None and bundle_group.options:
        b.actions(
            string_select(cid("order.addb", order_id), bundle_group.options, placeholder=bundle_group.placeholder,
                          max_values=len(bundle_group.options))
        )
    else:
        b.quote(tr("orders.create.no_bundles"))
    toggle_mode = "bundles" if mode == "products" else "products"
    b.actions(
        button(theme, tr(f"orders.create.show_{toggle_mode}"), emoji="bundle" if toggle_mode == "bundles" else "product",
               custom_id=cid("order.view", order_id, toggle_mode)),
        button(theme, tr("orders.create.cart", count=item_count), emoji="cart", custom_id=cid("order.cart", order_id)),
        button(theme, tr("common.next"), emoji="next", custom_id=cid("order.view", order_id, "details"),
               style=discord.ButtonStyle.success, disabled=item_count == 0),
    )
    return b.build()


# ====================================================================================== reviews
def stars(theme: Theme, rating: int, total: int = 5) -> str:
    return theme.emojis["star"] * rating + theme.emojis["star_empty"] * (total - rating)


def build_review(
    theme: Theme,
    tr: Translator,
    *,
    user_mention: str,
    user_name: str,
    order_number: int,
    rating: int,
    messages: int,
    participants: int,
    attachments: int,
    feedback: str | None,
) -> ui.LayoutView:
    """Reference layout "Bewertung" (public review channel)."""
    b = CV2(theme)
    b.banner("review")
    b.text(tr("reviews.post.title", user=user_mention))
    b.small(tr("reviews.post.subline", number=order_number, user=user_name))
    b.separator()
    b.bullets(
        [
            Bullet(tr("reviews.post.rating"), f"{stars(theme, rating)} {tr('reviews.post.rating_value', rating=rating)}",
                   emoji="star"),
            Bullet(tr("reviews.post.stats"), emoji="stats", sub=[
                tr("reviews.post.stats_line", messages=messages, participants=participants, attachments=attachments)
            ]),
            Bullet(tr("reviews.post.feedback"), emoji="message",
                   sub=(feedback or tr("reviews.post.no_feedback")).splitlines() or [tr("reviews.post.no_feedback")]),
        ]
    )
    b.separator()
    b.small(tr("reviews.post.footer"))
    return b.build()


def build_review_request(
    theme: Theme, tr: Translator, *, order_id: int, order_number: int, guild_name: str, rating: int | None = None
) -> ui.LayoutView:
    """DM with 1–5 star buttons (each opens the feedback modal)."""
    b = CV2(theme)
    b.banner("review")
    b.heading(tr("reviews.request.title"), emoji="review", subline=tr("reviews.request.subline",
                                                                      number=order_number, guild=guild_name))
    b.quote(tr("reviews.request.text"))
    b.footer(tr("reviews.request.footer"))
    b.separator()
    b.actions(*[
        button(theme, str(n), emoji="star", custom_id=cid("review.rate", order_id, n),
               style=discord.ButtonStyle.success if rating == n else discord.ButtonStyle.secondary,
               disabled=rating is not None)
        for n in range(1, 6)
    ])
    return b.build()


# ====================================================================================== downloads
def build_download(
    theme: Theme,
    tr: Translator,
    *,
    download_id: int,
    title: str,
    meta_line: str | None,
    description: str | None,
    download_url: str,
    password: str | None,
    youtube_url: str | None,
    preview_image: str | None,
    footer: str,
    gated: bool,
    admin_rows: Sequence[Sequence[ui.Item[Any]]] | None = None,
) -> ui.LayoutView:
    """Reference layout "Download". ``gated`` = clicks are counted or a role is required → the link is only
    handed out through the tracked button (never shown directly in the message)."""
    b = CV2(theme)
    b.banner("downloads")
    b.heading(title, emoji="download", subline=meta_line)
    b.quote(description)
    items = [
        Bullet(tr("downloads.layout.download"),
               tr("downloads.layout.via_button") if gated else f"[{tr('downloads.layout.download_link')}]({download_url})",
               emoji="download"),
    ]
    if password:
        if gated:
            shown = tr("downloads.layout.via_button")
        elif password.startswith(("http://", "https://")):
            shown = f"[{tr('downloads.layout.password_link')}]({password})"
        else:
            shown = f"`{password}`"
        items.append(Bullet(tr("downloads.layout.password"), shown, emoji="password"))
    if youtube_url:
        items.append(Bullet(tr("downloads.layout.preview"), f"[{tr('downloads.layout.youtube')}]({youtube_url})",
                            emoji="preview"))
    b.bullets(items)
    if preview_image:
        b.separator()
        b.media(preview_image)
    b.separator()
    b.small(footer)
    buttons: list[ui.Item[Any]] = []
    if gated:
        buttons.append(button(theme, tr("downloads.layout.download_button"), emoji="download",
                              custom_id=cid("dl.get", download_id), style=discord.ButtonStyle.success))
    else:
        buttons.append(button(theme, tr("downloads.layout.download_button"), emoji="download", url=download_url))
    if youtube_url:
        buttons.append(button(theme, tr("downloads.layout.preview_button"), emoji="preview", url=youtube_url))
    if password and not password.startswith(("http://", "https://")):
        buttons.append(button(theme, tr("downloads.layout.copy_password"), emoji="copy",
                              custom_id=cid("dl.pw", download_id)))
    b.actions(*buttons)
    if admin_rows:
        b.separator()
        b.action_rows(admin_rows)
    return b.build()
