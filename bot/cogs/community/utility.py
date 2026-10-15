"""Utility features: member counter, anti ghost ping, reminders, cross-server role sync."""

from __future__ import annotations

import datetime as dt
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import NotFound, UserError
from ...core.logger import get_logger
from ...core.permissions import require_admin
from ...core.tasks import resilient_iteration
from ...core.utils import parse_date, parse_duration, truncate, ts, utcnow
from ...ui.builder import CV2, Bullet

_log = get_logger("community")
MAX_REMINDER_DAYS = 366
MAX_REMINDERS_PER_USER = 25


class UtilityCog(XCog, name="CommunityUtility"):
    feature = "community"

    remind_group = app_commands.Group(name="reminders", description=T("reminders"), guild_only=True)
    sync_group = app_commands.Group(name="rolesync", description=T("rolesync"), guild_only=True,
                                    default_permissions=discord.Permissions(administrator=True))

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.community

    async def on_load(self) -> None:
        self.counter_loop.change_interval(minutes=max(5, self.cfg.member_counter.update_interval_minutes))
        self.counter_loop.start()
        self.reminder_loop.start()

    async def on_unload(self) -> None:
        self.counter_loop.cancel()
        self.reminder_loop.cancel()

    # ================================================================== member counter
    @tasks.loop(minutes=10)
    @resilient_iteration("member_counter")
    async def counter_loop(self) -> None:
        cfg = self.cfg.member_counter
        if not cfg.enabled or not cfg.channel_id:
            return
        for guild in self.bot.guilds:
            channel = guild.get_channel(cfg.channel_id)
            if channel is None or not self.bot.features.is_enabled(guild.id, "community"):
                continue
            count = (guild.member_count or 0) if cfg.count_bots else sum(1 for m in guild.members if not m.bot)
            name = cfg.name_format.format(count=count)[:100]
            if channel.name != name:
                try:
                    await channel.edit(name=name, reason="Member counter")
                except discord.HTTPException:
                    _log.info("Member counter rename skipped (rate limit/permissions)")

    @counter_loop.before_loop
    async def _before_counter(self) -> None:
        await self.bot.wait_until_ready()

    # ================================================================== anti ghost ping
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        cfg = self.cfg.ghost_ping
        if (
            not cfg.enabled
            or message.guild is None
            or message.author.bot
            or not (message.mentions or message.role_mentions or message.mention_everyone)
            or (utcnow() - message.created_at).total_seconds() > cfg.max_age_seconds
            or not self.bot.features.is_enabled(message.guild.id, "community")
        ):
            return
        targets = [m.mention for m in message.mentions if not m.bot and m.id != message.author.id]
        targets += [r.mention for r in message.role_mentions]
        if message.mention_everyone:
            targets.append("@everyone")
        if not targets:
            return
        tr = self.gtr(message.guild.id)
        b = CV2(self.bot.theme)
        b.heading(tr("community.ghost.title"), emoji="ghost", level=3)
        b.bullets([
            Bullet(tr("community.ghost.author"), message.author.mention, emoji="user"),
            Bullet(tr("community.ghost.mentioned"), " ".join(targets[:20]), emoji="bell"),
        ])
        if message.content:
            b.quote(truncate(message.content, 1500))
        b.footer(tr("community.ghost.footer", when=ts(message.created_at, "R")))
        channel = message.guild.get_channel(cfg.channel_id or 0) or message.channel
        try:
            await channel.send(view=b.build(), allowed_mentions=discord.AllowedMentions.none())  # type: ignore[union-attr]
        except discord.HTTPException:
            _log.debug("Could not report ghost ping")

    # ================================================================== reminders
    @app_commands.command(name="remind", description=T("remind"))
    @app_commands.describe(when=T("remind.when"), text=T("remind.text"), dm=T("remind.dm"))
    @app_commands.guild_only()
    async def remind(self, interaction: discord.Interaction[Any], when: str, text: app_commands.Range[str, 1, 1000],
                     dm: bool = True) -> None:
        assert interaction.guild_id is not None
        delta = parse_duration(when)
        if delta is not None:
            remind_at = utcnow() + delta
        else:
            day = parse_date(when)
            if day is None:
                raise UserError("errors.invalid_duration")
            remind_at = dt.datetime.combine(day, dt.time(9, 0), tzinfo=dt.UTC)
        if remind_at <= utcnow() or remind_at > utcnow() + dt.timedelta(days=MAX_REMINDER_DAYS):
            raise UserError("community.remind.range", days=MAX_REMINDER_DAYS)
        pending = await self.bot.db.fetchval(
            "SELECT count(*) FROM reminders WHERE guild_id = $1 AND user_id = $2 AND delivered_at IS NULL",
            interaction.guild_id, interaction.user.id,
        )
        if pending >= MAX_REMINDERS_PER_USER:
            raise UserError("community.remind.limit", max=MAX_REMINDERS_PER_USER)
        await self.bot.db.ensure_guild(interaction.guild_id)
        reminder_id = await self.bot.db.fetchval(
            "INSERT INTO reminders (guild_id, user_id, channel_id, text, dm, locale, remind_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
            interaction.guild_id, interaction.user.id, interaction.channel_id, text, dm,
            self.bot.locale_for(interaction), remind_at,
        )
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("community.remind.created", id=reminder_id,
                                                            when=ts(remind_at, "F")))

    @remind_group.command(name="list", description=T("reminders.list"))
    async def reminders_list(self, interaction: discord.Interaction[Any]) -> None:
        tr = self.tr(interaction)
        rows = await self.bot.db.fetch(
            "SELECT id, text, remind_at FROM reminders WHERE guild_id = $1 AND user_id = $2 AND delivered_at IS NULL "
            "ORDER BY remind_at",
            interaction.guild_id, interaction.user.id,
        )
        b = CV2(self.bot.theme)
        b.heading(tr("community.remind.list_title"), emoji="reminder")
        if rows:
            b.bullets([Bullet(f"#{r['id']} · {ts(r['remind_at'], 'R')}", emoji="reminder", separator="",
                              sub=[truncate(r["text"], 150)]) for r in rows])
        else:
            b.quote(tr("community.remind.none"))
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    @remind_group.command(name="delete", description=T("reminders.delete"))
    @app_commands.describe(reminder=T("reminders.reminder"))
    async def reminders_delete(self, interaction: discord.Interaction[Any], reminder: int) -> None:
        status = await self.bot.db.execute(
            "DELETE FROM reminders WHERE id = $1 AND guild_id = $2 AND user_id = $3 AND delivered_at IS NULL",
            reminder, interaction.guild_id, interaction.user.id,
        )
        if not status.endswith(" 1"):
            raise NotFound()
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("community.remind.deleted", id=reminder))

    @tasks.loop(seconds=30)
    @resilient_iteration("reminders")
    async def reminder_loop(self) -> None:
        rows = await self.bot.db.fetch(
            "SELECT * FROM reminders WHERE delivered_at IS NULL AND remind_at <= now() ORDER BY remind_at LIMIT 50"
        )
        for row in rows:
            await self.bot.db.execute("UPDATE reminders SET delivered_at = now() WHERE id = $1", row["id"])
            tr = self.bot.i18n.translator(row["locale"])
            b = CV2(self.bot.theme)
            b.heading(tr("community.remind.title"), emoji="reminder", level=3)
            b.quote(row["text"])
            b.footer(tr("community.remind.footer", when=ts(row["created_at"], "R")))
            user = self.bot.get_user(row["user_id"])
            delivered = False
            if row["dm"] and user is not None:
                delivered = await self.bot.safe_dm(user, b.build())
            if not delivered:
                channel = self.bot.get_channel(row["channel_id"] or 0)
                if isinstance(channel, (discord.TextChannel, discord.Thread)):
                    view = (CV2(self.bot.theme).mention(f"<@{row['user_id']}>")
                            .heading(tr("community.remind.title"), emoji="reminder", level=3)
                            .quote(row["text"]).build())
                    try:
                        await channel.send(view=view, allowed_mentions=discord.AllowedMentions(users=True))
                    except discord.HTTPException:
                        _log.info("Could not deliver reminder %s", row["id"])

    @reminder_loop.before_loop
    async def _before_reminders(self) -> None:
        await self.bot.wait_until_ready()

    # ================================================================== role sync
    async def pairs(self) -> list[tuple[int, int, int, int]]:
        """``(source_guild, source_role, target_guild, target_role)`` from config + DB."""
        result = [(p.source_guild_id, p.source_role_id, p.target_guild_id, p.target_role_id) for p in self.cfg.role_sync]
        rows = await self.bot.db.fetch(
            "SELECT source_guild_id, source_role_id, target_guild_id, target_role_id FROM role_sync")
        result.extend((r[0], r[1], r[2], r[3]) for r in rows)
        return list(dict.fromkeys(result))

    async def _apply(self, user_id: int, target_guild_id: int, target_role_id: int, give: bool) -> None:
        guild = self.bot.get_guild(target_guild_id)
        member = guild.get_member(user_id) if guild else None
        role = guild.get_role(target_role_id) if guild else None
        if member is None or role is None or (role in member.roles) == give:
            return
        try:
            if give:
                await member.add_roles(role, reason="Role sync")
            else:
                await member.remove_roles(role, reason="Role sync")
        except discord.HTTPException:
            _log.warning("Role sync failed for %s in %s", user_id, target_guild_id)

    @commands.Cog.listener("on_member_update")
    async def _sync_on_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles:
            return
        added = {r.id for r in after.roles} - {r.id for r in before.roles}
        removed = {r.id for r in before.roles} - {r.id for r in after.roles}
        for source_guild, source_role, target_guild, target_role in await self.pairs():
            if source_guild != after.guild.id:
                continue
            if source_role in added:
                await self._apply(after.id, target_guild, target_role, True)
            elif source_role in removed:
                await self._apply(after.id, target_guild, target_role, False)

    @commands.Cog.listener("on_member_join")
    async def _sync_on_join(self, member: discord.Member) -> None:
        for source_guild, source_role, target_guild, target_role in await self.pairs():
            if target_guild != member.guild.id:
                continue
            source = self.bot.get_guild(source_guild)
            source_member = source.get_member(member.id) if source else None
            if source_member is not None and any(r.id == source_role for r in source_member.roles):
                await self._apply(member.id, target_guild, target_role, True)

    @sync_group.command(name="add", description=T("rolesync.add"))
    @app_commands.describe(source_role=T("rolesync.source_role"), target_guild_id=T("rolesync.target_guild"),
                           target_role_id=T("rolesync.target_role"))
    @require_admin()
    async def sync_add(self, interaction: discord.Interaction[Any], source_role: discord.Role, target_guild_id: str,
                       target_role_id: str) -> None:
        assert interaction.guild_id is not None
        if not target_guild_id.isdigit() or not target_role_id.isdigit():
            raise UserError("errors.invalid_input")
        target = self.bot.get_guild(int(target_guild_id))
        if target is None or target.get_role(int(target_role_id)) is None:
            raise UserError("community.sync.target_missing")
        await self.bot.db.ensure_guild(interaction.guild_id)
        await self.bot.db.execute(
            "INSERT INTO role_sync (source_guild_id, source_role_id, target_guild_id, target_role_id, created_by) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
            interaction.guild_id, source_role.id, int(target_guild_id), int(target_role_id), interaction.user.id,
        )
        await self.bot.respond_notice(interaction, title=self.tr(interaction)(
            "community.sync.added", role=source_role.mention, guild=target.name))

    @sync_group.command(name="list", description=T("rolesync.list"))
    @require_admin()
    async def sync_list(self, interaction: discord.Interaction[Any]) -> None:
        tr = self.tr(interaction)
        rows = await self.bot.db.fetch("SELECT * FROM role_sync WHERE source_guild_id = $1 ORDER BY id",
                                       interaction.guild_id)
        b = CV2(self.bot.theme)
        b.heading(tr("community.sync.title"), emoji="sync")
        lines = [f"`#{r['id']}` <@&{r['source_role_id']}> {self.bot.icons['arrow']} "
                 f"{getattr(self.bot.get_guild(r['target_guild_id']), 'name', r['target_guild_id'])} / "
                 f"`{r['target_role_id']}`" for r in rows]
        lines += [f"`config` <@&{p.source_role_id}> {self.bot.icons['arrow']} `{p.target_guild_id}` / `{p.target_role_id}`"
                  for p in self.cfg.role_sync if p.source_guild_id == interaction.guild_id]
        b.text("\n".join(lines) if lines else tr("community.sync.none"))
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    @sync_group.command(name="remove", description=T("rolesync.remove"))
    @app_commands.describe(sync_id=T("rolesync.sync_id"))
    @require_admin()
    async def sync_remove(self, interaction: discord.Interaction[Any], sync_id: int) -> None:
        status = await self.bot.db.execute("DELETE FROM role_sync WHERE id = $1 AND source_guild_id = $2",
                                           sync_id, interaction.guild_id)
        if not status.endswith(" 1"):
            raise NotFound()
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("community.sync.removed", id=sync_id))

    @sync_group.command(name="run", description=T("rolesync.run"))
    @require_admin()
    async def sync_run(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        changed = 0
        for source_guild, source_role, target_guild, target_role in await self.pairs():
            if source_guild != interaction.guild.id:
                continue
            role = interaction.guild.get_role(source_role)
            target = self.bot.get_guild(target_guild)
            target_role_obj = target.get_role(target_role) if target else None
            if role is None or target is None or target_role_obj is None:
                continue
            holders = {m.id for m in role.members}
            for member in target.members:
                should_have = member.id in holders
                if should_have != (target_role_obj in member.roles):
                    await self._apply(member.id, target_guild, target_role, should_have)
                    changed += 1
        await interaction.followup.send(view=self.bot.notice(kind="success", title=self.tr(interaction)(
            "community.sync.done", count=changed)), ephemeral=True)
