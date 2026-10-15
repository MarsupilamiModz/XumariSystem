"""Ticket blacklist management via ``/blacklistpanel`` (router based, survives restarts)."""

from __future__ import annotations

import math
from typing import Any

import discord
from discord import app_commands

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import NotFound, UserError
from ...core.i18n import Translator
from ...core.permissions import ensure_staff, require_staff
from ...core.router import ComponentContext, cid, component
from ...core.utils import parse_duration, truncate, ts, utcnow
from ...ui.builder import CV2, Bullet, button, select_option, string_select
from ...ui.modals import EntityField, FormModal, TextField
from .service import TicketService

PAGE_SIZE = 8


class BlacklistCog(XCog, name="Blacklist"):
    feature = "tickets"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.service = TicketService(bot)

    def _display_name(self, guild_id: int, user_id: int) -> str:
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        return f"{member.display_name} ({user_id})" if member else str(user_id)

    async def render(self, guild_id: int, tr: Translator, page: int = 0) -> discord.ui.LayoutView:
        total_entries = (await self.service.blacklist_list(guild_id, limit=1))[1]
        pages = max(1, math.ceil(total_entries / PAGE_SIZE))
        page = max(0, min(page, pages - 1))
        entries, total = await self.service.blacklist_list(guild_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)

        b = CV2(self.bot.theme)
        b.banner("tickets")
        b.heading(tr("tickets.blacklist.title"), emoji="blacklist", subline=tr("tickets.blacklist.count", count=total))
        b.quote(tr("tickets.blacklist.subtitle"))
        if entries:
            b.bullets(
                [
                    Bullet(
                        f"<@{e.user_id}>",
                        emoji="blacklist",
                        separator="",
                        sub=[
                            tr("tickets.blacklist.reason", reason=truncate(e.reason, 200)),
                            tr("tickets.blacklist.until",
                               until=ts(e.expires_at, "f") if e.expires_at else tr("tickets.blacklist.permanent")),
                            tr("tickets.blacklist.by", user=f"<@{e.created_by}>" if e.created_by else "—"),
                        ],
                    )
                    for e in entries
                ]
            )
        else:
            b.text(tr("tickets.blacklist.empty"))
        b.footer(tr("common.page", page=page + 1, pages=pages))
        b.separator()
        if entries:
            b.actions(
                string_select(
                    cid("bl.remove", page),
                    [select_option(self.bot.theme, self._display_name(guild_id, e.user_id), str(e.user_id),
                                   description=truncate(e.reason, 90), emoji="unlock") for e in entries],
                    placeholder=tr("tickets.blacklist.remove_placeholder"),
                )
            )
        b.actions(
            button(self.bot.theme, tr("tickets.blacklist.add"), emoji="add", custom_id=cid("bl.add", page),
                   style=discord.ButtonStyle.danger),
            button(self.bot.theme, tr("common.previous"), emoji="back", custom_id=cid("bl.page", page - 1),
                   disabled=page <= 0),
            button(self.bot.theme, tr("common.next"), emoji="next", custom_id=cid("bl.page", page + 1),
                   disabled=page >= pages - 1),
            button(self.bot.theme, tr("common.refresh"), emoji="refresh", custom_id=cid("bl.page", page)),
        )
        return b.build()

    @app_commands.command(name="blacklistpanel", description=T("blacklistpanel"))
    @app_commands.guild_only()
    @require_staff()
    async def blacklistpanel(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        await interaction.response.send_message(
            view=await self.render(interaction.guild_id, self.tr(interaction)), ephemeral=True
        )

    @component("bl.page")
    async def on_page(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_staff(self.bot, interaction.user)
        assert interaction.guild_id is not None
        await interaction.response.edit_message(
            view=await self.render(interaction.guild_id, self.tr(interaction), ctx.int_arg(0))
        )

    @component("bl.add")
    async def on_add(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_staff(self.bot, interaction.user)
        tr = self.tr(interaction)
        page = ctx.int_arg(0)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            assert modal_interaction.guild_id is not None
            users = values.get("user") or []
            if not users:
                raise UserError("errors.invalid_input")
            user = users[0]
            duration_raw = str(values.get("duration") or "").strip()
            expires_at = None
            if duration_raw:
                delta = parse_duration(duration_raw)
                if delta is None:
                    raise UserError("errors.invalid_duration")
                expires_at = utcnow() + delta
            reason = str(values.get("reason") or "").strip()
            await self.service.blacklist_add(modal_interaction.guild_id, user.id, reason, modal_interaction.user.id,
                                             expires_at)
            await modal_interaction.response.edit_message(
                view=await self.render(modal_interaction.guild_id, tr, page)
            )
            await modal_interaction.followup.send(
                view=self.bot.notice(kind="success", title=tr("tickets.blacklist.added", user=user.mention)),
                ephemeral=True,
            )
            await self.bot.audit.log(
                modal_interaction.guild, "tickets", "blacklist.add", title=tr("tickets.log.blacklist_add"),
                emoji="blacklist", actor=modal_interaction.user, target_id=user.id,
                lines=[
                    tr("tickets.log.owner", user=f"{user.mention} ({user.id})"),
                    tr("tickets.log.reason", reason=reason),
                    tr("tickets.blacklist.until",
                       until=ts(expires_at, "f") if expires_at else tr("tickets.blacklist.permanent")),
                ],
                details={"reason": reason, "expires_at": expires_at},
            )

        await interaction.response.send_modal(
            FormModal(
                self.bot,
                title=tr("tickets.blacklist.add"),
                fields=[
                    EntityField(key="user", label=tr("tickets.blacklist.field_user"), kind="user"),
                    TextField(key="reason", label=tr("tickets.blacklist.field_reason"), paragraph=True,
                              max_length=500),
                    TextField(key="duration", label=tr("tickets.blacklist.field_duration"),
                              description=tr("tickets.blacklist.field_duration_hint"), required=False,
                              max_length=20, placeholder="7d"),
                ],
                on_submit=submitted,
            )
        )

    @component("bl.remove")
    async def on_remove(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_staff(self.bot, interaction.user)
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        if not ctx.values:
            return
        user_id = int(ctx.values[0])
        if not await self.service.blacklist_remove(interaction.guild_id, user_id, interaction.user.id):
            raise NotFound()
        await interaction.response.edit_message(
            view=await self.render(interaction.guild_id, tr, ctx.int_arg(0))
        )
        await interaction.followup.send(
            view=self.bot.notice(kind="success", title=tr("tickets.blacklist.removed", user=f"<@{user_id}>")),
            ephemeral=True,
        )
        await self.bot.audit.log(
            interaction.guild, "tickets", "blacklist.remove", title=tr("tickets.log.blacklist_remove"),
            emoji="unlock", actor=interaction.user, target_id=user_id, lines=[f"<@{user_id}> ({user_id})"],
        )
