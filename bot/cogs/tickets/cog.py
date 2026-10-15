"""Ticket system: panels, modal questions, AI pre-check, channels, close/reopen/delete, transcripts,
inactivity timers, opening hours & holidays, member management."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import defaultdict
from functools import partial
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ...config.settings import WEEKDAYS, TicketCategory
from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import NotFound, PermissionDenied, UserError
from ...core.i18n import Translator
from ...core.logger import get_logger
from ...core.permissions import has_any_role, is_staff, require_admin, require_staff
from ...core.router import ComponentContext, cid, component
from ...core.tasks import resilient_iteration
from ...core.utils import parse_date, slugify, ts, utcnow
from ...ui.builder import CV2, Bullet, button
from ...ui.layouts import (
    CategoryEntry,
    build_confirm,
    build_ticket_closed,
    build_ticket_panel,
    build_ticket_welcome,
)
from ...ui.modals import FormModal, TextField
from .ai import AIValidator
from .hours import OpeningHoursService
from .service import Ticket, TicketService
from .transcripts import TranscriptResult, TranscriptService

_log = get_logger("tickets")
DELETE_DELAY_SECONDS = 5


class TicketsCog(XCog, name="Tickets"):
    feature = "tickets"

    ticket = app_commands.Group(name="ticket", description=T("ticket"), guild_only=True)
    holiday = app_commands.Group(name="holiday", description=T("ticket.holiday"), parent=ticket)

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.service = TicketService(bot)
        self.hours = OpeningHoursService(bot)
        self.transcripts = TranscriptService(bot, self.service)
        self.ai = AIValidator(bot)
        self._active: dict[int, int] = {}
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ================================================================== lifecycle
    async def on_load(self) -> None:
        self._active = await self.service.active_channels()
        self.bot.renderers.register("ticket.panel", self._render_panel_for)
        self.inactivity_loop.change_interval(minutes=max(1, self.cfg.inactivity.check_interval_minutes))
        self.inactivity_loop.start()

    async def on_unload(self) -> None:
        self.inactivity_loop.cancel()
        self.bot.renderers.unregister("ticket.panel")

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.tickets

    # ================================================================== helpers
    def category(self, key: str) -> TicketCategory:
        category = self.cfg.category(key)
        if category is None:
            raise NotFound("tickets.errors.unknown_category")
        return category

    def category_name(self, key: str, locale: str) -> str:
        category = self.cfg.category(key)
        return category.name(locale) if category else key

    def support_role_ids(self, category: TicketCategory | None) -> list[int]:
        ids = list(self.bot.config.roles.staff) + list(self.bot.config.roles.admin)
        if category is not None:
            ids.extend(category.support_role_ids)
        return list(dict.fromkeys(ids))

    def can_manage(self, user: discord.abc.User, ticket: Ticket) -> bool:
        if is_staff(self.bot, user):
            return True
        category = self.cfg.category(ticket.category_key)
        return bool(category and has_any_role(user, category.support_role_ids))

    async def ticket_here(self, interaction: discord.Interaction[Any]) -> Ticket:
        ticket = await self.service.by_channel(interaction.channel_id or 0)
        if ticket is None or ticket.status == "deleted":
            raise NotFound("tickets.errors.not_a_ticket")
        return ticket

    async def ticket_by_code(self, code: str, guild_id: int | None = None) -> Ticket:
        ticket = await self.service.by_code(code)
        if ticket is None or ticket.status == "deleted" or (guild_id is not None and ticket.guild_id != guild_id):
            raise NotFound("tickets.errors.not_a_ticket")
        return ticket

    def _entries(self, tr: Translator) -> list[CategoryEntry]:
        other = tr.other().locale
        return [
            CategoryEntry(
                key=c.key,
                emoji=c.emoji,
                name=c.name(tr.locale),
                other_name=c.name(other),
                description=c.description(tr.locale),
            )
            for c in self.cfg.categories
        ]

    async def hours_text(self, guild_id: int, tr: Translator) -> str | None:
        if not self.cfg.opening_hours.enabled:
            return None
        return await self.hours.describe(guild_id, tr)

    async def render_panel(self, guild_id: int, locale: str) -> discord.ui.LayoutView:
        if not self.cfg.categories:
            raise UserError("errors.not_configured", what="tickets.categories")
        tr = self.bot.i18n.translator(locale)
        show_ai = self.ai.available and any(c.ai_validation for c in self.cfg.categories)
        return build_ticket_panel(
            self.bot.theme,
            tr,
            categories=self._entries(tr),
            select_id=cid("ticket.open", locale),
            hours_text=await self.hours_text(guild_id, tr),
            show_ai_hint=show_ai,
        )

    async def _render_panel_for(
        self, interaction: discord.Interaction[Any], locale: str, args: tuple[str, ...]
    ) -> discord.ui.LayoutView:
        return await self.render_panel(interaction.guild_id or 0, locale)

    # ================================================================== opening a ticket
    async def _precheck(self, guild: discord.Guild, member: discord.abc.User, locale: str) -> None:
        entry = await self.service.blacklist_entry(guild.id, member.id)
        if entry is not None:
            tr = self.bot.i18n.translator(locale)
            until = ts(entry.expires_at, "f") if entry.expires_at else tr("tickets.blacklist.permanent")
            raise UserError("tickets.errors.blacklisted", reason=entry.reason, until=until)
        if await self.service.open_count(guild.id, member.id) >= self.cfg.max_open_per_user:
            raise UserError("tickets.errors.limit", limit=self.cfg.max_open_per_user)
        if self.cfg.opening_hours.block_outside:
            status = await self.hours.status(guild.id)
            if not status.is_open:
                when = ts(status.next_open, "F") if status.next_open else "—"
                raise UserError("tickets.errors.closed_hours", next=when)

    async def _reset_panel(self, interaction: discord.Interaction[Any], locale: str) -> None:
        """Reset the select of a public panel so the same category can be chosen again."""
        message = interaction.message
        if message is None or message.flags.ephemeral or interaction.guild_id is None:
            return
        try:
            await message.edit(view=await self.render_panel(interaction.guild_id, locale))
        except discord.HTTPException:
            _log.debug("Could not reset ticket panel %s", message.id, exc_info=True)

    async def start_ticket(self, interaction: discord.Interaction[Any], category_key: str, locale: str) -> None:
        """Entry point for every "open a ticket" action (panel, shop, partnership, …)."""
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        category = self.category(category_key)
        await self._precheck(interaction.guild, interaction.user, locale)
        if category.questions:
            await interaction.response.send_modal(self._question_modal(category, locale))
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.create_ticket(interaction, category, locale, answers=[])

    @component("ticket.open")
    async def on_panel_select(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        if interaction.guild is None or not ctx.values:
            return
        locale = self.bot.i18n.resolve_locale(ctx.arg(0))
        await self.start_ticket(interaction, ctx.values[0], locale)
        await self._reset_panel(interaction, locale)

    def _question_modal(self, category: TicketCategory, locale: str) -> FormModal:
        de = locale == "de"
        fields = [
            TextField(
                key=q.key,
                label=q.label_de if de else q.label_en,
                placeholder=(q.placeholder_de if de else q.placeholder_en) or None,
                paragraph=q.style == "paragraph",
                required=q.required,
                min_length=q.min_length,
                max_length=q.max_length,
            )
            for q in category.questions
        ]
        tr = self.bot.i18n.translator(locale)
        return FormModal(
            self.bot,
            title=tr("tickets.modal_title", category=category.name(locale)),
            fields=fields,
            on_submit=partial(self._questions_submitted, category=category, locale=locale),
        )

    async def _questions_submitted(
        self, interaction: discord.Interaction[Any], values: dict[str, Any], *, category: TicketCategory, locale: str
    ) -> None:
        de = locale == "de"
        answers = [
            {"key": q.key, "question": q.label_de if de else q.label_en, "answer": str(values.get(q.key, ""))}
            for q in category.questions
        ]
        await interaction.response.defer(ephemeral=True, thinking=True)
        tr = self.bot.i18n.translator(locale)
        if category.ai_validation and self.ai.available:
            verdict = await self.ai.validate(category=category.name("en"), answers=answers, locale=locale)
            if not verdict.valid:
                await interaction.followup.send(
                    view=self.bot.notice(
                        kind="warning",
                        title=tr("tickets.ai.rejected_title"),
                        description=tr("tickets.ai.rejected", reason=verdict.reason or tr("tickets.ai.no_reason")),
                        footer=tr("tickets.ai.footer"),
                    ),
                    ephemeral=True,
                )
                await self.bot.audit.log(
                    interaction.guild, "tickets", "ticket.ai_rejected", title=tr("tickets.log.ai_rejected"),
                    emoji="ai", actor=interaction.user,
                    lines=[tr("tickets.log.category", category=category.name(locale)),
                           tr("tickets.log.reason", reason=verdict.reason or "—")],
                    details={"answers": answers, "reason": verdict.reason},
                )
                return
        await self.create_ticket(interaction, category, locale, answers=answers)

    async def create_ticket(
        self,
        interaction: discord.Interaction[Any],
        category: TicketCategory,
        locale: str,
        *,
        answers: list[dict[str, str]],
    ) -> Ticket:
        guild = interaction.guild
        member = interaction.user
        assert guild is not None and isinstance(member, discord.Member)
        tr = self.bot.i18n.translator(locale)
        async with self._locks[member.id]:
            await self._precheck(guild, member, locale)
            ticket = await self.service.create(
                guild_id=guild.id, category_key=category.key, owner_id=member.id, locale=locale, answers=answers
            )
            try:
                channel = await self._create_channel(guild, member, category, ticket, tr)
            except discord.HTTPException as exc:
                await self.service.discard(ticket.id)
                _log.warning("Ticket channel creation failed: %s", exc)
                raise UserError("tickets.errors.channel_failed") from exc
            await self.service.set_channel(ticket.id, channel.id)
            ticket.channel_id = channel.id
            self._active[channel.id] = ticket.id

        status = await self.hours.status(guild.id)
        mentions = [member.mention]
        if category.ping_support_roles:
            mentions.extend(f"<@&{rid}>" for rid in category.support_role_ids if guild.get_role(rid))
        view = build_ticket_welcome(
            self.bot.theme,
            tr,
            mention=" ".join(mentions),
            category_name=category.name(locale),
            category_emoji=category.emoji,
            code=ticket.code,
            hours_text=await self.hours_text(guild.id, tr),
            answers=[(a["question"], a["answer"]) for a in answers],
            outside_hours_until=ts(status.next_open, "F") if not status.is_open and status.next_open else None,
            close_id=cid("ticket.close", ticket.code),
            delete_id=cid("ticket.delete", ticket.code),
        )
        welcome = await channel.send(view=view, allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        if self.cfg.pin_welcome:
            try:
                await welcome.pin(reason=f"Ticket {ticket.code}")
            except discord.HTTPException:
                _log.debug("Could not pin welcome message in %s", channel.id)
        await self.service.set_welcome_message(ticket.id, welcome.id)

        await interaction.followup.send(
            view=self.bot.notice(
                kind="success",
                title=tr("tickets.created_title"),
                description=tr("tickets.created", channel=channel.mention),
                rows=[[button(self.bot.theme, tr("tickets.buttons.goto"), emoji="link", url=channel.jump_url)]],
            ),
            ephemeral=True,
        )
        await self.bot.audit.log(
            guild, "tickets", "ticket.create", title=tr("tickets.log.created"), emoji="ticket", actor=member,
            lines=[
                tr("tickets.log.ticket", code=ticket.code, channel=channel.mention),
                tr("tickets.log.category", category=category.name(locale)),
            ],
            target_id=member.id, channel_id=channel.id, details={"code": ticket.code, "category": category.key},
        )
        self.bot.dispatch("xs_ticket_created", ticket, channel, member)
        return ticket

    async def _create_channel(
        self,
        guild: discord.Guild,
        member: discord.Member,
        category: TicketCategory,
        ticket: Ticket,
        tr: Translator,
    ) -> discord.TextChannel:
        user_perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True
        )
        overwrites: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: user_perms,
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True,
                embed_links=True, manage_messages=True, manage_channels=True,
            ),
        }
        for role_id in self.support_role_ids(category):
            role = guild.get_role(role_id)
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True, attach_files=True,
                    embed_links=True, manage_messages=True,
                )
        parent = guild.get_channel(category.discord_category_id) if category.discord_category_id else None
        name = self.channel_name(category.channel_prefix, member.name)
        return await guild.create_text_channel(
            name,
            category=parent if isinstance(parent, discord.CategoryChannel) else None,
            overwrites=overwrites,
            topic=tr("tickets.channel_topic", code=ticket.code, category=category.name(tr.locale), user=member.id),
            reason=f"Ticket {ticket.code}",
        )

    @staticmethod
    def channel_name(prefix: str, username: str, status_emoji: str | None = None) -> str:
        base = slugify(f"{prefix}-{username}", max_length=90)
        return f"{status_emoji}{base}" if status_emoji else base

    # ================================================================== closing
    def _ensure_can_close(self, user: discord.abc.User, ticket: Ticket) -> None:
        if user.id != ticket.owner_id and not self.can_manage(user, ticket):
            raise PermissionDenied()

    async def close_ticket(self, ticket: Ticket, *, actor: discord.abc.User | None, reason: str | None) -> None:
        guild = self.bot.get_guild(ticket.guild_id)
        if guild is None:
            return
        await self.service.mark_closed(ticket.id, actor.id if actor else None, reason)
        tr = self.bot.i18n.translator(ticket.locale)
        category = self.cfg.category(ticket.category_key)
        category_name = self.category_name(ticket.category_key, ticket.locale)
        channel = guild.get_channel(ticket.channel_id or 0)
        transcript: TranscriptResult | None = None
        closed_by = actor.mention if actor else tr("tickets.system")

        if isinstance(channel, discord.TextChannel):
            for user_id in [ticket.owner_id, *await self.service.member_ids(ticket.id)]:
                target = guild.get_member(user_id)
                if target is not None:
                    await channel.set_permissions(
                        target, view_channel=True, send_messages=False, read_message_history=True,
                        reason=f"Ticket {ticket.code} closed",
                    )
            closed_parent = guild.get_channel(self.cfg.closed_category_id or 0)
            if isinstance(closed_parent, discord.CategoryChannel):
                try:
                    await channel.edit(category=closed_parent, sync_permissions=False)
                except discord.HTTPException:
                    _log.warning("Could not move ticket %s to the closed category", ticket.code)
            if self.cfg.transcripts.enabled:
                try:
                    transcript = await self.transcripts.create(ticket, channel, category_name)
                except Exception:  # noqa: BLE001 – a failing transcript must not block closing
                    _log.exception("Transcript for ticket %s failed", ticket.code)
            await channel.send(
                view=build_ticket_closed(
                    self.bot.theme, tr, closed_by=closed_by, reason=reason,
                    transcript_url=transcript.url if transcript else None,
                    reopen_id=cid("ticket.reopen", ticket.code), delete_id=cid("ticket.delete", ticket.code),
                    code=ticket.code,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await self._dm_closed(guild, ticket, tr, category_name, reason, transcript)
        attach = transcript is not None and (
            self.cfg.transcripts.send_file_to_log or self.cfg.transcripts.storage == "discord"
        )
        await self.bot.audit.log(
            guild, "tickets", "ticket.close", title=tr("tickets.log.closed"), emoji="close", actor=actor,
            lines=[
                tr("tickets.log.ticket", code=ticket.code, channel=f"<#{ticket.channel_id}>"),
                tr("tickets.log.category", category=category_name),
                tr("tickets.log.owner", user=f"<@{ticket.owner_id}>"),
                tr("tickets.log.reason", reason=reason or "—"),
                *( [tr("tickets.log.transcript", url=transcript.url)] if transcript and transcript.url else [] ),
            ],
            target_id=ticket.owner_id, channel_id=ticket.channel_id,
            details={"code": ticket.code, "reason": reason, "category": category.key if category else None},
            file=transcript.as_file() if attach and transcript else None,
        )
        self.bot.dispatch("xs_ticket_closed", ticket)

    async def _dm_closed(
        self, guild: discord.Guild, ticket: Ticket, tr: Translator, category_name: str, reason: str | None,
        transcript: TranscriptResult | None,
    ) -> None:
        if not self.cfg.transcripts.dm_user:
            return
        user = guild.get_member(ticket.owner_id) or await self._fetch_user(ticket.owner_id)
        if user is None:
            return
        b = CV2(self.bot.theme)
        b.banner("tickets")
        b.heading(tr("tickets.dm_closed.title"), emoji="ticket")
        b.quote(tr("tickets.dm_closed.description", guild=guild.name))
        items = [
            Bullet(tr("tickets.dm_closed.ticket"), f"`{ticket.code}`", emoji="ticket"),
            Bullet(tr("tickets.dm_closed.category"), category_name, emoji="category"),
        ]
        if reason:
            items.append(Bullet(tr("tickets.closed.reason"), emoji="message", sub=[reason]))
        b.bullets(items)
        kwargs: dict[str, Any] = {}
        if transcript is not None and transcript.url:
            b.separator()
            b.actions(button(self.bot.theme, tr("tickets.buttons.transcript"), emoji="transcript", url=transcript.url))
        elif transcript is not None:
            b.file(transcript.file_name)
            kwargs["file"] = transcript.as_file()
        b.footer(tr("tickets.dm_closed.footer"))
        await self.bot.safe_dm(user, b.build(), **kwargs)

    async def _fetch_user(self, user_id: int) -> discord.User | None:
        try:
            return await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None

    async def reopen_ticket(self, ticket: Ticket, actor: discord.abc.User) -> None:
        guild = self.bot.get_guild(ticket.guild_id)
        if guild is None:
            return
        tr = self.bot.i18n.translator(ticket.locale)
        await self.service.mark_open(ticket.id)
        channel = guild.get_channel(ticket.channel_id or 0)
        if isinstance(channel, discord.TextChannel):
            for user_id in [ticket.owner_id, *await self.service.member_ids(ticket.id)]:
                target = guild.get_member(user_id)
                if target is not None:
                    await channel.set_permissions(
                        target, view_channel=True, send_messages=True, read_message_history=True,
                        attach_files=True, embed_links=True, reason=f"Ticket {ticket.code} reopened",
                    )
            category = self.cfg.category(ticket.category_key)
            parent = guild.get_channel(category.discord_category_id or 0) if category else None
            if isinstance(parent, discord.CategoryChannel) and channel.category_id != parent.id:
                try:
                    await channel.edit(category=parent, sync_permissions=False)
                except discord.HTTPException:
                    _log.warning("Could not move ticket %s back to its category", ticket.code)
            await channel.send(
                view=self.bot.notice(kind="success", title=tr("tickets.reopened_title"),
                                     description=tr("tickets.reopened", user=actor.mention)),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        self._active[ticket.channel_id or 0] = ticket.id
        await self.bot.audit.log(
            guild, "tickets", "ticket.reopen", title=tr("tickets.log.reopened"), emoji="reopen", actor=actor,
            lines=[tr("tickets.log.ticket", code=ticket.code, channel=f"<#{ticket.channel_id}>")],
            channel_id=ticket.channel_id, details={"code": ticket.code},
        )
        self.bot.dispatch("xs_ticket_reopened", ticket)

    async def delete_ticket(self, ticket: Ticket, actor: discord.abc.User | None) -> None:
        guild = self.bot.get_guild(ticket.guild_id)
        if guild is None:
            return
        tr = self.bot.i18n.translator(ticket.locale)
        channel = guild.get_channel(ticket.channel_id or 0)
        transcript: TranscriptResult | None = None
        if (
            isinstance(channel, discord.TextChannel)
            and self.cfg.transcripts.enabled
            and not await self.service.has_transcript(ticket.id)
        ):
            try:
                transcript = await self.transcripts.create(
                    ticket, channel, self.category_name(ticket.category_key, ticket.locale)
                )
            except Exception:  # noqa: BLE001
                _log.exception("Transcript for ticket %s failed", ticket.code)
        await self.service.mark_deleted(ticket.id)
        self._active.pop(ticket.channel_id or 0, None)
        attach = transcript is not None and (
            self.cfg.transcripts.send_file_to_log or self.cfg.transcripts.storage == "discord"
        )
        await self.bot.audit.log(
            guild, "tickets", "ticket.delete", title=tr("tickets.log.deleted"), emoji="delete", actor=actor,
            lines=[
                tr("tickets.log.ticket", code=ticket.code, channel=f"#{getattr(channel, 'name', '?')}"),
                tr("tickets.log.owner", user=f"<@{ticket.owner_id}>"),
            ],
            channel_id=ticket.channel_id, details={"code": ticket.code},
            file=transcript.as_file() if attach and transcript else None,
        )
        self.bot.dispatch("xs_ticket_deleted", ticket)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    view=self.bot.notice(kind="warning", title=tr("tickets.deleting", seconds=DELETE_DELAY_SECONDS))
                )
                await asyncio.sleep(DELETE_DELAY_SECONDS)
                await channel.delete(reason=f"Ticket {ticket.code} deleted")
            except discord.HTTPException:
                _log.warning("Could not delete channel of ticket %s", ticket.code)

    # ================================================================== components
    @component("ticket.close")
    async def on_close_button(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ticket = await self.ticket_by_code(ctx.arg(0) or "", interaction.guild_id)
        self._ensure_can_close(interaction.user, ticket)
        tr = self.tr(interaction)
        if not ticket.is_open:
            raise UserError("tickets.errors.already_closed")
        await interaction.response.send_message(
            view=build_confirm(
                self.bot.theme, tr, title=tr("tickets.confirm_close.title"),
                description=tr("tickets.confirm_close.description"),
                confirm_id=cid("ticket.close_ok", ticket.code), cancel_id=cid("dismiss"),
            ),
            ephemeral=True,
        )

    @component("ticket.close_ok")
    async def on_close_confirm(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ticket = await self.ticket_by_code(ctx.arg(0) or "", interaction.guild_id)
        self._ensure_can_close(interaction.user, ticket)
        tr = self.tr(interaction)
        if not ticket.is_open:
            raise UserError("tickets.errors.already_closed")
        await interaction.response.edit_message(view=self.bot.notice(kind="info", title=tr("tickets.closing")))
        await self.close_ticket(ticket, actor=interaction.user, reason=None)
        await interaction.edit_original_response(view=self.bot.notice(kind="success", title=tr("tickets.closed_ok")))

    @component("ticket.reopen")
    async def on_reopen_button(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ticket = await self.ticket_by_code(ctx.arg(0) or "", interaction.guild_id)
        if not self.can_manage(interaction.user, ticket):
            raise PermissionDenied()
        if ticket.is_open:
            raise UserError("tickets.errors.already_open")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.reopen_ticket(ticket, interaction.user)
        tr = self.tr(interaction)
        await interaction.followup.send(view=self.bot.notice(kind="success", title=tr("tickets.reopened_title")),
                                        ephemeral=True)

    @component("ticket.delete")
    async def on_delete_button(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ticket = await self.ticket_by_code(ctx.arg(0) or "", interaction.guild_id)
        if not self.can_manage(interaction.user, ticket):
            raise PermissionDenied()
        tr = self.tr(interaction)
        await interaction.response.send_message(
            view=build_confirm(
                self.bot.theme, tr, title=tr("tickets.confirm_delete.title"),
                description=tr("tickets.confirm_delete.description", code=ticket.code),
                confirm_id=cid("ticket.delete_ok", ticket.code), cancel_id=cid("dismiss"), danger=True,
            ),
            ephemeral=True,
        )

    @component("ticket.delete_ok")
    async def on_delete_confirm(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ticket = await self.ticket_by_code(ctx.arg(0) or "", interaction.guild_id)
        if not self.can_manage(interaction.user, ticket):
            raise PermissionDenied()
        tr = self.tr(interaction)
        await interaction.response.edit_message(
            view=self.bot.notice(kind="warning", title=tr("tickets.deleting", seconds=DELETE_DELAY_SECONDS))
        )
        await self.delete_ticket(ticket, interaction.user)

    # ================================================================== slash commands
    @app_commands.command(name="close", description=T("close"))
    @app_commands.describe(reason=T("close.reason"))
    @app_commands.guild_only()
    async def close_cmd(self, interaction: discord.Interaction[Any], reason: str | None = None) -> None:
        ticket = await self.ticket_here(interaction)
        self._ensure_can_close(interaction.user, ticket)
        if not ticket.is_open:
            raise UserError("tickets.errors.already_closed")
        tr = self.tr(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.close_ticket(ticket, actor=interaction.user, reason=reason)
        await interaction.followup.send(view=self.bot.notice(kind="success", title=tr("tickets.closed_ok")),
                                        ephemeral=True)

    @app_commands.command(name="reopen", description=T("reopen"))
    @app_commands.guild_only()
    async def reopen_cmd(self, interaction: discord.Interaction[Any]) -> None:
        ticket = await self.ticket_here(interaction)
        if not self.can_manage(interaction.user, ticket):
            raise PermissionDenied()
        if ticket.is_open:
            raise UserError("tickets.errors.already_open")
        tr = self.tr(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.reopen_ticket(ticket, interaction.user)
        await interaction.followup.send(view=self.bot.notice(kind="success", title=tr("tickets.reopened_title")),
                                        ephemeral=True)

    @app_commands.command(name="delete", description=T("delete"))
    @app_commands.guild_only()
    async def delete_cmd(self, interaction: discord.Interaction[Any]) -> None:
        ticket = await self.ticket_here(interaction)
        if not self.can_manage(interaction.user, ticket):
            raise PermissionDenied()
        tr = self.tr(interaction)
        await interaction.response.send_message(
            view=build_confirm(
                self.bot.theme, tr, title=tr("tickets.confirm_delete.title"),
                description=tr("tickets.confirm_delete.description", code=ticket.code),
                confirm_id=cid("ticket.delete_ok", ticket.code), cancel_id=cid("dismiss"), danger=True,
            ),
            ephemeral=True,
        )

    @ticket.command(name="panel", description=T("ticket.panel"))
    @app_commands.describe(channel=T("ticket.panel.channel"))
    @require_admin()
    async def panel_cmd(self, interaction: discord.Interaction[Any], channel: discord.TextChannel | None = None) -> None:
        assert interaction.guild is not None
        target = channel or interaction.guild.get_channel(self.cfg.panel_channel_id or 0) or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise UserError("errors.invalid_input")
        locale = self.bot.guild_locale(interaction.guild_id)
        message = await target.send(view=await self.render_panel(interaction.guild.id, locale))
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("tickets.panel_sent", channel=target.mention))
        await self.bot.audit.log(interaction.guild, "tickets", "ticket.panel", title=tr("tickets.log.panel"),
                                 emoji="ticket", actor=interaction.user, lines=[message.jump_url])

    @ticket.command(name="open", description=T("ticket.open"))
    async def open_cmd(self, interaction: discord.Interaction[Any]) -> None:
        locale = self.bot.locale_for(interaction)
        await interaction.response.send_message(
            view=await self.render_panel(interaction.guild_id or 0, locale), ephemeral=True
        )

    @ticket.command(name="add", description=T("ticket.add"))
    @app_commands.describe(user=T("ticket.add.user"))
    async def add_cmd(self, interaction: discord.Interaction[Any], user: discord.Member) -> None:
        ticket = await self.ticket_here(interaction)
        if not self.can_manage(interaction.user, ticket):
            raise PermissionDenied()
        channel = interaction.channel
        assert isinstance(channel, discord.TextChannel)
        await channel.set_permissions(
            user, view_channel=True, send_messages=ticket.is_open, read_message_history=True, attach_files=True,
            embed_links=True, reason=f"Ticket {ticket.code}: add",
        )
        await self.service.add_member(ticket.id, user.id, interaction.user.id)
        tr = self.bot.i18n.translator(ticket.locale)
        await interaction.response.send_message(
            view=self.bot.notice(kind="success", title=tr("tickets.member_added", user=user.mention)),
            allowed_mentions=discord.AllowedMentions(users=[user]),
        )
        await self.bot.audit.log(interaction.guild, "tickets", "ticket.add", title=tr("tickets.log.member_added"),
                                 emoji="add", actor=interaction.user, target_id=user.id,
                                 lines=[tr("tickets.log.ticket", code=ticket.code, channel=channel.mention),
                                        tr("tickets.log.owner", user=user.mention)])

    @ticket.command(name="remove", description=T("ticket.remove"))
    @app_commands.describe(user=T("ticket.remove.user"))
    async def remove_cmd(self, interaction: discord.Interaction[Any], user: discord.Member) -> None:
        ticket = await self.ticket_here(interaction)
        if not self.can_manage(interaction.user, ticket):
            raise PermissionDenied()
        if user.id == ticket.owner_id:
            raise UserError("tickets.errors.cannot_remove_owner")
        channel = interaction.channel
        assert isinstance(channel, discord.TextChannel)
        await channel.set_permissions(user, overwrite=None, reason=f"Ticket {ticket.code}: remove")
        await self.service.remove_member(ticket.id, user.id)
        tr = self.bot.i18n.translator(ticket.locale)
        await interaction.response.send_message(
            view=self.bot.notice(kind="info", title=tr("tickets.member_removed", user=user.mention)),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.bot.audit.log(interaction.guild, "tickets", "ticket.remove", title=tr("tickets.log.member_removed"),
                                 emoji="remove", actor=interaction.user, target_id=user.id,
                                 lines=[tr("tickets.log.ticket", code=ticket.code, channel=channel.mention)])

    @ticket.command(name="move", description=T("ticket.move"))
    @app_commands.describe(category=T("ticket.move.category"))
    async def move_cmd(self, interaction: discord.Interaction[Any], category: str) -> None:
        ticket = await self.ticket_here(interaction)
        if not self.can_manage(interaction.user, ticket):
            raise PermissionDenied()
        new_category = self.category(category)
        old_category = self.cfg.category(ticket.category_key)
        channel = interaction.channel
        guild = interaction.guild
        assert isinstance(channel, discord.TextChannel) and guild is not None
        await interaction.response.defer(thinking=True)
        for role_id in set(self.support_role_ids(old_category)) - set(self.support_role_ids(new_category)):
            role = guild.get_role(role_id)
            if role is not None:
                await channel.set_permissions(role, overwrite=None)
        for role_id in self.support_role_ids(new_category):
            role = guild.get_role(role_id)
            if role is not None:
                await channel.set_permissions(
                    role, view_channel=True, send_messages=True, read_message_history=True,
                    attach_files=True, embed_links=True, manage_messages=True,
                )
        parent = guild.get_channel(new_category.discord_category_id or 0)
        owner = guild.get_member(ticket.owner_id)
        edit_kwargs: dict[str, Any] = {
            "name": self.channel_name(new_category.channel_prefix, owner.name if owner else str(ticket.owner_id))
        }
        if isinstance(parent, discord.CategoryChannel):
            edit_kwargs["category"] = parent
            edit_kwargs["sync_permissions"] = False
        try:
            await channel.edit(**edit_kwargs, reason=f"Ticket {ticket.code}: move")
        except discord.HTTPException:
            _log.warning("Could not edit channel while moving ticket %s", ticket.code)
        await self.service.set_category(ticket.id, guild.id, new_category.key)
        tr = self.bot.i18n.translator(ticket.locale)
        await interaction.followup.send(
            view=self.bot.notice(kind="success", title=tr("tickets.moved", category=new_category.name(ticket.locale)))
        )
        await self.bot.audit.log(guild, "tickets", "ticket.move", title=tr("tickets.log.moved"), emoji="category",
                                 actor=interaction.user,
                                 lines=[tr("tickets.log.ticket", code=ticket.code, channel=channel.mention),
                                        tr("tickets.log.category", category=new_category.name(ticket.locale))])
        ticket.category_key = new_category.key
        self.bot.dispatch("xs_ticket_moved", ticket, channel)

    @move_cmd.autocomplete("category")
    async def _category_autocomplete(
        self, interaction: discord.Interaction[Any], current: str
    ) -> list[app_commands.Choice[str]]:
        locale = self.bot.locale_for(interaction)
        return [
            app_commands.Choice(name=c.name(locale)[:100], value=c.key)
            for c in self.cfg.categories
            if current.lower() in c.name(locale).lower() or current.lower() in c.key
        ][:25]

    @ticket.command(name="rename", description=T("ticket.rename"))
    @app_commands.describe(name=T("ticket.rename.name"))
    async def rename_cmd(self, interaction: discord.Interaction[Any], name: app_commands.Range[str, 1, 90]) -> None:
        ticket = await self.ticket_here(interaction)
        if not self.can_manage(interaction.user, ticket):
            raise PermissionDenied()
        channel = interaction.channel
        assert isinstance(channel, discord.TextChannel)
        new_name = slugify(name)
        try:
            await channel.edit(name=new_name, reason=f"Ticket {ticket.code}: rename")
        except discord.HTTPException as exc:
            raise UserError("tickets.errors.rename_failed") from exc
        tr = self.bot.i18n.translator(ticket.locale)
        await interaction.response.send_message(
            view=self.bot.notice(kind="success", title=tr("tickets.renamed", name=new_name))
        )
        await self.bot.audit.log(interaction.guild, "tickets", "ticket.rename", title=tr("tickets.log.renamed"),
                                 emoji="edit", actor=interaction.user,
                                 lines=[tr("tickets.log.ticket", code=ticket.code, channel=channel.mention)])

    # ---------------------------------------------------------------- opening hours & holidays
    @ticket.command(name="hours", description=T("ticket.hours"))
    async def hours_cmd(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        schedule = await self.hours.schedule(interaction.guild_id)
        status = await self.hours.status(interaction.guild_id)
        holidays = await self.hours.holidays(interaction.guild_id)
        today = dt.date.today()
        upcoming = sorted((d, n) for d, n in holidays.items() if d >= today)[:10]
        b = CV2(self.bot.theme)
        b.banner("tickets")
        b.heading(tr("tickets.hours.title"), emoji="clock")
        b.quote(tr("tickets.hours.open_now") if status.is_open else tr(
            "tickets.hours.closed_now", next=ts(status.next_open, "F") if status.next_open else "—"))
        b.bullets([
            Bullet(tr(f"tickets.hours.days_short.{day}"), schedule.get(day) or tr("tickets.hours.closed"),
                   emoji="calendar")
            for day in WEEKDAYS
        ])
        if upcoming:
            b.block_break()
            b.text(f"**{tr('tickets.hours.holidays')}**")
            b.bullets([f"`{d:%d.%m.%Y}` {n}" for d, n in upcoming], default_emoji="holiday")
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    @ticket.command(name="sethours", description=T("ticket.sethours"))
    @app_commands.describe(day=T("ticket.sethours.day"), span=T("ticket.sethours.span"))
    @app_commands.choices(day=[app_commands.Choice(name=d, value=d) for d in WEEKDAYS])
    @require_admin()
    async def sethours_cmd(self, interaction: discord.Interaction[Any], day: str, span: str) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        value: str | None = None if span.strip().lower() in {"off", "closed", "zu", "-"} else span.strip()
        try:
            await self.hours.set_day(interaction.guild_id, day, value)
        except ValueError as exc:
            raise UserError("tickets.errors.invalid_span") from exc
        await self.bot.respond_notice(interaction, title=tr("tickets.hours.updated", day=day,
                                                            span=value or tr("tickets.hours.closed")))
        await self.bot.audit.log(interaction.guild, "tickets", "hours.update", title=tr("tickets.log.hours"),
                                 emoji="clock", actor=interaction.user, lines=[f"`{day}` → `{value or 'off'}`"])

    @holiday.command(name="add", description=T("ticket.holiday.add"))
    @app_commands.describe(date=T("ticket.holiday.add.date"), name=T("ticket.holiday.add.name"))
    @require_staff()
    async def holiday_add(self, interaction: discord.Interaction[Any], date: str,
                          name: app_commands.Range[str, 1, 100]) -> None:
        assert interaction.guild_id is not None
        day = parse_date(date)
        if day is None:
            raise UserError("errors.invalid_date")
        await self.hours.add_holiday(interaction.guild_id, day, name, interaction.user.id)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("tickets.holidays.added", date=f"{day:%d.%m.%Y}", name=name))
        await self.bot.audit.log(interaction.guild, "tickets", "holiday.add", title=tr("tickets.log.holiday"),
                                 emoji="holiday", actor=interaction.user, lines=[f"`{day:%d.%m.%Y}` {name}"])

    @holiday.command(name="remove", description=T("ticket.holiday.remove"))
    @app_commands.describe(date=T("ticket.holiday.remove.date"))
    @require_staff()
    async def holiday_remove(self, interaction: discord.Interaction[Any], date: str) -> None:
        assert interaction.guild_id is not None
        day = parse_date(date)
        if day is None:
            raise UserError("errors.invalid_date")
        if not await self.hours.remove_holiday(interaction.guild_id, day):
            raise NotFound()
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("tickets.holidays.removed", date=f"{day:%d.%m.%Y}"))

    @holiday.command(name="list", description=T("ticket.holiday.list"))
    async def holiday_list(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        holidays = sorted((await self.hours.holidays(interaction.guild_id)).items())
        b = CV2(self.bot.theme)
        b.heading(tr("tickets.hours.holidays"), emoji="holiday")
        if holidays:
            b.bullets([f"`{d:%d.%m.%Y}` {n or tr('tickets.holidays.unnamed')}" for d, n in holidays[:40]],
                      default_emoji="calendar")
        else:
            b.quote(tr("tickets.holidays.none"))
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    # ================================================================== activity tracking & timers
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        ticket_id = self._active.get(message.channel.id)
        if ticket_id is None:
            return
        await self.service.record_message(
            ticket_id, message.author.id, is_staff=is_staff(self.bot, message.author),
            attachments=len(message.attachments),
        )
        await self.service.touch(ticket_id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        ticket_id = self._active.pop(channel.id, None)
        if ticket_id is not None:
            await self.service.mark_deleted(ticket_id)

    @tasks.loop(minutes=10)
    @resilient_iteration("ticket_inactivity")
    async def inactivity_loop(self) -> None:
        cfg = self.cfg.inactivity
        if not cfg.enabled:
            return
        now = utcnow()
        close_before = now - dt.timedelta(hours=cfg.close_after_hours)
        if cfg.reminder_before_hours > 0:
            remind_before = close_before + dt.timedelta(hours=cfg.reminder_before_hours)
            for ticket in await self.service.due_for_reminder(remind_before):
                if self.bot.get_guild(ticket.guild_id) is not None:
                    await self._send_reminder(ticket, ticket.last_activity_at + dt.timedelta(hours=cfg.close_after_hours))
        for ticket in await self.service.due_for_close(close_before):
            if self.bot.get_guild(ticket.guild_id) is None:
                continue
            tr = self.bot.i18n.translator(ticket.locale)
            try:
                await self.close_ticket(ticket, actor=None, reason=tr("tickets.inactivity.reason"))
            except Exception:  # noqa: BLE001 – one broken ticket must not block the others
                _log.exception("Auto-close of ticket %s failed", ticket.code)
        if cfg.delete_after_close_hours > 0:
            for ticket in await self.service.due_for_delete(now - dt.timedelta(hours=cfg.delete_after_close_hours)):
                if self.bot.get_guild(ticket.guild_id) is None:
                    continue
                try:
                    await self.delete_ticket(ticket, actor=None)
                except Exception:  # noqa: BLE001 – one broken ticket must not block the others
                    _log.exception("Auto-delete of ticket %s failed", ticket.code)

    @inactivity_loop.before_loop
    async def _before_inactivity(self) -> None:
        await self.bot.wait_until_ready()

    @inactivity_loop.error
    async def _inactivity_error(self, error: BaseException) -> None:
        _log.exception("Inactivity loop crashed", exc_info=error)

    async def _send_reminder(self, ticket: Ticket, close_at: dt.datetime) -> None:
        guild = self.bot.get_guild(ticket.guild_id)
        assert guild is not None
        tr = self.bot.i18n.translator(ticket.locale)
        channel = guild.get_channel(ticket.channel_id or 0)
        title = tr("tickets.inactivity.title")
        description = tr("tickets.inactivity.description", when=ts(close_at, "R"))
        if isinstance(channel, discord.TextChannel):
            in_channel = (
                CV2(self.bot.theme)
                .mention(f"<@{ticket.owner_id}>")
                .heading(title, emoji="warning", level=3)
                .quote(description)
                .build()
            )
            await channel.send(view=in_channel, allowed_mentions=discord.AllowedMentions(users=True))
            user = guild.get_member(ticket.owner_id)
            if user is not None:
                await self.bot.safe_dm(
                    user,
                    self.bot.notice(
                        kind="warning", title=title, description=description,
                        rows=[[button(self.bot.theme, tr("tickets.buttons.goto"), emoji="link", url=channel.jump_url)]],
                    ),
                )
        await self.service.mark_reminded(ticket.id)
