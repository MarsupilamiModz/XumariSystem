"""Giveaways: duration, winner count, role requirement, join button, automatic ending, reroll."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

import asyncpg
import discord
from discord import app_commands
from discord.ext import tasks

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import NotFound, UserError
from ...core.logger import get_logger
from ...core.permissions import require_staff
from ...core.router import ComponentContext, cid, component
from ...core.tasks import resilient_iteration
from ...core.utils import parse_duration, truncate, ts, utcnow
from ...ui.builder import CV2, Bullet, button

_log = get_logger("giveaways")
_rng = random.SystemRandom()


@dataclass(slots=True)
class Giveaway:
    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    host_id: int | None
    prize: str
    description: str | None
    winners: int
    required_role_id: int | None
    locale: str
    ends_at: Any
    ended: bool
    winner_ids: list[int]

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Giveaway:
        data = dict(row)
        data.pop("created_at", None)
        data.pop("retry_at", None)
        data["winner_ids"] = list(data["winner_ids"] or [])
        return cls(**data)


class GiveawaysCog(XCog, name="Giveaways"):
    feature = "community"

    group = app_commands.Group(name="giveaway", description=T("giveaway"), guild_only=True)

    async def on_load(self) -> None:
        self.end_loop.change_interval(seconds=max(10, self.bot.config.community.giveaways.check_interval_seconds))
        self.end_loop.start()

    async def on_unload(self) -> None:
        self.end_loop.cancel()

    async def get(self, giveaway_id: int, guild_id: int | None = None) -> Giveaway:
        row = await self.bot.db.fetchrow(
            "SELECT * FROM giveaways WHERE id = $1 AND ($2::bigint IS NULL OR guild_id = $2)", giveaway_id, guild_id
        )
        if row is None:
            raise NotFound("community.giveaway.not_found")
        return Giveaway.from_row(row)

    async def entry_count(self, giveaway_id: int) -> int:
        return int(await self.bot.db.fetchval("SELECT count(*) FROM giveaway_entries WHERE giveaway_id = $1",
                                              giveaway_id))

    async def render(self, gw: Giveaway) -> discord.ui.LayoutView:
        tr = self.bot.i18n.translator(gw.locale)
        entries = await self.entry_count(gw.id)
        b = CV2(self.bot.theme)
        b.banner("giveaway")
        b.heading(gw.prize, emoji="giveaway", subline=tr("community.giveaway.subline", id=gw.id))
        b.quote(gw.description)
        items = [
            Bullet(tr("community.giveaway.ends") if not gw.ended else tr("community.giveaway.ended_at"),
                   f"{ts(gw.ends_at, 'R')} ({ts(gw.ends_at, 'f')})", emoji="clock"),
            Bullet(tr("community.giveaway.winners"), str(gw.winners), emoji="trophy"),
            Bullet(tr("community.giveaway.entries"), str(entries), emoji="users"),
        ]
        if gw.host_id:
            items.append(Bullet(tr("community.giveaway.host"), f"<@{gw.host_id}>", emoji="user"))
        if gw.required_role_id:
            items.append(Bullet(tr("community.giveaway.required"), f"<@&{gw.required_role_id}>", emoji="role"))
        b.bullets(items)
        if gw.ended:
            b.separator()
            winners = " ".join(f"<@{uid}>" for uid in gw.winner_ids) or tr("community.giveaway.no_winners")
            b.text(tr("community.giveaway.result", winners=winners))
        else:
            b.footer(tr("community.giveaway.footer"))
            b.separator()
            b.actions(button(self.bot.theme, tr("community.giveaway.join"), emoji="giveaway",
                             custom_id=cid("gw.join", gw.id), style=discord.ButtonStyle.success))
        return b.build()

    async def _refresh_message(self, gw: Giveaway) -> None:
        channel = self.bot.get_channel(gw.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)) or not gw.message_id:
            return
        try:
            await channel.get_partial_message(gw.message_id).edit(view=await self.render(gw))
        except discord.HTTPException:
            _log.debug("Could not refresh giveaway %s", gw.id)

    # ------------------------------------------------------------------ commands
    @group.command(name="start", description=T("giveaway.start"))
    @app_commands.describe(prize=T("giveaway.prize"), duration=T("giveaway.duration"), winners=T("giveaway.winners"),
                           channel=T("giveaway.channel"), required_role=T("giveaway.required_role"),
                           description=T("giveaway.description"))
    @require_staff()
    async def start(
        self, interaction: discord.Interaction[Any], prize: app_commands.Range[str, 1, 200], duration: str,
        winners: app_commands.Range[int, 1, 50] = 1, channel: discord.TextChannel | None = None,
        required_role: discord.Role | None = None, description: app_commands.Range[str, 1, 1000] | None = None,
    ) -> None:
        assert interaction.guild is not None
        delta = parse_duration(duration)
        if delta is None:
            raise UserError("errors.invalid_duration")
        target = channel or interaction.guild.get_channel(self.bot.config.community.giveaways.default_channel_id or 0) \
            or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise UserError("errors.invalid_input")
        locale = self.bot.guild_locale(interaction.guild.id)
        await self.bot.db.ensure_guild(interaction.guild.id)
        row = await self.bot.db.fetchrow(
            """
            INSERT INTO giveaways (guild_id, channel_id, host_id, prize, description, winners, required_role_id,
                                   locale, ends_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING *
            """,
            interaction.guild.id, target.id, interaction.user.id, prize, description, winners,
            required_role.id if required_role else None, locale, utcnow() + delta,
        )
        assert row is not None
        gw = Giveaway.from_row(row)
        message = await target.send(view=await self.render(gw), allowed_mentions=discord.AllowedMentions.none())
        await self.bot.db.execute("UPDATE giveaways SET message_id = $2 WHERE id = $1", gw.id, message.id)
        tr = self.tr(interaction)
        await self.bot.respond_notice(interaction, title=tr("community.giveaway.started", id=gw.id,
                                                            channel=target.mention))
        await self.bot.audit.log(interaction.guild, "community", "giveaway.start",
                                 title=tr("community.giveaway.log_start"), emoji="giveaway", actor=interaction.user,
                                 lines=[f"#{gw.id} · {prize}", message.jump_url])

    async def _active_choices(self, interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[int]]:
        rows = await self.bot.db.fetch(
            "SELECT id, prize, ended FROM giveaways WHERE guild_id = $1 ORDER BY id DESC LIMIT 25", interaction.guild_id
        )
        return [app_commands.Choice(name=truncate(f"#{r['id']} {r['prize']}" + (" ✓" if r["ended"] else ""), 100),
                                    value=r["id"]) for r in rows if current.lower() in r["prize"].lower()][:25]

    @group.command(name="end", description=T("giveaway.end"))
    @app_commands.describe(giveaway=T("giveaway.giveaway"))
    @require_staff()
    async def end_cmd(self, interaction: discord.Interaction[Any], giveaway: int) -> None:
        gw = await self.get(giveaway, interaction.guild_id)
        if gw.ended:
            raise UserError("community.giveaway.already_ended")
        await interaction.response.defer(ephemeral=True)
        await self.end(gw)
        await interaction.followup.send(view=self.bot.notice(kind="success", title=self.tr(interaction)(
            "community.giveaway.ended_ok", id=gw.id)), ephemeral=True)

    end_cmd.autocomplete("giveaway")(_active_choices)

    @group.command(name="reroll", description=T("giveaway.reroll"))
    @app_commands.describe(giveaway=T("giveaway.giveaway"), winners=T("giveaway.winners"))
    @require_staff()
    async def reroll(self, interaction: discord.Interaction[Any], giveaway: int,
                     winners: app_commands.Range[int, 1, 50] | None = None) -> None:
        gw = await self.get(giveaway, interaction.guild_id)
        if not gw.ended:
            raise UserError("community.giveaway.not_ended")
        await interaction.response.defer(ephemeral=True)
        await self.end(gw, count=winners, reroll=True)
        await interaction.followup.send(view=self.bot.notice(kind="success", title=self.tr(interaction)(
            "community.giveaway.rerolled", id=gw.id)), ephemeral=True)

    reroll.autocomplete("giveaway")(_active_choices)

    @group.command(name="list", description=T("giveaway.list"))
    async def list_cmd(self, interaction: discord.Interaction[Any]) -> None:
        tr = self.tr(interaction)
        rows = await self.bot.db.fetch(
            "SELECT * FROM giveaways WHERE guild_id = $1 AND NOT ended ORDER BY ends_at", interaction.guild_id
        )
        b = CV2(self.bot.theme)
        b.banner("giveaway")
        b.heading(tr("community.giveaway.list_title"), emoji="giveaway")
        if rows:
            b.bullets([Bullet(f"#{r['id']} {r['prize']}", ts(r["ends_at"], "R"), emoji="giveaway") for r in rows[:20]])
        else:
            b.quote(tr("community.giveaway.none"))
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    # ------------------------------------------------------------------ join / end
    @component("gw.join")
    async def on_join(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        gw = await self.get(ctx.int_arg(0), interaction.guild_id)
        tr = self.tr(interaction)
        if gw.ended:
            raise UserError("community.giveaway.already_ended")
        member = interaction.user
        if gw.required_role_id and isinstance(member, discord.Member) and \
                not any(r.id == gw.required_role_id for r in member.roles):
            raise UserError("community.giveaway.missing_role", role=f"<@&{gw.required_role_id}>")
        removed = await self.bot.db.execute(
            "DELETE FROM giveaway_entries WHERE giveaway_id = $1 AND user_id = $2", gw.id, member.id
        )
        if removed.endswith(" 0"):
            await self.bot.db.execute(
                "INSERT INTO giveaway_entries (giveaway_id, guild_id, user_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                gw.id, gw.guild_id, member.id,
            )
            title = tr("community.giveaway.joined")
        else:
            title = tr("community.giveaway.left")
        await interaction.response.send_message(view=self.bot.notice(kind="success", title=title), ephemeral=True)
        await self._refresh_message(gw)

    async def end(self, gw: Giveaway, *, count: int | None = None, reroll: bool = False) -> list[int]:
        guild = self.bot.get_guild(gw.guild_id)
        rows = await self.bot.db.fetch("SELECT user_id FROM giveaway_entries WHERE giveaway_id = $1", gw.id)
        candidates = []
        for r in rows:
            member = guild.get_member(r["user_id"]) if guild else None
            if member is None:
                continue
            if gw.required_role_id and not any(role.id == gw.required_role_id for role in member.roles):
                continue
            if reroll and member.id in gw.winner_ids:
                continue
            candidates.append(member.id)
        winners = _rng.sample(candidates, min(count or gw.winners, len(candidates)))
        await self.bot.db.execute("UPDATE giveaways SET ended = TRUE, winner_ids = $2 WHERE id = $1", gw.id, winners)
        gw.ended, gw.winner_ids = True, winners
        await self._refresh_message(gw)
        channel = self.bot.get_channel(gw.channel_id)
        tr = self.bot.i18n.translator(gw.locale)
        if isinstance(channel, discord.TextChannel):
            text = tr("community.giveaway.announce", prize=gw.prize, winners=" ".join(f"<@{w}>" for w in winners)) \
                if winners else tr("community.giveaway.announce_none", prize=gw.prize)
            b = CV2(self.bot.theme)
            if winners:
                b.mention(" ".join(f"<@{w}>" for w in winners))
            b.heading(tr("community.giveaway.reroll_title") if reroll else tr("community.giveaway.end_title"),
                      emoji="giveaway", level=3)
            b.quote(text)
            try:
                reference = channel.get_partial_message(gw.message_id) if gw.message_id else None
                await channel.send(view=b.build(), reference=reference,
                                   allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                _log.warning("Could not announce giveaway %s", gw.id)
        if guild is not None:
            gtr = self.gtr(guild.id)
            await self.bot.audit.log(guild, "community", "giveaway.end", title=gtr("community.giveaway.log_end"),
                                     emoji="giveaway", lines=[f"#{gw.id} · {gw.prize}",
                                                              " ".join(f"<@{w}>" for w in winners) or "—"])
        return winners

    @tasks.loop(seconds=30)
    @resilient_iteration("giveaways")
    async def end_loop(self) -> None:
        rows = await self.bot.db.fetch(
            "SELECT * FROM giveaways WHERE NOT ended AND ends_at <= now() AND (retry_at IS NULL OR retry_at <= now()) "
            "AND guild_id = ANY($1::bigint[]) "
            "ORDER BY ends_at LIMIT 20",
            [g.id for g in self.bot.guilds],
        )
        for row in rows:
            gw = Giveaway.from_row(row)
            try:
                await self.end(gw)
            except Exception:  # noqa: BLE001 – one broken giveaway must not stop the others
                _log.exception("Ending giveaway %s failed – retrying in 5 minutes", gw.id)
                await self.bot.db.execute(
                    "UPDATE giveaways SET retry_at = now() + interval '5 minutes' WHERE id = $1", gw.id)

    @end_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
