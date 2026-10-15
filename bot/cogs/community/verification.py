"""Join flow: CAPTCHA verification (image or button code), welcome message + auto roles, nickname filter."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import random
import re
import secrets
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import UserError
from ...core.logger import get_logger
from ...core.permissions import require_admin
from ...core.router import ComponentContext, cid, component
from ...core.tasks import resilient_iteration
from ...core.utils import utcnow
from ...ui.builder import CV2, button
from ...ui.modals import FormModal, TextField

_log = get_logger("verification")
_rng = random.SystemRandom()
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
_FONTS: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)
CHALLENGE_MINUTES = 10


def render_captcha(code: str) -> bytes:
    """Distorted PNG captcha (runs in a worker thread)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    width, height = 60 + 42 * len(code), 90
    image = Image.new("RGB", (width, height), (32, 34, 40))
    draw = ImageDraw.Draw(image)
    font_path = next((f for f in _FONTS if Path(f).is_file()), None)
    font = ImageFont.truetype(font_path, 46) if font_path else ImageFont.load_default(size=46)
    for _ in range(14):
        draw.line([(_rng.randint(0, width), _rng.randint(0, height)), (_rng.randint(0, width), _rng.randint(0, height))],
                  fill=(_rng.randint(70, 140),) * 3, width=_rng.randint(1, 3))
    x = 30
    for char in code:
        layer = Image.new("RGBA", (60, 70), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((8, 4), char, font=font,
                                   fill=(_rng.randint(150, 255), _rng.randint(150, 255), _rng.randint(150, 255)))
        layer = layer.rotate(_rng.uniform(-28, 28), resample=Image.Resampling.BICUBIC, expand=False)
        image.paste(layer, (x, _rng.randint(4, 16)), layer)
        x += 42
    for _ in range(400):
        draw.point((_rng.randint(0, width - 1), _rng.randint(0, height - 1)), fill=(_rng.randint(90, 200),) * 3)
    image = image.filter(ImageFilter.SMOOTH)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class VerificationCog(XCog, name="Verification"):
    feature = "community"

    captcha_group = app_commands.Group(name="captcha", description=T("captcha"), guild_only=True,
                                       default_permissions=discord.Permissions(administrator=True))

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.community

    async def on_load(self) -> None:
        self.kick_loop.start()

    async def on_unload(self) -> None:
        self.kick_loop.cancel()

    # ================================================================== join
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or not self.bot.features.is_enabled(member.guild.id, "community"):
            return
        await self.check_nickname(member)
        captcha = self.cfg.captcha
        if captcha.enabled:
            role = member.guild.get_role(captcha.unverified_role_id or 0)
            if role is not None:
                try:
                    await member.add_roles(role, reason="Captcha pending")
                except discord.HTTPException:
                    _log.warning("Could not add unverified role to %s", member.id)
        else:
            await self.give_auto_roles(member)
        await self.welcome(member)

    async def give_auto_roles(self, member: discord.Member) -> None:
        roles = [r for r in (member.guild.get_role(rid) for rid in self.cfg.welcome.auto_role_ids) if r]
        if roles:
            try:
                await member.add_roles(*roles, reason="Auto roles")
            except discord.HTTPException:
                _log.warning("Could not add auto roles to %s", member.id)

    async def welcome(self, member: discord.Member) -> None:
        cfg = self.cfg.welcome
        if not cfg.enabled:
            return
        tr = self.gtr(member.guild.id)
        b = CV2(self.bot.theme)
        b.mention(member.mention)
        b.banner("welcome")
        b.heading(tr("community.welcome.title", guild=member.guild.name), emoji="welcome")
        b.quote(tr("community.welcome.text", user=member.mention))
        b.footer(tr("community.welcome.footer", count=member.guild.member_count or 0))
        channel = member.guild.get_channel(cfg.channel_id or 0)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(view=b.build(), allowed_mentions=discord.AllowedMentions(users=[member]))
            except discord.HTTPException:
                _log.warning("Could not send welcome message in %s", channel.id)
        if cfg.dm_user:
            dm = CV2(self.bot.theme).banner("welcome").heading(
                tr("community.welcome.title", guild=member.guild.name), emoji="welcome"
            ).quote(tr("community.welcome.dm", guild=member.guild.name)).build()
            await self.bot.safe_dm(member, dm)

    # ================================================================== nickname filter
    def nickname_violation(self, name: str) -> bool:
        cfg = self.cfg.nickname_filter
        if cfg.allowed_pattern and not re.fullmatch(cfg.allowed_pattern, name):
            return True
        lowered = name.lower()
        return any(word.lower() in lowered for word in cfg.blocked_words if word)

    async def check_nickname(self, member: discord.Member) -> None:
        cfg = self.cfg.nickname_filter
        if not cfg.enabled or member.bot or member.id == member.guild.owner_id:
            return
        name = member.display_name
        if not self.nickname_violation(name):
            return
        new_name = cfg.replacement.format(id4=str(member.id)[-4:], id=member.id)[:32]
        try:
            await member.edit(nick=new_name, reason="Nickname filter")
        except discord.HTTPException:
            _log.info("Could not rename %s (permissions/hierarchy)", member.id)
            return
        tr = self.gtr(member.guild.id)
        await self.bot.audit.log(member.guild, "moderation", "nickname.filter", title=tr("community.nickname.log"),
                                 emoji="shield", target_id=member.id,
                                 lines=[tr("community.nickname.line", user=member.mention, old=name, new=new_name)])

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.nick != after.nick and self.bot.features.is_enabled(after.guild.id, "community"):
            await self.check_nickname(after)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if before.global_name == after.global_name and before.name == after.name:
            return
        for guild in self.bot.guilds:
            member = guild.get_member(after.id)
            if member is not None and member.nick is None and self.bot.features.is_enabled(guild.id, "community"):
                await self.check_nickname(member)

    # ================================================================== captcha
    @captcha_group.command(name="panel", description=T("captcha.panel"))
    @app_commands.describe(channel=T("shop.channel"))
    @require_admin()
    async def panel(self, interaction: discord.Interaction[Any], channel: discord.TextChannel | None = None) -> None:
        assert interaction.guild is not None
        target = channel or interaction.guild.get_channel(self.cfg.captcha.channel_id or 0) or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise UserError("errors.invalid_input")
        tr = self.gtr(interaction.guild.id)
        b = CV2(self.bot.theme)
        b.banner("captcha")
        b.heading(tr("community.captcha.title"), emoji="captcha")
        b.quote(tr("community.captcha.text"))
        b.footer(tr("community.captcha.footer"))
        b.separator()
        b.actions(button(self.bot.theme, tr("community.captcha.start"), emoji="shield", custom_id=cid("cap.start"),
                         style=discord.ButtonStyle.success))
        await target.send(view=b.build())
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("shop.panel_sent", channel=target.mention))

    def _is_verified(self, member: discord.Member) -> bool:
        role_id = self.cfg.captcha.verified_role_id
        unverified = self.cfg.captcha.unverified_role_id
        has_verified = bool(role_id) and any(r.id == role_id for r in member.roles)
        has_unverified = bool(unverified) and any(r.id == unverified for r in member.roles)
        return has_verified and not has_unverified

    async def _new_challenge(self, guild_id: int, user_id: int) -> str:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(max(4, min(8, self.cfg.captcha.code_length))))
        await self.bot.db.execute(
            """
            INSERT INTO captcha_challenges (guild_id, user_id, code, attempts, expires_at)
            VALUES ($1, $2, $3, 0, now() + make_interval(mins => $4))
            ON CONFLICT (guild_id, user_id) DO UPDATE
               SET code = EXCLUDED.code, expires_at = EXCLUDED.expires_at,
                   attempts = CASE WHEN captcha_challenges.expires_at < now() THEN 0 ELSE captcha_challenges.attempts END
            """,
            guild_id, user_id, code, CHALLENGE_MINUTES,
        )
        return code

    @component("cap.start")
    async def on_start(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member) and interaction.guild_id is not None
        tr = self.tr(interaction)
        if not self.cfg.captcha.enabled:
            raise UserError("errors.feature_disabled")
        if self._is_verified(member):
            raise UserError("community.captcha.already")
        attempts = await self.bot.db.fetchval(
            "SELECT attempts FROM captcha_challenges WHERE guild_id = $1 AND user_id = $2 AND expires_at > now()",
            interaction.guild_id, member.id,
        )
        if attempts is not None and attempts >= self.cfg.captcha.max_attempts:
            raise UserError("community.captcha.locked", minutes=CHALLENGE_MINUTES)
        code = await self._new_challenge(interaction.guild_id, member.id)
        b = CV2(self.bot.theme)
        b.heading(tr("community.captcha.challenge_title"), emoji="captcha", level=3)
        if self.cfg.captcha.mode == "image":
            data = await asyncio.to_thread(render_captcha, code)
            b.quote(tr("community.captcha.image_text"))
            b.media("attachment://captcha.png")
            b.separator()
            b.actions(button(self.bot.theme, tr("community.captcha.enter"), emoji="edit", custom_id=cid("cap.enter"),
                             style=discord.ButtonStyle.success))
            await interaction.response.send_message(view=b.build(), ephemeral=True,
                                                    file=discord.File(io.BytesIO(data), filename="captcha.png"))
            return
        options = {code}
        while len(options) < 5:
            options.add("".join(secrets.choice(_ALPHABET) for _ in range(len(code))))
        shuffled = list(options)
        _rng.shuffle(shuffled)
        b.quote(tr("community.captcha.button_text", code=f"`{code}`"))
        b.separator()
        b.actions(*[button(self.bot.theme, option, emoji="shield", custom_id=cid("cap.pick", option)) for option in shuffled])
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    @component("cap.enter")
    async def on_enter(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            await self.verify(modal_interaction, str(values["code"]))

        await interaction.response.send_modal(FormModal(
            self.bot, title=tr("community.captcha.challenge_title"),
            fields=[TextField(key="code", label=tr("community.captcha.field"), max_length=8)], on_submit=submitted))

    @component("cap.pick")
    async def on_pick(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        await self.verify(interaction, ctx.arg(0) or "")

    async def verify(self, interaction: discord.Interaction[Any], value: str) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member) and interaction.guild_id is not None
        tr = self.tr(interaction)
        row = await self.bot.db.fetchrow(
            "UPDATE captcha_challenges SET attempts = attempts + 1 WHERE guild_id = $1 AND user_id = $2 "
            "RETURNING code, attempts, expires_at",
            interaction.guild_id, member.id,
        )
        if row is None or row["expires_at"] < utcnow():
            raise UserError("community.captcha.expired")
        if value.strip().upper() != row["code"]:
            remaining = self.cfg.captcha.max_attempts - row["attempts"]
            if remaining <= 0:
                raise UserError("community.captcha.locked", minutes=CHALLENGE_MINUTES)
            raise UserError("community.captcha.wrong", remaining=remaining)
        await self.bot.db.execute("DELETE FROM captcha_challenges WHERE guild_id = $1 AND user_id = $2",
                                  interaction.guild_id, member.id)
        verified = member.guild.get_role(self.cfg.captcha.verified_role_id or 0)
        unverified = member.guild.get_role(self.cfg.captcha.unverified_role_id or 0)
        try:
            if verified is not None:
                await member.add_roles(verified, reason="Captcha solved")
            if unverified is not None and unverified in member.roles:
                await member.remove_roles(unverified, reason="Captcha solved")
        except discord.HTTPException as exc:
            raise UserError("errors.bot_missing_permissions") from exc
        await self.give_auto_roles(member)
        await self.bot.respond_notice(interaction, title=tr("community.captcha.success"))
        self.bot.dispatch("xs_member_verified", member)
        gtr = self.gtr(member.guild.id)
        await self.bot.audit.log(member.guild, "members", "member.verified", title=gtr("community.captcha.log"),
                                 emoji="shield", target_id=member.id, lines=[member.mention])

    @tasks.loop(minutes=1)
    @resilient_iteration("captcha_kick")
    async def kick_loop(self) -> None:
        captcha = self.cfg.captcha
        if not captcha.enabled or captcha.kick_after_minutes <= 0 or not captcha.unverified_role_id:
            return
        limit = utcnow() - dt.timedelta(minutes=captcha.kick_after_minutes)
        for guild in self.bot.guilds:
            role = guild.get_role(captcha.unverified_role_id)
            if role is None or not self.bot.features.is_enabled(guild.id, "community"):
                continue
            for member in list(role.members):
                if member.joined_at and member.joined_at < limit:
                    try:
                        await self.bot.safe_dm(member, self.bot.notice(
                            kind="warning", title=self.gtr(guild.id)("community.captcha.kicked", guild=guild.name)))
                        await member.kick(reason="Captcha not solved in time")
                    except discord.HTTPException:
                        _log.info("Could not kick unverified member %s", member.id)

    @kick_loop.before_loop
    async def _before_kick(self) -> None:
        await self.bot.wait_until_ready()
