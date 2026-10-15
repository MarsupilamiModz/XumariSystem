"""Levels: XP for messages, level-up announcements, role rewards, rank card and leaderboard."""

from __future__ import annotations

import random
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import NotFound
from ...core.logger import get_logger
from ...core.permissions import require_admin
from ...core.utils import format_number, progress_bar
from ...services.levels import LevelService, LevelUp
from ...ui.builder import CV2, Bullet

_log = get_logger("levels")
_rng = random.SystemRandom()


class LevelsCog(XCog, name="Levels"):
    feature = "levels"

    group = app_commands.Group(name="levels", description=T("levels"), guild_only=True)
    reward_group = app_commands.Group(name="reward", description=T("levels.reward"), parent=group)

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.levels = LevelService(bot.db, bot.config.levels)

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.levels

    # ------------------------------------------------------------------ XP
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            message.guild is None
            or message.author.bot
            or not isinstance(message.author, discord.Member)
            or message.channel.id in self.cfg.blacklisted_channel_ids
            or getattr(message.channel, "category_id", None) in self.cfg.blacklisted_channel_ids
            or not self.bot.features.is_enabled(message.guild.id, "levels")
        ):
            return
        amount = _rng.randint(self.cfg.xp_min, max(self.cfg.xp_min, self.cfg.xp_max))
        level_up = await self.levels.award(message.guild.id, message.author.id, amount)
        if level_up is not None:
            await self._on_level_up(message, message.author, level_up)

    async def _on_level_up(self, message: discord.Message, member: discord.Member, level_up: LevelUp) -> None:
        roles = await self.apply_rewards(member, level_up.new_level)
        if not self.cfg.announce_level_up:
            return
        tr = self.gtr(member.guild.id)
        b = CV2(self.bot.theme)
        b.mention(member.mention)
        b.banner("levels")
        b.heading(tr("levels.up.title"), emoji="level")
        b.quote(tr("levels.up.text", user=member.mention, level=level_up.new_level))
        if roles:
            b.bullets([Bullet(tr("levels.up.rewards"), " ".join(r.mention for r in roles), emoji="role")])
        channel = member.guild.get_channel(self.cfg.level_up_channel_id or 0) or message.channel
        try:
            await channel.send(view=b.build(), allowed_mentions=discord.AllowedMentions(users=[member]))  # type: ignore[union-attr]
        except discord.HTTPException:
            _log.debug("Could not announce level up in %s", getattr(channel, "id", None))

    async def apply_rewards(self, member: discord.Member, level: int) -> list[discord.Role]:
        rewards = await self.levels.rewards(member.guild.id)
        earned = [rid for lvl, ids in rewards.items() if lvl <= level for rid in ids]
        if not self.cfg.stack_rewards and earned:
            highest = max(lvl for lvl in rewards if lvl <= level)
            earned = rewards[highest]
        all_reward_ids = {rid for ids in rewards.values() for rid in ids}
        add = [r for r in (member.guild.get_role(rid) for rid in earned) if r and r not in member.roles]
        remove = [] if self.cfg.stack_rewards else [
            r for r in member.roles if r.id in all_reward_ids and r.id not in earned
        ]
        try:
            if add:
                await member.add_roles(*add, reason=f"Level {level}")
            if remove:
                await member.remove_roles(*remove, reason=f"Level {level}")
        except discord.HTTPException:
            _log.warning("Could not update level roles for %s", member.id)
        return add

    # ------------------------------------------------------------------ commands
    @app_commands.command(name="rank", description=T("rank"))
    @app_commands.describe(user=T("rank.user"))
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction[Any], user: discord.Member | None = None) -> None:
        assert interaction.guild_id is not None
        target = user or interaction.user
        entry = await self.levels.get(interaction.guild_id, target.id)
        level, current, needed = self.levels.progress(entry.xp)
        position = await self.levels.rank(interaction.guild_id, target.id)
        tr = self.tr(interaction)
        icons = self.bot.icons
        b = CV2(self.bot.theme)
        b.banner("levels")
        avatar = target.display_avatar.replace(size=128).url
        b.thumbnail_section(
            f"## {icons['level']} {tr('levels.rank.title', user=target.display_name)}\n"
            f"-# {tr('levels.rank.position', rank=position)}",
            tr("levels.rank.level", level=level),
            url=avatar,
        )
        b.text(f"{progress_bar(current, needed, 12, icons['bar_full'], icons['bar_empty'])} "
               f"`{format_number(current, tr.locale)} / {format_number(needed, tr.locale)} XP`")
        b.bullets([
            Bullet(tr("levels.rank.total"), format_number(entry.xp, tr.locale), emoji="xp"),
            Bullet(tr("levels.rank.messages"), format_number(entry.messages, tr.locale), emoji="message"),
        ])
        await interaction.response.send_message(view=b.build(), ephemeral=user is None)

    @group.command(name="leaderboard", description=T("levels.leaderboard"))
    async def leaderboard(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        entries = await self.levels.leaderboard(interaction.guild_id, self.cfg.leaderboard_size)
        b = CV2(self.bot.theme)
        b.banner("levels")
        b.heading(tr("levels.leaderboard.title"), emoji="trophy")
        if entries:
            medals = ["🥇", "🥈", "🥉"]
            b.text("\n".join(
                f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{e.user_id}> · "
                f"{tr('levels.rank.level', level=e.level)} · {format_number(e.xp, tr.locale)} XP"
                for i, e in enumerate(entries)
            ))
        else:
            b.quote(tr("levels.leaderboard.empty"))
        await interaction.response.send_message(view=b.build(), allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="setxp", description=T("levels.setxp"))
    @app_commands.describe(user=T("rank.user"), xp=T("levels.setxp.xp"))
    @require_admin()
    async def setxp(self, interaction: discord.Interaction[Any], user: discord.Member,
                    xp: app_commands.Range[int, 0, 100_000_000]) -> None:
        assert interaction.guild_id is not None
        entry = await self.levels.set_xp(interaction.guild_id, user.id, xp)
        await self.apply_rewards(user, entry.level)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("levels.admin.set", user=user.mention, level=entry.level,
                                                            xp=format_number(xp, tr.locale)))
        await self.bot.audit.log(interaction.guild, "community", "levels.set", title=tr("levels.log.set"),
                                 emoji="level", actor=interaction.user, target_id=user.id,
                                 lines=[f"{user.mention} → {xp} XP (Level {entry.level})"])

    @group.command(name="reset", description=T("levels.reset"))
    @app_commands.describe(user=T("levels.reset.user"))
    @require_admin()
    async def reset(self, interaction: discord.Interaction[Any], user: discord.Member | None = None) -> None:
        assert interaction.guild_id is not None
        await self.levels.reset(interaction.guild_id, user.id if user else None)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("levels.admin.reset",
                                                            user=user.mention if user else tr("economy.log.everyone")))

    @reward_group.command(name="add", description=T("levels.reward.add"))
    @app_commands.describe(level=T("levels.reward.level"), role=T("levels.reward.role"))
    @require_admin()
    async def reward_add(self, interaction: discord.Interaction[Any], level: app_commands.Range[int, 1, 1000],
                         role: discord.Role) -> None:
        assert interaction.guild_id is not None
        await self.levels.add_reward(interaction.guild_id, level, role.id)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("levels.reward.added", level=level, role=role.mention))

    @reward_group.command(name="remove", description=T("levels.reward.remove"))
    @app_commands.describe(level=T("levels.reward.level"), role=T("levels.reward.role"))
    @require_admin()
    async def reward_remove(self, interaction: discord.Interaction[Any], level: int, role: discord.Role) -> None:
        assert interaction.guild_id is not None
        if not await self.levels.remove_reward(interaction.guild_id, level, role.id):
            raise NotFound()
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("levels.reward.removed", level=level, role=role.mention))

    @reward_group.command(name="list", description=T("levels.reward.list"))
    async def reward_list(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        rewards = await self.levels.rewards(interaction.guild_id)
        b = CV2(self.bot.theme)
        b.heading(tr("levels.reward.title"), emoji="role")
        if rewards:
            b.bullets([Bullet(tr("levels.rank.level", level=lvl), " ".join(f"<@&{rid}>" for rid in ids), emoji="level")
                       for lvl, ids in rewards.items()])
        else:
            b.quote(tr("levels.reward.none"))
        await interaction.response.send_message(view=b.build(), ephemeral=True)


class BoostCog(XCog, name="Boost"):
    """Booster roles and thank-you message."""

    feature = "levels"

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.levels.boost

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if not self.cfg.enabled or not self.bot.features.is_enabled(after.guild.id, "levels"):
            return
        roles = [r for r in (after.guild.get_role(rid) for rid in self.cfg.role_ids) if r]
        if before.premium_since is None and after.premium_since is not None:
            if roles:
                try:
                    await after.add_roles(*roles, reason="Server boost")
                except discord.HTTPException:
                    _log.warning("Could not add boost roles to %s", after.id)
            await self._thank(after)
            tr = self.gtr(after.guild.id)
            await self.bot.audit.log(after.guild, "members", "member.boost", title=tr("levels.boost.log"),
                                     emoji="boost", target_id=after.id, lines=[after.mention])
        elif before.premium_since is not None and after.premium_since is None and roles:
            try:
                await after.remove_roles(*roles, reason="Boost ended")
            except discord.HTTPException:
                _log.warning("Could not remove boost roles from %s", after.id)

    async def _thank(self, member: discord.Member) -> None:
        channel = member.guild.get_channel(self.cfg.channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            return
        tr = self.gtr(member.guild.id)
        b = CV2(self.bot.theme)
        b.mention(member.mention)
        b.banner("welcome")
        b.heading(tr("levels.boost.title"), emoji="boost")
        b.quote(tr("levels.boost.text", user=member.mention, count=member.guild.premium_subscription_count))
        await channel.send(view=b.build(), allowed_mentions=discord.AllowedMentions(users=[member]))
