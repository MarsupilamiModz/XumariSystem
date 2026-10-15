"""System administration: ``/xumari info|sync|reload|language|diagnose``."""

from __future__ import annotations

from typing import Any, Literal

import discord
from discord import app_commands

from ... import __version__
from ...core.bot import EXTENSIONS
from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import UserError
from ...core.permissions import require_admin
from ...core.utils import ts
from ...ui.builder import CV2, Bullet


class SystemCog(XCog, name="System"):
    feature = "admin"

    group = app_commands.Group(
        name="xumari",
        description=T("xumari"),
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @group.command(name="info", description=T("xumari.info"))
    @require_admin()
    async def info(self, interaction: discord.Interaction[Any]) -> None:
        tr = self.tr(interaction)
        db_ok = await self.bot.db.ping()
        loaded = [k for k in EXTENSIONS if self.bot.features.loaded(k)]
        b = CV2(self.bot.theme)
        b.banner("controlpanel")
        b.heading(tr("admin.info.title", bot=self.bot.config.bot.name), emoji="settings",
                  subline=tr("admin.info.version", version=__version__))
        b.bullets(
            [
                Bullet(tr("admin.info.latency"), f"`{round(self.bot.latency * 1000)} ms`", emoji="clock"),
                Bullet(tr("admin.info.database"), tr("common.ok") if db_ok else tr("common.error"), emoji="info"),
                Bullet(tr("admin.info.uptime"), ts(self.bot.started_at, "R"), emoji="calendar"),
                Bullet(tr("admin.info.guilds"), str(len(self.bot.guilds)), emoji="users"),
                Bullet(tr("admin.info.routes"), str(len(self.bot.routes)), emoji="button"),
                Bullet(tr("admin.info.api"), f"`{self.bot.api_mode()}`", emoji="link"),
            ]
        )
        b.footer(tr("admin.info.modules", modules=", ".join(loaded) or "—"))
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    @group.command(name="sync", description=T("xumari.sync"))
    @require_admin()
    async def sync(self, interaction: discord.Interaction[Any]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        count = await self.bot.sync_commands()
        tr = self.tr(interaction)
        await interaction.followup.send(
            view=self.bot.notice(kind="success", title=tr("admin.sync.done", count=count)), ephemeral=True
        )

    @group.command(name="reload", description=T("xumari.reload"))
    @app_commands.describe(module=T("xumari.reload.module"))
    @require_admin()
    async def reload(self, interaction: discord.Interaction[Any], module: str) -> None:
        tr = self.tr(interaction)
        if module not in EXTENSIONS or not self.bot.features.loaded(module):
            raise UserError("admin.reload.unknown", module=module)
        await self.bot.reload_extension(EXTENSIONS[module])
        await self.bot.respond_notice(interaction, title=tr("admin.reload.done", module=module))
        await self.bot.audit.log(interaction.guild, "commands", "system.reload", title=tr("admin.reload.done", module=module),
                                 emoji="refresh", actor=interaction.user)

    @reload.autocomplete("module")
    async def _reload_autocomplete(self, interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=key, value=key)
            for key in EXTENSIONS
            if self.bot.features.loaded(key) and current.lower() in key
        ][:25]

    @group.command(name="language", description=T("xumari.language"))
    @app_commands.describe(locale=T("xumari.language.locale"))
    @require_admin()
    async def language(self, interaction: discord.Interaction[Any], locale: Literal["de", "en"]) -> None:
        assert interaction.guild_id is not None
        await self.bot.set_guild_locale(interaction.guild_id, locale)
        tr = self.bot.i18n.translator(locale)
        await self.bot.respond_notice(interaction, title=tr("admin.language.done", locale=locale.upper()))

    @group.command(name="diagnose", description=T("xumari.diagnose"))
    @require_admin()
    async def diagnose(self, interaction: discord.Interaction[Any]) -> None:
        assert interaction.guild is not None
        tr = self.tr(interaction)
        guild = interaction.guild
        cfg = self.bot.config
        problems: list[str] = []

        def check_channel(label: str, channel_id: int | None) -> None:
            if channel_id and guild.get_channel(channel_id) is None:
                problems.append(tr("admin.diagnose.missing_channel", label=label, id=channel_id))

        def check_role(label: str, role_id: int | None) -> None:
            if role_id and guild.get_role(role_id) is None:
                problems.append(tr("admin.diagnose.missing_role", label=label, id=role_id))

        check_channel("logging.default_channel_id", cfg.logging.default_channel_id)
        for key, cid_ in cfg.logging.channels.items():
            check_channel(f"logging.channels.{key}", cid_)
        check_channel("tickets.panel_channel_id", cfg.tickets.panel_channel_id)
        check_channel("tickets.closed_category_id", cfg.tickets.closed_category_id)
        check_channel("tickets.reviews.channel_id", cfg.tickets.reviews.channel_id)
        for cat in cfg.tickets.categories:
            check_channel(f"tickets.categories.{cat.key}.discord_category_id", cat.discord_category_id)
            for rid in cat.support_role_ids:
                check_role(f"tickets.categories.{cat.key}.support_role_ids", rid)
        for section, ids in (("roles.admin", cfg.roles.admin), ("roles.staff", cfg.roles.staff),
                             ("roles.designer", cfg.roles.designer)):
            for rid in ids:
                check_role(section, rid)
        check_role("roles.customer", cfg.roles.customer)
        check_channel("downloads.default_channel_id", cfg.downloads.default_channel_id)
        check_channel("levels.level_up_channel_id", cfg.levels.level_up_channel_id)
        for level, rid in cfg.levels.rewards.items():
            check_role(f"levels.rewards.{level}", rid)

        me = guild.me
        needed = discord.Permissions(
            manage_channels=True, manage_roles=True, manage_messages=True, send_messages=True,
            embed_links=True, attach_files=True, read_message_history=True, add_reactions=True,
            manage_nicknames=True, kick_members=True,
        )
        missing_perms = [name for name, value in needed if value and not getattr(me.guild_permissions, name)]
        if missing_perms:
            problems.append(tr("admin.diagnose.missing_perms", perms=", ".join(f"`{p}`" for p in missing_perms)))

        banners = len(self.bot.assets.configured())
        b = CV2(self.bot.theme)
        b.heading(tr("admin.diagnose.title"), emoji="search")
        b.bullets(
            [
                Bullet(tr("admin.diagnose.banners"), str(banners), emoji="image"),
                Bullet(tr("admin.diagnose.emojis"), str(len(cfg.design.emojis)), emoji="star"),
                Bullet(tr("admin.diagnose.problems"), str(len(problems)), emoji="warning" if problems else "success"),
            ]
        )
        if problems:
            b.separator()
            b.text("\n".join(f"{self.bot.icons['error']} {p}" for p in problems[:40]))
        else:
            b.quote(tr("admin.diagnose.all_good"))
        await interaction.response.send_message(view=b.build(), ephemeral=True)
