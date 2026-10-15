"""Server event logging: commands, button clicks, modal submits, members, roles, messages."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ...core.cog import XCog
from ...core.utils import truncate, ts
from ...ui.builder import CV2


def _walk_modal_components(components: list[dict[str, Any]]) -> Iterator[tuple[str, str]]:
    for comp in components:
        if "components" in comp:
            yield from _walk_modal_components(comp["components"])
        if "component" in comp:
            yield from _walk_modal_components([comp["component"]])
        custom_id = comp.get("custom_id")
        if custom_id is None:
            continue
        if "value" in comp:
            yield custom_id, str(comp["value"])
        elif "values" in comp:
            yield custom_id, ", ".join(str(v) for v in comp["values"])


class LoggingCog(XCog, name="Logging"):
    feature = "logging"

    def _ignored(self, channel_id: int | None) -> bool:
        return channel_id is not None and channel_id in self.bot.config.logging.ignored_channel_ids

    # ------------------------------------------------------------------ interactions
    @commands.Cog.listener()
    async def on_app_command_completion(
        self, interaction: discord.Interaction[Any], command: app_commands.Command[Any, ..., Any] | app_commands.ContextMenu
    ) -> None:
        if not self.bot.config.logging.log_commands or interaction.guild is None:
            return
        tr = self.gtr(interaction.guild_id)
        options = []
        for opt in (interaction.data or {}).get("options", []) or []:
            options.extend(self._flatten_options(opt))
        lines = [
            tr("logs.command.line", command=f"/{command.qualified_name}", channel=f"<#{interaction.channel_id}>"),
        ]
        if options:
            lines.append(tr("logs.command.options", options=truncate(", ".join(options), 900)))
        await self.bot.audit.log(
            interaction.guild,
            "commands",
            "command.used",
            title=tr("logs.command.title"),
            emoji="settings",
            lines=lines,
            actor=interaction.user,
            channel_id=interaction.channel_id,
            details={"command": command.qualified_name, "options": options},
        )

    @staticmethod
    def _flatten_options(opt: dict[str, Any]) -> list[str]:
        if "options" in opt:
            out: list[str] = []
            for sub in opt["options"]:
                out.extend(LoggingCog._flatten_options(sub))
            return out
        return [f"{opt.get('name')}={opt.get('value')}"]

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction[Any]) -> None:
        if interaction.guild is None or self._ignored(interaction.channel_id):
            return
        cfg = self.bot.config.logging
        data: dict[str, Any] = interaction.data or {}  # type: ignore[assignment]
        tr = self.gtr(interaction.guild_id)
        if interaction.type is discord.InteractionType.component and cfg.log_buttons:
            custom_id = str(data.get("custom_id", ""))
            values = data.get("values")
            lines = [tr("logs.component.line", custom_id=f"`{custom_id}`", channel=f"<#{interaction.channel_id}>")]
            if values:
                lines.append(tr("logs.component.values", values=truncate(", ".join(map(str, values)), 900)))
            await self.bot.audit.log(
                interaction.guild,
                "buttons",
                "component.used",
                title=tr("logs.component.title"),
                emoji="button",
                lines=lines,
                actor=interaction.user,
                channel_id=interaction.channel_id,
                details={"custom_id": custom_id, "values": values or []},
            )
        elif interaction.type is discord.InteractionType.modal_submit and cfg.log_modals:
            fields = list(_walk_modal_components(data.get("components", [])))
            lines = [tr("logs.modal.line", custom_id=f"`{data.get('custom_id', '')}`")]
            lines.extend(f"> **{key}:** {truncate(value, 200)}" for key, value in fields[:5])
            await self.bot.audit.log(
                interaction.guild,
                "modals",
                "modal.submitted",
                title=tr("logs.modal.title"),
                emoji="edit",
                lines=lines,
                actor=interaction.user,
                channel_id=interaction.channel_id,
                details={"custom_id": data.get("custom_id"), "fields": dict(fields)},
            )

    # ------------------------------------------------------------------ members
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self.bot.config.logging.log_member_events:
            return
        tr = self.gtr(member.guild.id)
        await self.bot.audit.log(
            member.guild,
            "members",
            "member.join",
            title=tr("logs.member.join_title"),
            emoji="welcome",
            lines=[
                tr("logs.member.user", user=f"{member.mention} ({member})"),
                tr("logs.member.created", created=ts(member.created_at, "R")),
                tr("logs.member.count", count=member.guild.member_count or 0),
            ],
            target_id=member.id,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not self.bot.config.logging.log_member_events:
            return
        tr = self.gtr(member.guild.id)
        roles = [r.mention for r in member.roles if not r.is_default()]
        lines = [tr("logs.member.user", user=f"{member.mention} ({member})")]
        if member.joined_at:
            lines.append(tr("logs.member.joined", joined=ts(member.joined_at, "R")))
        if roles:
            lines.append(tr("logs.member.roles", roles=truncate(" ".join(roles), 900)))
        await self.bot.audit.log(
            member.guild, "members", "member.leave", title=tr("logs.member.leave_title"), emoji="remove",
            lines=lines, target_id=member.id,
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        cfg = self.bot.config.logging
        tr = self.gtr(after.guild.id)
        if cfg.log_role_events and before.roles != after.roles:
            added = [r.mention for r in after.roles if r not in before.roles]
            removed = [r.mention for r in before.roles if r not in after.roles]
            lines = [tr("logs.member.user", user=f"{after.mention} ({after})")]
            if added:
                lines.append(tr("logs.roles.added", roles=" ".join(added)))
            if removed:
                lines.append(tr("logs.roles.removed", roles=" ".join(removed)))
            await self.bot.audit.log(
                after.guild, "roles", "member.roles", title=tr("logs.roles.member_title"), emoji="role",
                lines=lines, target_id=after.id, details={"added": len(added), "removed": len(removed)},
            )
        if cfg.log_member_events and before.nick != after.nick:
            await self.bot.audit.log(
                after.guild,
                "members",
                "member.nick",
                title=tr("logs.member.nick_title"),
                emoji="edit",
                lines=[
                    tr("logs.member.user", user=f"{after.mention} ({after})"),
                    tr("logs.member.nick_change", before=before.nick or "—", after=after.nick or "—"),
                ],
                target_id=after.id,
            )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member) -> None:
        tr = self.gtr(guild.id)
        await self.bot.audit.log(
            guild, "moderation", "member.ban", title=tr("logs.moderation.ban_title"), emoji="shield",
            lines=[tr("logs.member.user", user=f"{user.mention} ({user})")], target_id=user.id,
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        tr = self.gtr(guild.id)
        await self.bot.audit.log(
            guild, "moderation", "member.unban", title=tr("logs.moderation.unban_title"), emoji="shield",
            lines=[tr("logs.member.user", user=f"{user.mention} ({user})")], target_id=user.id,
        )

    # ------------------------------------------------------------------ messages
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if (
            not self.bot.config.logging.log_message_events
            or message.guild is None
            or message.author.bot
            or self._ignored(message.channel.id)
        ):
            return
        tr = self.gtr(message.guild.id)
        lines = [
            tr("logs.message.author", user=f"{message.author.mention} ({message.author})"),
            tr("logs.message.channel", channel=message.channel.mention),  # type: ignore[union-attr]
        ]
        if message.content:
            lines.append(CV2.quote_lines(truncate(message.content, 1500)))
        if message.attachments:
            lines.append(tr("logs.message.attachments", count=len(message.attachments)))
        await self.bot.audit.log(
            message.guild, "messages", "message.delete", title=tr("logs.message.deleted_title"), emoji="delete",
            lines=lines, target_id=message.author.id, channel_id=message.channel.id,
            details={"content": message.content[:1500]},
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if (
            not self.bot.config.logging.log_message_events
            or after.guild is None
            or after.author.bot
            or before.content == after.content
            or self._ignored(after.channel.id)
        ):
            return
        tr = self.gtr(after.guild.id)
        await self.bot.audit.log(
            after.guild,
            "messages",
            "message.edit",
            title=tr("logs.message.edited_title"),
            emoji="edit",
            lines=[
                tr("logs.message.author", user=f"{after.author.mention} ({after.author})"),
                tr("logs.message.jump", url=after.jump_url),
                f"**{tr('logs.message.before')}**",
                CV2.quote_lines(truncate(before.content or "—", 900)),
                f"**{tr('logs.message.after')}**",
                CV2.quote_lines(truncate(after.content or "—", 900)),
            ],
            target_id=after.author.id,
            channel_id=after.channel.id,
        )

    # ------------------------------------------------------------------ roles / channels
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        if self.bot.config.logging.log_role_events:
            tr = self.gtr(role.guild.id)
            await self.bot.audit.log(role.guild, "roles", "role.create", title=tr("logs.roles.created_title"),
                                     emoji="add", lines=[f"{role.mention} (`{role.id}`)"], target_id=role.id)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if self.bot.config.logging.log_role_events:
            tr = self.gtr(role.guild.id)
            await self.bot.audit.log(role.guild, "roles", "role.delete", title=tr("logs.roles.deleted_title"),
                                     emoji="remove", lines=[f"**{role.name}** (`{role.id}`)"], target_id=role.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        tr = self.gtr(channel.guild.id)
        await self.bot.audit.log(channel.guild, "server", "channel.create", title=tr("logs.channel.created_title"),
                                 emoji="add", lines=[f"{channel.mention} (`{channel.id}`)"], channel_id=channel.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        tr = self.gtr(channel.guild.id)
        await self.bot.audit.log(channel.guild, "server", "channel.delete", title=tr("logs.channel.deleted_title"),
                                 emoji="remove", lines=[f"**#{channel.name}** (`{channel.id}`)"], channel_id=channel.id)

