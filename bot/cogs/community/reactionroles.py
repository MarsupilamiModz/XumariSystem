"""Reaction roles as Components V2 panels (buttons or a multi-select)."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import NotFound, UserError
from ...core.logger import get_logger
from ...core.permissions import require_admin
from ...core.router import ComponentContext, cid, component
from ...core.utils import truncate
from ...ui.builder import CV2, Bullet, button, select_option, string_select

_log = get_logger("reactionroles")
MAX_BUTTON_ROLES = 20
MAX_SELECT_ROLES = 25


class ReactionRolesCog(XCog, name="ReactionRoles"):
    feature = "community"

    group = app_commands.Group(name="reactionroles", description=T("reactionroles"), guild_only=True,
                               default_permissions=discord.Permissions(manage_roles=True))

    async def _panel(self, panel_id: int, guild_id: int) -> Any:
        row = await self.bot.db.fetchrow("SELECT * FROM reaction_role_panels WHERE id = $1 AND guild_id = $2",
                                         panel_id, guild_id)
        if row is None:
            raise NotFound("community.rr.not_found")
        return row

    async def _roles(self, panel_id: int) -> list[Any]:
        return list(await self.bot.db.fetch("SELECT * FROM reaction_roles WHERE panel_id = $1 ORDER BY position, id",
                                            panel_id))

    async def render(self, panel: Any) -> discord.ui.LayoutView:
        tr = self.gtr(panel["guild_id"])
        roles = await self._roles(panel["id"])
        b = CV2(self.bot.theme)
        b.banner("roles")
        b.heading(panel["title"], emoji="role")
        b.quote(panel["description"])
        if roles:
            b.bullets([Bullet(f"<@&{r['role_id']}>", emoji=r["emoji"] or "role", separator="",
                              sub=[r["description"]] if r["description"] else ()) for r in roles])
        else:
            b.text(tr("community.rr.empty"))
        b.footer(tr("community.rr.footer"))
        if roles:
            b.separator()
            if panel["mode"] == "select":
                options = [select_option(self.bot.theme, r["label"], str(r["role_id"]), description=r["description"],
                                         emoji=r["emoji"] or "role") for r in roles[:MAX_SELECT_ROLES]]
                b.actions(string_select(cid("rr.select", panel["id"]), options,
                                        placeholder=tr("community.rr.placeholder"), min_values=0,
                                        max_values=len(options)))
            else:
                buttons = [button(self.bot.theme, r["label"], emoji=r["emoji"] or "role",
                                  custom_id=cid("rr.toggle", panel["id"], r["role_id"])) for r in roles[:MAX_BUTTON_ROLES]]
                for start in range(0, len(buttons), 5):
                    b.actions(*buttons[start:start + 5])
        return b.build()

    async def refresh(self, panel: Any) -> None:
        if not panel["channel_id"] or not panel["message_id"]:
            return
        channel = self.bot.get_channel(panel["channel_id"])
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await channel.get_partial_message(panel["message_id"]).edit(view=await self.render(panel))
            except discord.HTTPException:
                _log.debug("Could not refresh reaction role panel %s", panel["id"])

    async def _panel_choices(self, interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[int]]:
        rows = await self.bot.db.fetch("SELECT id, title FROM reaction_role_panels WHERE guild_id = $1 ORDER BY id DESC",
                                       interaction.guild_id)
        return [app_commands.Choice(name=truncate(f"#{r['id']} {r['title']}", 100), value=r["id"])
                for r in rows if current.lower() in r["title"].lower()][:25]

    @group.command(name="create", description=T("reactionroles.create"))
    @app_commands.describe(title=T("reactionroles.title"), description=T("reactionroles.description"),
                           mode=T("reactionroles.mode"), channel=T("shop.channel"))
    @app_commands.choices(mode=[app_commands.Choice(name=T("reactionroles.mode.buttons"), value="buttons"),
                                app_commands.Choice(name=T("reactionroles.mode.select"), value="select")])
    @require_admin()
    async def create(self, interaction: discord.Interaction[Any], title: app_commands.Range[str, 1, 100],
                     mode: str = "buttons", description: str | None = None,
                     channel: discord.TextChannel | None = None) -> None:
        assert interaction.guild_id is not None
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise UserError("errors.invalid_input")
        await self.bot.db.ensure_guild(interaction.guild_id)
        panel = await self.bot.db.fetchrow(
            "INSERT INTO reaction_role_panels (guild_id, channel_id, title, description, mode, created_by) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            interaction.guild_id, target.id, title, description, mode, interaction.user.id,
        )
        message = await target.send(view=await self.render(panel))
        await self.bot.db.execute("UPDATE reaction_role_panels SET message_id = $2 WHERE id = $1", panel["id"], message.id)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("community.rr.created", id=panel["id"]),
                                      description=tr("community.rr.hint"))

    @group.command(name="add", description=T("reactionroles.add"))
    @app_commands.describe(panel=T("reactionroles.panel"), role=T("levels.reward.role"),
                           label=T("reactionroles.label"), emoji=T("product.emoji"),
                           description=T("reactionroles.role_description"))
    @require_admin()
    async def add(self, interaction: discord.Interaction[Any], panel: int, role: discord.Role,
                  label: app_commands.Range[str, 1, 80] | None = None, emoji: str | None = None,
                  description: app_commands.Range[str, 1, 100] | None = None) -> None:
        assert interaction.guild is not None
        row = await self._panel(panel, interaction.guild.id)
        if role >= interaction.guild.me.top_role or role.managed:
            raise UserError("community.rr.role_too_high")
        limit = MAX_SELECT_ROLES if row["mode"] == "select" else MAX_BUTTON_ROLES
        if len(await self._roles(panel)) >= limit:
            raise UserError("community.rr.full", max=limit)
        await self.bot.db.execute(
            """
            INSERT INTO reaction_roles (panel_id, guild_id, role_id, label, emoji, description, position)
            VALUES ($1, $2, $3, $4, $5, $6, (SELECT COALESCE(max(position) + 1, 0) FROM reaction_roles WHERE panel_id = $1))
            ON CONFLICT (panel_id, role_id) DO UPDATE SET label = EXCLUDED.label, emoji = EXCLUDED.emoji,
                                                          description = EXCLUDED.description
            """,
            panel, interaction.guild.id, role.id, label or role.name, emoji, description,
        )
        await self.refresh(row)
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("community.rr.added", role=role.mention))

    add.autocomplete("panel")(_panel_choices)

    @group.command(name="remove", description=T("reactionroles.remove"))
    @app_commands.describe(panel=T("reactionroles.panel"), role=T("levels.reward.role"))
    @require_admin()
    async def remove(self, interaction: discord.Interaction[Any], panel: int, role: discord.Role) -> None:
        assert interaction.guild_id is not None
        row = await self._panel(panel, interaction.guild_id)
        await self.bot.db.execute("DELETE FROM reaction_roles WHERE panel_id = $1 AND role_id = $2", panel, role.id)
        await self.refresh(row)
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("community.rr.removed", role=role.mention))

    remove.autocomplete("panel")(_panel_choices)

    @group.command(name="delete", description=T("reactionroles.delete"))
    @app_commands.describe(panel=T("reactionroles.panel"))
    @require_admin()
    async def delete(self, interaction: discord.Interaction[Any], panel: int) -> None:
        assert interaction.guild_id is not None
        row = await self._panel(panel, interaction.guild_id)
        channel = self.bot.get_channel(row["channel_id"] or 0)
        if isinstance(channel, discord.TextChannel) and row["message_id"]:
            try:
                await channel.get_partial_message(row["message_id"]).delete()
            except discord.HTTPException:
                _log.debug("Panel message %s already gone", row["message_id"])
        await self.bot.db.execute("DELETE FROM reaction_role_panels WHERE id = $1", panel)
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("community.rr.deleted", id=panel))

    delete.autocomplete("panel")(_panel_choices)

    # ------------------------------------------------------------------ components
    @component("rr.toggle")
    async def on_toggle(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member) and interaction.guild is not None
        panel_id, role_id = ctx.int_arg(0), ctx.int_arg(1)
        if not await self.bot.db.fetchval("SELECT 1 FROM reaction_roles WHERE panel_id = $1 AND role_id = $2",
                                          panel_id, role_id):
            raise NotFound("community.rr.not_found")
        role = interaction.guild.get_role(role_id)
        if role is None:
            raise NotFound("community.rr.role_missing")
        tr = self.tr(interaction)
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Reaction role")
                title = tr("community.rr.role_removed", role=role.mention)
            else:
                await member.add_roles(role, reason="Reaction role")
                title = tr("community.rr.role_added", role=role.mention)
        except discord.HTTPException as exc:
            raise UserError("errors.bot_missing_permissions") from exc
        await self.bot.respond_notice(interaction, title=title)

    @component("rr.select")
    async def on_select(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member) and interaction.guild is not None
        panel_role_ids = {r["role_id"] for r in await self._roles(ctx.int_arg(0))}
        selected = {int(v) for v in ctx.values if v.isdigit()} & panel_role_ids
        add = [r for r in (interaction.guild.get_role(i) for i in selected) if r and r not in member.roles]
        remove = [r for r in member.roles if r.id in panel_role_ids and r.id not in selected]
        try:
            if add:
                await member.add_roles(*add, reason="Reaction role select")
            if remove:
                await member.remove_roles(*remove, reason="Reaction role select")
        except discord.HTTPException as exc:
            raise UserError("errors.bot_missing_permissions") from exc
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("community.rr.updated", added=len(add), removed=len(remove)))
        if interaction.message is not None and not interaction.message.flags.ephemeral:
            panel = await self._panel(ctx.int_arg(0), interaction.guild.id)
            await self.refresh(panel)
