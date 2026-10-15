"""Audit log service: writes every relevant action to the ``logs`` table and posts a uniform CV2
log entry to the configured log channel of the category."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import discord

from ..ui.layouts import build_log_entry
from .utils import ts, utcnow

if TYPE_CHECKING:
    from .bot import XumariBot

_log = logging.getLogger("xumari.audit")


class AuditService:
    def __init__(self, bot: XumariBot) -> None:
        self.bot = bot

    def channel_for(self, guild: discord.Guild, category: str) -> discord.TextChannel | discord.Thread | None:
        cfg = self.bot.config.logging
        channel_id = cfg.channels.get(category) or cfg.default_channel_id
        if not channel_id:
            return None
        channel = guild.get_channel_or_thread(channel_id)
        return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None

    async def log(
        self,
        guild: discord.Guild | None,
        category: str,
        action: str,
        *,
        title: str,
        lines: Sequence[str] = (),
        emoji: str = "info",
        actor: discord.abc.User | None = None,
        target_id: int | None = None,
        channel_id: int | None = None,
        details: dict[str, Any] | None = None,
        file: discord.File | None = None,
        post: bool = True,
    ) -> None:
        """Persist and post a log entry. Never raises – logging must not break features."""
        if guild is None:
            return
        try:
            if self.bot.config.logging.store_in_db:
                await self.bot.db.ensure_guild(guild.id, guild.name)
                await self.bot.db.execute(
                    """
                    INSERT INTO logs (guild_id, category, action, actor_id, target_id, channel_id, details)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    guild.id,
                    category,
                    action,
                    actor.id if actor else None,
                    target_id,
                    channel_id,
                    details or {},
                )
        except Exception:  # noqa: BLE001
            _log.exception("Failed to store log entry %s/%s", category, action)

        if not post:
            return
        channel = self.channel_for(guild, category)
        if channel is None:
            return
        tr = self.bot.guild_translator(guild.id)
        footer_parts = [ts(utcnow(), "f")]
        if actor is not None:
            footer_parts.insert(0, tr("logs.by", user=f"{actor.mention} ({actor.id})"))
        view = build_log_entry(
            self.bot.theme, title=title, emoji=emoji, lines=lines, footer=" · ".join(footer_parts),
            attachment=file.filename if file is not None else None,
        )
        try:
            kwargs: dict[str, Any] = {"view": view, "allowed_mentions": discord.AllowedMentions.none()}
            if file is not None:
                kwargs["file"] = file
            await channel.send(**kwargs)
        except discord.HTTPException:
            _log.warning("Could not post log entry to channel %s", channel.id, exc_info=True)
