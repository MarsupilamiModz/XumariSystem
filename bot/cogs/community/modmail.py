"""Mod-mail: DMs to the bot are forwarded into a staff thread; staff replies are sent back as DM."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import NotFound, UserError
from ...core.logger import get_logger
from ...core.permissions import require_staff
from ...core.utils import slugify, truncate
from ...ui.builder import CV2

_log = get_logger("modmail")
MAX_FILES = 5
MAX_FILE_BYTES = 8 * 1024 * 1024
INTERNAL_PREFIX = "!"


class ModmailCog(XCog, name="Modmail"):
    feature = "community"

    group = app_commands.Group(name="modmail", description=T("modmail"), guild_only=True)

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._threads: dict[int, int] = {}  # thread_id -> user_id
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.community.modmail

    async def on_load(self) -> None:
        rows = await self.bot.db.fetch("SELECT thread_id, user_id FROM modmail_threads WHERE status = 'open'")
        self._threads = {r["thread_id"]: r["user_id"] for r in rows}

    def _guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.cfg.guild_id or 0) if self.cfg.enabled else None

    @staticmethod
    async def _files(message: discord.Message) -> list[discord.File]:
        files: list[discord.File] = []
        for attachment in message.attachments[:MAX_FILES]:
            if attachment.size <= MAX_FILE_BYTES:
                try:
                    files.append(await attachment.to_file())
                except discord.HTTPException:
                    _log.debug("Could not download attachment %s", attachment.id)
        return files

    def _relay_view(self, title: str, emoji: str, author: discord.abc.User, content: str,
                    files: list[discord.File], footer: str | None = None) -> discord.ui.LayoutView:
        b = CV2(self.bot.theme)
        b.thumbnail_section(f"### {self.bot.icons.resolve(emoji)} {title}\n-# {author} ({author.id})",
                            content and truncate(content, 3000) or "—", url=author.display_avatar.replace(size=64).url)
        images = [f"attachment://{f.filename}" for f in files
                  if f.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))]
        if images:
            b.media(*images)
        for f in files:
            if f"attachment://{f.filename}" not in images:
                b.file(f.filename)
        b.footer(footer)
        return b.build()

    async def _open_thread(self, guild: discord.Guild, user: discord.abc.User) -> discord.Thread:
        # two quick DMs must not create two threads
        async with self._locks[user.id]:
            return await self._open_thread_locked(guild, user)

    async def _open_thread_locked(self, guild: discord.Guild, user: discord.abc.User) -> discord.Thread:
        row = await self.bot.db.fetchrow(
            "SELECT thread_id FROM modmail_threads WHERE guild_id = $1 AND user_id = $2 AND status = 'open'",
            guild.id, user.id,
        )
        if row is not None:
            thread = guild.get_thread(row["thread_id"]) or await self._fetch_thread(row["thread_id"])
            if thread is not None:
                if thread.archived:
                    await thread.edit(archived=False, locked=False)
                return thread
            await self.bot.db.execute("UPDATE modmail_threads SET status = 'closed', closed_at = now() "
                                      "WHERE thread_id = $1", row["thread_id"])
        channel = guild.get_channel(self.cfg.channel_id or 0)
        tr = self.gtr(guild.id)
        name = f"modmail-{slugify(user.name, 80)}"
        intro = CV2(self.bot.theme).mention(" ".join(f"<@&{rid}>" for rid in self.cfg.staff_role_ids)) \
            .banner("modmail").heading(tr("community.modmail.new_title"), emoji="modmail") \
            .quote(tr("community.modmail.new_text", user=f"{user.mention} ({user.id})")) \
            .footer(tr("community.modmail.staff_hint", prefix=INTERNAL_PREFIX)).build()
        if isinstance(channel, discord.ForumChannel):
            created = await channel.create_thread(name=name, view=intro,
                                                  allowed_mentions=discord.AllowedMentions(roles=True))
            thread = created.thread
        elif isinstance(channel, discord.TextChannel):
            starter = await channel.send(view=intro, allowed_mentions=discord.AllowedMentions(roles=True))
            thread = await starter.create_thread(name=name, auto_archive_duration=10080)
        else:
            raise UserError("errors.not_configured", what="community.modmail.channel_id")
        await self.bot.db.ensure_guild(guild.id)
        try:
            await self.bot.db.execute("INSERT INTO modmail_threads (guild_id, user_id, thread_id) VALUES ($1, $2, $3)",
                                      guild.id, user.id, thread.id)
        except asyncpg.UniqueViolationError:
            _log.warning("Open modmail thread for %s already existed (created by another process)", user.id)
        self._threads[thread.id] = user.id
        return thread

    async def _fetch_thread(self, thread_id: int) -> discord.Thread | None:
        try:
            channel = await self.bot.fetch_channel(thread_id)
        except discord.HTTPException:
            return None
        return channel if isinstance(channel, discord.Thread) else None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            await self._from_user(message)
        elif isinstance(message.channel, discord.Thread) and message.channel.id in self._threads:
            await self._from_staff(message)

    async def _from_user(self, message: discord.Message) -> None:
        guild = self._guild()
        if guild is None or guild.get_member(message.author.id) is None:
            return
        if not self.bot.features.is_enabled(guild.id, "community"):
            return
        tr = self.gtr(guild.id)
        try:
            thread = await self._open_thread(guild, message.author)
            files = await self._files(message)
            await thread.send(view=self._relay_view(tr("community.modmail.from_user"), "message", message.author,
                                                    message.content, files), files=files,
                              allowed_mentions=discord.AllowedMentions.none())
            await message.add_reaction("✅")
        except (discord.HTTPException, UserError):
            _log.exception("Modmail relay failed for %s", message.author.id)
            await message.add_reaction("❌")

    async def _from_staff(self, message: discord.Message) -> None:
        if message.content.startswith(INTERNAL_PREFIX):
            return
        user_id = self._threads[message.channel.id]
        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        guild = message.guild
        assert guild is not None
        tr = self.bot.i18n.translator(self.bot.guild_locale(guild.id))
        files = await self._files(message)
        view = self._relay_view(tr("community.modmail.from_staff", guild=guild.name), "support", message.author,
                                message.content, files, footer=tr("community.modmail.reply_hint"))
        try:
            await user.send(view=view, files=files)
            await message.add_reaction("✅")
        except discord.HTTPException:
            await message.add_reaction("❌")

    @group.command(name="close", description=T("modmail.close"))
    @require_staff()
    async def close(self, interaction: discord.Interaction[Any]) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.Thread) or channel.id not in self._threads:
            raise NotFound("community.modmail.not_thread")
        user_id = self._threads.pop(channel.id)
        await self.bot.db.execute(
            "UPDATE modmail_threads SET status = 'closed', closed_at = now(), closed_by = $2 WHERE thread_id = $1",
            channel.id, interaction.user.id,
        )
        tr = self.tr(interaction)
        await interaction.response.send_message(view=self.bot.notice(kind="success", title=tr("community.modmail.closed")))
        user = self.bot.get_user(user_id)
        if user is not None and interaction.guild is not None:
            gtr = self.gtr(interaction.guild.id)
            await self.bot.safe_dm(user, self.bot.notice(kind="info", title=gtr("community.modmail.closed_dm",
                                                                                 guild=interaction.guild.name)))
        try:
            await channel.edit(archived=True, locked=True)
        except discord.HTTPException:
            _log.debug("Could not archive modmail thread %s", channel.id)

    @group.command(name="contact", description=T("modmail.contact"))
    @app_commands.describe(user=T("invites.user"), message=T("modmail.message"))
    @require_staff()
    async def contact(self, interaction: discord.Interaction[Any], user: discord.Member,
                      message: app_commands.Range[str, 1, 2000]) -> None:
        assert interaction.guild is not None
        if not self.cfg.enabled or interaction.guild.id != self.cfg.guild_id:
            raise UserError("errors.not_configured", what="community.modmail")
        await interaction.response.defer(ephemeral=True, thinking=True)
        thread = await self._open_thread(interaction.guild, user)
        gtr = self.gtr(interaction.guild.id)
        view = self._relay_view(gtr("community.modmail.from_staff", guild=interaction.guild.name), "support",
                                interaction.user, message, [], footer=gtr("community.modmail.reply_hint"))
        delivered = await self.bot.safe_dm(user, view)
        await thread.send(view=self._relay_view(gtr("community.modmail.to_user"), "send", interaction.user, message, []),
                          allowed_mentions=discord.AllowedMentions.none())
        tr = self.tr(interaction)
        await interaction.followup.send(view=self.bot.notice(
            kind="success" if delivered else "warning",
            title=tr("community.modmail.contacted", thread=thread.mention) if delivered else tr("errors.dm_closed")),
            ephemeral=True)
