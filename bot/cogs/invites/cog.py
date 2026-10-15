"""Invite tracking: who invited whom (invite-use diffing), fake/rejoin/left detection, statistics."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.logger import get_logger
from ...core.utils import ts, utcnow
from ...ui.builder import CV2, Bullet

_log = get_logger("invites")
VANITY = "vanity"


@dataclass(slots=True)
class CachedInvite:
    uses: int
    max_uses: int
    inviter_id: int | None


@dataclass(slots=True)
class InviteStats:
    total: int
    left: int
    fake: int
    rejoins: int

    @property
    def effective(self) -> int:
        return max(0, self.total - self.left - self.fake - self.rejoins)


class InvitesCog(XCog, name="Invites"):
    feature = "invites"

    group = app_commands.Group(name="invites", description=T("invites"), guild_only=True)

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._cache: dict[int, dict[str, CachedInvite]] = {}

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.invites

    async def on_load(self) -> None:
        if self.bot.is_ready():
            for guild in self.bot.guilds:
                await self._refresh(guild)

    async def _refresh(self, guild: discord.Guild) -> dict[str, CachedInvite]:
        snapshot: dict[str, CachedInvite] = {}
        try:
            for invite in await guild.invites():
                snapshot[invite.code] = CachedInvite(invite.uses or 0, invite.max_uses or 0,
                                                     invite.inviter.id if invite.inviter else None)
        except discord.HTTPException:
            _log.debug("No permission to read invites of %s", guild.id)
            return self._cache.get(guild.id, {})
        if "VANITY_URL" in guild.features:
            try:
                vanity = await guild.vanity_invite()
                if vanity is not None:
                    snapshot[VANITY] = CachedInvite(vanity.uses or 0, 0, None)
            except discord.HTTPException:
                _log.debug("Could not read vanity invite of %s", guild.id)
        self._cache[guild.id] = snapshot
        return snapshot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._refresh(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._refresh(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is not None:
            self._cache.setdefault(invite.guild.id, {})[invite.code] = CachedInvite(
                invite.uses or 0, invite.max_uses or 0, invite.inviter.id if invite.inviter else None)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        cached = self._cache.get(invite.guild.id, {}).get(invite.code)
        # single-use invites are deleted right after the join → keep them until the join was processed
        if cached is not None and not (cached.max_uses and cached.uses + 1 >= cached.max_uses):
            self._cache[invite.guild.id].pop(invite.code, None)

    @staticmethod
    def _detect(old: dict[str, CachedInvite], new: dict[str, CachedInvite]) -> tuple[str | None, int | None]:
        increased = [code for code, inv in new.items() if inv.uses > (old[code].uses if code in old else 0)]
        if len(increased) == 1:
            code = increased[0]
            return code, new[code].inviter_id
        if not increased:
            vanished = [code for code in old if code not in new and code != VANITY]
            if len(vanished) == 1:
                return vanished[0], old[vanished[0]].inviter_id
        return None, None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self.bot.features.is_enabled(member.guild.id, "invites"):
            return
        guild = member.guild
        old = dict(self._cache.get(guild.id, {}))
        new = await self._refresh(guild)
        code, inviter_id = self._detect(old, new)
        fake = (utcnow() - member.created_at) < dt.timedelta(days=self.cfg.fake_account_age_days)
        await self.bot.db.ensure_guild(guild.id, guild.name)
        rejoin = bool(await self.bot.db.fetchval(
            "SELECT 1 FROM invites WHERE guild_id = $1 AND invitee_id = $2 LIMIT 1", guild.id, member.id))
        await self.bot.db.execute(
            "INSERT INTO invites (guild_id, invitee_id, inviter_id, code, fake, rejoin) VALUES ($1, $2, $3, $4, $5, $6)",
            guild.id, member.id, inviter_id, code, fake, rejoin,
        )
        await self._announce(member, code, inviter_id, fake, rejoin)

    async def _announce(self, member: discord.Member, code: str | None, inviter_id: int | None, fake: bool,
                        rejoin: bool) -> None:
        channel = member.guild.get_channel(self.cfg.log_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            return
        tr = self.gtr(member.guild.id)
        b = CV2(self.bot.theme)
        b.heading(tr("invites.join.title"), emoji="invite", level=3)
        if inviter_id:
            stats = await self.stats(member.guild.id, inviter_id)
            b.text(tr("invites.join.by", user=member.mention, inviter=f"<@{inviter_id}>", count=stats.effective))
        elif code == VANITY:
            b.text(tr("invites.join.vanity", user=member.mention))
        else:
            b.text(tr("invites.join.unknown", user=member.mention))
        flags = []
        if code:
            flags.append(f"`{code}`")
        if fake:
            flags.append(tr("invites.flags.fake"))
        if rejoin:
            flags.append(tr("invites.flags.rejoin"))
        b.footer(" · ".join([tr("invites.join.created", created=ts(member.created_at, "R")), *flags]))
        await channel.send(view=b.build(), allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.bot.db.execute(
            """
            UPDATE invites SET left_at = now()
             WHERE id = (SELECT id FROM invites WHERE guild_id = $1 AND invitee_id = $2 AND left_at IS NULL
                          ORDER BY joined_at DESC LIMIT 1)
            """,
            member.guild.id, member.id,
        )

    # ------------------------------------------------------------------ statistics
    async def stats(self, guild_id: int, inviter_id: int) -> InviteStats:
        row = await self.bot.db.fetchrow(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE left_at IS NOT NULL AND NOT fake AND NOT rejoin) AS left_count,
                   count(*) FILTER (WHERE fake) AS fake,
                   count(*) FILTER (WHERE rejoin AND NOT fake) AS rejoins
              FROM invites WHERE guild_id = $1 AND inviter_id = $2
            """,
            guild_id, inviter_id,
        )
        assert row is not None
        return InviteStats(int(row["total"]), int(row["left_count"]), int(row["fake"]), int(row["rejoins"]))

    async def leaderboard(self, guild_id: int, limit: int) -> list[tuple[int, int]]:
        rows = await self.bot.db.fetch(
            """
            SELECT inviter_id, count(*) FILTER (WHERE left_at IS NULL AND NOT fake AND NOT rejoin) AS effective
              FROM invites WHERE guild_id = $1 AND inviter_id IS NOT NULL
             GROUP BY inviter_id ORDER BY effective DESC, inviter_id LIMIT $2
            """,
            guild_id, limit,
        )
        return [(r["inviter_id"], int(r["effective"])) for r in rows if r["effective"] > 0]

    @group.command(name="show", description=T("invites.show"))
    @app_commands.describe(user=T("invites.user"))
    async def show(self, interaction: discord.Interaction[Any], user: discord.Member | None = None) -> None:
        assert interaction.guild_id is not None
        target = user or interaction.user
        stats = await self.stats(interaction.guild_id, target.id)
        tr = self.tr(interaction)
        b = CV2(self.bot.theme)
        b.banner("invites")
        b.heading(tr("invites.show.title", user=target.display_name), emoji="invite",
                  subline=tr("invites.show.effective", count=stats.effective))
        b.bullets([
            Bullet(tr("invites.show.total"), str(stats.total), emoji="add"),
            Bullet(tr("invites.show.left"), str(stats.left), emoji="remove"),
            Bullet(tr("invites.show.fake"), str(stats.fake), emoji="warning"),
            Bullet(tr("invites.show.rejoins"), str(stats.rejoins), emoji="refresh"),
        ])
        await interaction.response.send_message(view=b.build(), ephemeral=user is None)

    @group.command(name="leaderboard", description=T("invites.leaderboard"))
    async def leaderboard_cmd(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        rows = await self.leaderboard(interaction.guild_id, self.cfg.leaderboard_size)
        b = CV2(self.bot.theme)
        b.banner("invites")
        b.heading(tr("invites.leaderboard.title"), emoji="trophy")
        if rows:
            medals = ["🥇", "🥈", "🥉"]
            b.text("\n".join(f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{uid}> · "
                             f"{tr('invites.leaderboard.count', count=count)}" for i, (uid, count) in enumerate(rows)))
        else:
            b.quote(tr("invites.leaderboard.empty"))
        await interaction.response.send_message(view=b.build(), allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="who", description=T("invites.who"))
    @app_commands.describe(user=T("invites.user"))
    async def who(self, interaction: discord.Interaction[Any], user: discord.Member) -> None:
        assert interaction.guild_id is not None
        tr = self.tr(interaction)
        row = await self.bot.db.fetchrow(
            "SELECT inviter_id, code, joined_at FROM invites WHERE guild_id = $1 AND invitee_id = $2 "
            "ORDER BY joined_at DESC LIMIT 1",
            interaction.guild_id, user.id,
        )
        if row is None or row["inviter_id"] is None:
            text = tr("invites.who.unknown", user=user.mention)
        else:
            text = tr("invites.who.found", user=user.mention, inviter=f"<@{row['inviter_id']}>",
                      code=row["code"] or "—", when=ts(row["joined_at"], "R"))
        await self.bot.respond_notice(interaction, kind="info", title=tr("invites.who.title"), description=text)
