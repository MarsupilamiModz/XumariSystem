"""Download system: create/edit/post/delete downloads with YouTube preview, click tracking and statistics.

Flow (``/download create`` or control panel → "Download erstellen"):
    1. Modal (title, download link, password, YouTube link, description)
    2. Setup view: category + target channel + optional role, details modal (version, size, EN text, own image)
    3. Live preview (ephemeral, final layout) with "Post", "Edit", "Cancel"
Every change of a posted download updates the posted message(s) automatically.
"""

from __future__ import annotations

import math
from typing import Any

import discord
from discord import app_commands, ui

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.controlpanel import ControlSection
from ...core.errors import NotFound, PermissionDenied, UserError
from ...core.i18n import Translator
from ...core.logger import get_logger
from ...core.permissions import ensure_staff, has_any_role, is_staff, require_staff
from ...core.router import ComponentContext, cid, component
from ...core.utils import format_number, is_url, progress_bar, truncate, ts
from ...services.downloads import Download, DownloadService
from ...services.youtube import parse_video_id, resolve_thumbnail, watch_url
from ...ui.builder import CV2, Bullet, button, select_option, string_select
from ...ui.layouts import build_confirm, build_download
from ...ui.modals import FormModal, TextField

_log = get_logger("downloads")
PAGE_SIZE = 10


class DownloadsCog(XCog, name="Downloads"):
    feature = "downloads"

    group = app_commands.Group(name="download", description=T("download"), guild_only=True)

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.service = DownloadService(bot.db)
        self._thumbnails: dict[str, str] = {}

    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.bot.config.downloads

    async def on_load(self) -> None:
        self.bot.control_sections.register(ControlSection("downloads", "download", 50, "downloads", self._cp_section))

    async def on_unload(self) -> None:
        self.bot.control_sections.unregister("downloads")

    # ================================================================== rendering
    def category_label(self, key: str | None, locale: str) -> str | None:
        category = self.cfg.category(key)
        return category.name(locale) if category else key

    def meta_line(self, d: Download, locale: str) -> str | None:
        parts = [self.category_label(d.category, locale), f"v{d.version.lstrip('vV')}" if d.version else None, d.file_size]
        return " · ".join(p for p in parts if p) or None

    def gated(self, d: Download) -> bool:
        return self.cfg.track_clicks or d.required_role_id is not None

    async def preview_image(self, d: Download) -> str | None:
        if d.preview_image_url:
            return d.preview_image_url
        if not d.youtube_id:
            return None
        if d.youtube_id not in self._thumbnails:
            self._thumbnails[d.youtube_id] = await resolve_thumbnail(self.bot.http_session, d.youtube_id)
        return self._thumbnails[d.youtube_id]

    async def render(self, d: Download, locale: str, *, admin_rows: list[list[ui.Item[Any]]] | None = None
                     ) -> ui.LayoutView:
        tr = self.bot.i18n.translator(locale)
        published = d.published_at or d.created_at
        footer = tr("downloads.layout.footer", shop=self.bot.config.bot.shop_name, date=published.strftime("%d.%m.%Y"))
        return build_download(
            self.bot.theme, tr, download_id=d.id, title=d.title, meta_line=self.meta_line(d, locale),
            description=d.description(locale), download_url=d.download_url, password=d.password,
            youtube_url=watch_url(d.youtube_id) if d.youtube_id else None, preview_image=await self.preview_image(d),
            footer=footer, gated=self.gated(d), admin_rows=admin_rows,
        )

    # ================================================================== posting
    def _target_channel(self, d: Download) -> discord.TextChannel:
        guild = self.bot.get_guild(d.guild_id)
        channel = guild.get_channel(d.channel_id or self.cfg.default_channel_id or 0) if guild else None
        if not isinstance(channel, (discord.TextChannel, discord.NewsChannel)):
            raise UserError("downloads.errors.no_channel")
        return channel  # type: ignore[return-value]

    async def post(self, d: Download, actor: discord.abc.User | None) -> Download:
        if d.is_posted:
            await self.unpost(d)
            d = await self.service.get(d.id)
        channel = self._target_channel(d)
        locale = self.bot.guild_locale(d.guild_id)
        message = await channel.send(view=await self.render(d, locale), allowed_mentions=discord.AllowedMentions.none())
        embed_id = None
        if self.cfg.send_video_embed and d.youtube_id:
            embed = await channel.send(watch_url(d.youtube_id))
            embed_id = embed.id
        d = await self.service.update(d.id, channel_id=channel.id, message_id=message.id, embed_message_id=embed_id,
                                      published_at=d.published_at or message.created_at)
        await self._log(d, "post", actor, extra=[message.jump_url])
        return d

    async def sync_posted(self, d: Download) -> None:
        """Re-render the posted message(s) after a change."""
        if not d.is_posted or not d.channel_id:
            return
        channel = self.bot.get_channel(d.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.NewsChannel)):
            return
        locale = self.bot.guild_locale(d.guild_id)
        try:
            await channel.get_partial_message(d.message_id).edit(view=await self.render(d, locale))  # type: ignore[arg-type]
        except discord.NotFound:
            await self.service.update(d.id, message_id=None, embed_message_id=None)
            return
        wants_embed = self.cfg.send_video_embed and d.youtube_id
        try:
            if d.embed_message_id and wants_embed:
                await channel.get_partial_message(d.embed_message_id).edit(content=watch_url(d.youtube_id or ""))
            elif d.embed_message_id and not wants_embed:
                await channel.get_partial_message(d.embed_message_id).delete()
                await self.service.update(d.id, embed_message_id=None)
            elif wants_embed and not d.embed_message_id:
                embed = await channel.send(watch_url(d.youtube_id or ""))
                await self.service.update(d.id, embed_message_id=embed.id)
        except discord.HTTPException:
            _log.debug("Could not sync video embed of download %s", d.id)

    async def unpost(self, d: Download) -> None:
        channel = self.bot.get_channel(d.channel_id or 0)
        if isinstance(channel, (discord.TextChannel, discord.NewsChannel)):
            for message_id in (d.message_id, d.embed_message_id):
                if message_id:
                    try:
                        await channel.get_partial_message(message_id).delete()
                    except discord.HTTPException:
                        _log.debug("Message %s already deleted", message_id)
        await self.service.update(d.id, message_id=None, embed_message_id=None)

    async def _log(self, d: Download, action: str, actor: discord.abc.User | None, extra: list[str] | None = None) -> None:
        guild = self.bot.get_guild(d.guild_id)
        tr = self.gtr(d.guild_id)
        await self.bot.audit.log(
            guild, "downloads", f"download.{action}", title=tr(f"downloads.log.{action}"), emoji="download",
            actor=actor, lines=[f"**{d.title}** (#{d.id})", *(extra or [])], details={"download_id": d.id},
        )

    # ================================================================== create / edit modal
    def _main_fields(self, tr: Translator, d: Download | None = None) -> list[TextField]:
        return [
            TextField(key="title", label=tr("downloads.fields.title"), max_length=100, default=d.title if d else None),
            TextField(key="download_url", label=tr("downloads.fields.download_url"), max_length=500,
                      placeholder="https://", default=d.download_url if d else None),
            TextField(key="password", label=tr("downloads.fields.password"), required=False, max_length=200,
                      description=tr("downloads.fields.password_hint"), default=d.password if d else None),
            TextField(key="youtube_url", label=tr("downloads.fields.youtube_url"), required=False, max_length=200,
                      placeholder="https://youtu.be/…", default=d.youtube_url if d else None),
            TextField(key="description_de", label=tr("downloads.fields.description"), paragraph=True, required=False,
                      max_length=1500, default=d.description_de if d else None),
        ]

    @staticmethod
    def _main_values(values: dict[str, Any]) -> dict[str, Any]:
        url = str(values["download_url"]).strip()
        if not is_url(url):
            raise UserError("errors.invalid_url")
        youtube_raw = str(values.get("youtube_url") or "").strip()
        youtube_id = parse_video_id(youtube_raw) if youtube_raw else None
        if youtube_raw and youtube_id is None:
            raise UserError("downloads.errors.youtube")
        return {
            "title": str(values["title"]).strip(),
            "download_url": url,
            "password": str(values.get("password") or "").strip() or None,
            "youtube_url": youtube_raw or None,
            "youtube_id": youtube_id,
            "description_de": str(values.get("description_de") or "").strip() or None,
        }

    def _create_modal(self, interaction: discord.Interaction[Any]) -> FormModal:
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            data = self._main_values(values)
            d = await self.service.create(modal_interaction.guild_id or 0, modal_interaction.user.id,
                                          category=self.cfg.categories[0].key if self.cfg.categories else None,
                                          channel_id=self.cfg.default_channel_id, **data)
            await self._log(d, "create", modal_interaction.user)
            await modal_interaction.response.send_message(view=await self.render_setup(modal_interaction, d),
                                                          ephemeral=True)

        return FormModal(self.bot, title=tr("downloads.create_title"), fields=self._main_fields(tr), on_submit=submitted)

    def _edit_modal(self, interaction: discord.Interaction[Any], d: Download) -> FormModal:
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            updated = await self.service.update(d.id, **self._main_values(values))
            await self.sync_posted(updated)
            await self._log(updated, "edit", modal_interaction.user)
            if modal_interaction.message is not None and modal_interaction.message.flags.ephemeral:
                await modal_interaction.response.edit_message(view=await self.render_setup(modal_interaction, updated))
            else:
                await modal_interaction.response.send_message(view=await self.render_setup(modal_interaction, updated),
                                                              ephemeral=True)

        return FormModal(self.bot, title=tr("downloads.edit_title"), fields=self._main_fields(tr, d), on_submit=submitted)

    # ================================================================== setup view (step 2)
    async def render_setup(self, interaction: discord.Interaction[Any], d: Download) -> ui.LayoutView:
        tr = self.tr(interaction)
        guild = interaction.guild
        b = CV2(self.bot.theme)
        b.banner("downloads")
        b.heading(tr("downloads.setup.title"), emoji="settings", subline=f"#{d.id} · {d.title}")
        b.quote(tr("downloads.setup.subtitle"))
        channel_id = d.channel_id or self.cfg.default_channel_id
        image_source = tr("downloads.setup.image_custom") if d.preview_image_url else (
            tr("downloads.setup.image_youtube") if d.youtube_id else tr("downloads.setup.image_none"))
        b.bullets([
            Bullet(tr("downloads.setup.category"), self.category_label(d.category, tr.locale) or "—", emoji="category"),
            Bullet(tr("downloads.setup.channel"), f"<#{channel_id}>" if channel_id else "—", emoji="message"),
            Bullet(tr("downloads.setup.role"), f"<@&{d.required_role_id}>" if d.required_role_id else tr("common.none"),
                   emoji="role"),
            Bullet(tr("downloads.setup.details"), self.meta_line(d, tr.locale) or "—", emoji="version"),
            Bullet(tr("downloads.setup.image"), image_source, emoji="image"),
            Bullet(tr("downloads.setup.status"),
                   tr("downloads.setup.posted", channel=f"<#{d.channel_id}>") if d.is_posted
                   else tr("downloads.setup.draft"), emoji="info"),
        ])
        b.separator()
        if self.cfg.categories:
            b.actions(string_select(
                cid("dl.cat", d.id),
                [select_option(self.bot.theme, c.name(tr.locale), c.key, description=c.name(tr.other().locale),
                               emoji=c.emoji, default=c.key == d.category) for c in self.cfg.categories[:25]],
                placeholder=tr("downloads.setup.pick_category"),
            ))
        channel_default = [discord.Object(id=channel_id)] if channel_id and guild and guild.get_channel(channel_id) else []
        b.actions(ui.ChannelSelect(custom_id=cid("dl.chan", d.id), placeholder=tr("downloads.setup.pick_channel"),
                                   channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                                   default_values=channel_default))
        role_default = [discord.Object(id=d.required_role_id)] if d.required_role_id and guild and \
            guild.get_role(d.required_role_id) else []
        b.actions(ui.RoleSelect(custom_id=cid("dl.role", d.id), placeholder=tr("downloads.setup.pick_role"),
                                min_values=0, max_values=1, default_values=role_default))
        b.actions(
            button(self.bot.theme, tr("downloads.setup.edit"), emoji="edit", custom_id=cid("dl.edit", d.id)),
            button(self.bot.theme, tr("downloads.setup.details_button"), emoji="version", custom_id=cid("dl.details", d.id)),
            button(self.bot.theme, tr("downloads.setup.preview"), emoji="preview", custom_id=cid("dl.preview", d.id)),
        )
        b.actions(
            button(self.bot.theme, tr("downloads.setup.update") if d.is_posted else tr("downloads.setup.post"),
                   emoji="send", custom_id=cid("dl.update" if d.is_posted else "dl.post", d.id),
                   style=discord.ButtonStyle.success),
            button(self.bot.theme, tr("downloads.setup.repost"), emoji="refresh", custom_id=cid("dl.post", d.id),
                   disabled=not d.is_posted),
            button(self.bot.theme, tr("common.delete") if d.is_posted else tr("common.cancel"), emoji="delete",
                   custom_id=cid("dl.del", d.id), style=discord.ButtonStyle.danger),
        )
        return b.build()

    async def _staff_download(self, interaction: discord.Interaction[Any], download_id: int) -> Download:
        ensure_staff(self.bot, interaction.user)
        return await self.service.get(download_id, interaction.guild_id)

    async def _refresh_setup(self, interaction: discord.Interaction[Any], d: Download) -> None:
        await self.sync_posted(d)
        await interaction.response.edit_message(view=await self.render_setup(interaction, d))

    @component("dl.setup")
    async def on_setup(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        await interaction.response.send_message(view=await self.render_setup(interaction, d), ephemeral=True)

    @component("dl.cat")
    async def on_category(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        d = await self.service.update(d.id, category=ctx.values[0] if ctx.values else None)
        await self._refresh_setup(interaction, d)

    @component("dl.chan")
    async def on_channel(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        if not ctx.resolved_values:
            await interaction.response.edit_message(view=await self.render_setup(interaction, d))
            return
        if d.is_posted and d.channel_id != ctx.resolved_values[0].id:
            # moving a posted download → remove old messages; "Post" publishes it in the new channel
            await self.unpost(d)
        d = await self.service.update(d.id, channel_id=ctx.resolved_values[0].id)
        await interaction.response.edit_message(view=await self.render_setup(interaction, d))

    @component("dl.role")
    async def on_role(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        role = ctx.resolved_values[0] if ctx.resolved_values else None
        d = await self.service.update(d.id, required_role_id=role.id if role else None)
        await self._refresh_setup(interaction, d)

    @component("dl.edit")
    async def on_edit(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        await interaction.response.send_modal(self._edit_modal(interaction, d))

    @component("dl.details")
    async def on_details(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            image = str(values.get("preview_image_url") or "").strip()
            if image and not is_url(image):
                raise UserError("errors.invalid_url")
            tags = [t.strip() for t in str(values.get("tags") or "").split(",") if t.strip()][:10]
            updated = await self.service.update(
                d.id, version=str(values.get("version") or "").strip() or None,
                file_size=str(values.get("file_size") or "").strip() or None,
                description_en=str(values.get("description_en") or "").strip() or None,
                preview_image_url=image or None, tags=tags,
            )
            await self.sync_posted(updated)
            await self._log(updated, "edit", modal_interaction.user)
            await modal_interaction.response.edit_message(view=await self.render_setup(modal_interaction, updated))

        await interaction.response.send_modal(FormModal(self.bot, title=tr("downloads.setup.details_button"), fields=[
            TextField(key="version", label=tr("downloads.fields.version"), required=False, max_length=30,
                      placeholder="1.2", default=d.version),
            TextField(key="file_size", label=tr("downloads.fields.file_size"), required=False, max_length=30,
                      placeholder="45 MB", default=d.file_size),
            TextField(key="description_en", label=tr("downloads.fields.description_en"), paragraph=True,
                      required=False, max_length=1500, default=d.description_en),
            TextField(key="preview_image_url", label=tr("downloads.fields.preview_image"), required=False,
                      max_length=500, description=tr("downloads.fields.preview_image_hint"),
                      default=d.preview_image_url),
            TextField(key="tags", label=tr("downloads.fields.tags"), required=False, max_length=200,
                      placeholder="fivem, esx, script", default=", ".join(d.tags) or None),
        ], on_submit=submitted))

    @component("dl.preview")
    async def on_preview(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        tr = self.tr(interaction)
        admin_row: list[ui.Item[Any]] = [
            button(self.bot.theme, tr("downloads.setup.update") if d.is_posted else tr("downloads.setup.post"),
                   emoji="send", custom_id=cid("dl.update" if d.is_posted else "dl.post", d.id),
                   style=discord.ButtonStyle.success),
            button(self.bot.theme, tr("common.edit"), emoji="edit", custom_id=cid("dl.edit", d.id)),
            button(self.bot.theme, tr("common.cancel"), emoji="cancel", custom_id=cid("dl.del", d.id),
                   style=discord.ButtonStyle.danger),
        ]
        await interaction.response.send_message(
            view=await self.render(d, self.bot.guild_locale(d.guild_id), admin_rows=[admin_row]), ephemeral=True)

    @component("dl.post")
    async def on_post(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        await interaction.response.defer()
        d = await self.post(d, interaction.user)
        tr = self.tr(interaction)
        await interaction.edit_original_response(view=await self.render_setup(interaction, d))
        await interaction.followup.send(view=self.bot.notice(kind="success", title=tr(
            "downloads.posted", channel=f"<#{d.channel_id}>")), ephemeral=True)

    @component("dl.update")
    async def on_update(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        await self.sync_posted(d)
        await self._log(d, "update", interaction.user)
        await interaction.response.edit_message(view=await self.render_setup(interaction, d))
        await interaction.followup.send(view=self.bot.notice(kind="success", title=self.tr(interaction)(
            "downloads.updated")), ephemeral=True)

    @component("dl.del")
    async def on_delete(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        tr = self.tr(interaction)
        await interaction.response.edit_message(view=build_confirm(
            self.bot.theme, tr, title=tr("downloads.delete_title"),
            description=tr("downloads.delete_text", title=d.title), confirm_id=cid("dl.del_ok", d.id),
            cancel_id=cid("dl.back", d.id), danger=True))

    @component("dl.back")
    async def on_back(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        await interaction.response.edit_message(view=await self.render_setup(interaction, d))

    @component("dl.del_ok")
    async def on_delete_ok(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, ctx.int_arg(0))
        await self.unpost(d)
        await self.service.delete(d.id)
        await self._log(d, "delete", interaction.user)
        await interaction.response.edit_message(view=self.bot.notice(kind="success", title=self.tr(interaction)(
            "downloads.deleted", title=d.title)))

    # ================================================================== public buttons
    async def _check_access(self, interaction: discord.Interaction[Any], d: Download) -> None:
        if d.required_role_id and not is_staff(self.bot, interaction.user) and \
                not has_any_role(interaction.user, [d.required_role_id]):
            raise PermissionDenied("downloads.errors.role_required", role=f"<@&{d.required_role_id}>")

    @component("dl.get")
    async def on_get(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self.service.get(ctx.int_arg(0), interaction.guild_id)
        await self._check_access(interaction, d)
        clicks = await self.service.register_click(d, interaction.user.id)
        tr = self.tr(interaction)
        rows: list[list[ui.Item[Any]]] = [[button(self.bot.theme, tr("downloads.layout.download_button"),
                                                  emoji="download", url=d.download_url)]]
        bullets = [Bullet(tr("downloads.layout.download"), f"[{tr('downloads.layout.download_link')}]({d.download_url})",
                          emoji="download")]
        if d.password:
            shown = f"[{tr('downloads.layout.password_link')}]({d.password})" if d.password.startswith("http") \
                else f"`{d.password}`"
            bullets.append(Bullet(tr("downloads.layout.password"), shown, emoji="password"))
        await interaction.response.send_message(view=self.bot.notice(
            kind="success", title=d.title, bullets=bullets, footer=tr("downloads.clicks", count=clicks), rows=rows),
            ephemeral=True)

    @component("dl.pw")
    async def on_password(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self.service.get(ctx.int_arg(0), interaction.guild_id)
        await self._check_access(interaction, d)
        if not d.password:
            raise NotFound()
        tr = self.tr(interaction)
        await interaction.response.send_message(view=self.bot.notice(
            kind="info", title=tr("downloads.layout.password"), description=f"```\n{d.password}\n```",
            footer=tr("downloads.password_hint")), ephemeral=True)

    # ================================================================== list (shared by command + control panel)
    async def render_list(self, interaction: discord.Interaction[Any], page: int) -> ui.LayoutView:
        tr = self.tr(interaction)
        b = CV2(self.bot.theme)
        b.banner("downloads")
        await self._list_body(interaction, b, tr, page)
        return b.build()

    async def _list_body(self, interaction: discord.Interaction[Any], b: CV2, tr: Translator, page: int,
                         *, heading: bool = True) -> None:
        _, total = await self.service.list_downloads(interaction.guild_id or 0, limit=1)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(0, min(page, pages - 1))
        items, _ = await self.service.list_downloads(interaction.guild_id or 0, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        if heading:
            b.heading(tr("downloads.list_title"), emoji="download", subline=tr("downloads.list_count", count=total))
        else:
            b.quote(tr("downloads.list_count", count=total))
        if items:
            b.text("\n".join(
                f"{self.bot.icons['toggle_on' if d.is_posted else 'toggle_off']} `#{d.id}` **{truncate(d.title, 60)}**"
                f" · {self.category_label(d.category, tr.locale) or '—'} · "
                f"{tr('downloads.clicks', count=format_number(d.click_count, tr.locale))}" for d in items))
            b.actions(string_select(
                cid("dl.pick"),
                [select_option(self.bot.theme, truncate(d.title, 90), str(d.id), emoji="download",
                               description=self.meta_line(d, tr.locale) or f"#{d.id}") for d in items],
                placeholder=tr("downloads.pick"),
            ))
        else:
            b.quote(tr("downloads.none"))
        b.footer(tr("common.page", page=page + 1, pages=pages))
        b.actions(
            button(self.bot.theme, tr("downloads.new"), emoji="add", custom_id=cid("dl.new"),
                   style=discord.ButtonStyle.success),
            button(self.bot.theme, tr("common.previous"), emoji="back", custom_id=cid("dl.page", page - 1),
                   disabled=page <= 0),
            button(self.bot.theme, tr("common.next"), emoji="next", custom_id=cid("dl.page", page + 1),
                   disabled=page >= pages - 1),
            button(self.bot.theme, tr("downloads.stats_button"), emoji="stats", custom_id=cid("dl.stats")),
        )

    async def _cp_section(self, interaction: discord.Interaction[Any], b: CV2, tr: Translator) -> None:
        await self._list_body(interaction, b, tr, 0, heading=False)

    @component("dl.page")
    async def on_page(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_staff(self.bot, interaction.user)
        await interaction.response.edit_message(view=await self.render_list(interaction, ctx.int_arg(0)))

    @component("dl.pick")
    async def on_pick(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        d = await self._staff_download(interaction, int(ctx.values[0]))
        await interaction.response.send_message(view=await self.render_setup(interaction, d), ephemeral=True)

    @component("dl.new")
    async def on_new(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_staff(self.bot, interaction.user)
        await interaction.response.send_modal(self._create_modal(interaction))

    @component("dl.stats")
    async def on_stats(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        ensure_staff(self.bot, interaction.user)
        await interaction.response.send_message(
            view=await self.render_stats(interaction.guild_id or 0, self.bot.locale_for(interaction)), ephemeral=True)

    async def render_stats(self, guild_id: int, locale: str) -> ui.LayoutView:
        tr = self.bot.i18n.translator(locale)
        data = await self.service.stats(guild_id)
        icons = self.bot.icons
        b = CV2(self.bot.theme)
        b.banner("downloads")
        b.heading(tr("downloads.stats.title"), emoji="stats")
        b.bullets([
            Bullet(tr("downloads.stats.downloads"), f"{data['downloads']} ({tr('downloads.stats.posted', count=data['posted'])})",
                   emoji="download"),
            Bullet(tr("downloads.stats.clicks_total"), format_number(data["clicks"], locale), emoji="stats"),
            Bullet(tr("downloads.stats.clicks_period"),
                   tr("downloads.stats.periods", day=data["day"], week=data["week"], month=data["month"]), emoji="clock"),
            Bullet(tr("downloads.stats.users"), str(data["users_month"]), emoji="users"),
        ])
        if data["top"] and data["top"][0]["click_count"]:
            b.block_break()
            b.text(f"**{tr('downloads.stats.top')}**")
            b.text("\n".join(f"`#{i}` **{truncate(t['title'], 60)}** · {format_number(t['click_count'], locale)}"
                             for i, t in enumerate(data["top"], start=1) if t["click_count"]))
        if data["daily"]:
            b.block_break()
            b.text(f"**{tr('downloads.stats.daily')}**")
            peak = max(c for _, c in data["daily"]) or 1
            b.text("\n".join(f"`{day:%d.%m.}` {progress_bar(c, peak, 10, icons['bar_full'], icons['bar_empty'])} {c}"
                             for day, c in data["daily"]))
        return b.build()

    # ================================================================== slash commands
    async def _autocomplete(self, interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[int]]:
        items = await self.service.search(interaction.guild_id or 0, current)
        return [app_commands.Choice(name=truncate(f"#{d.id} {d.title}", 100), value=d.id) for d in items]

    @group.command(name="create", description=T("download.create"))
    @require_staff()
    async def create_cmd(self, interaction: discord.Interaction[Any]) -> None:
        await interaction.response.send_modal(self._create_modal(interaction))

    @group.command(name="edit", description=T("download.edit"))
    @app_commands.describe(download=T("download.download"))
    @require_staff()
    async def edit_cmd(self, interaction: discord.Interaction[Any], download: int) -> None:
        d = await self.service.get(download, interaction.guild_id)
        await interaction.response.send_message(view=await self.render_setup(interaction, d), ephemeral=True)

    edit_cmd.autocomplete("download")(_autocomplete)

    @group.command(name="delete", description=T("download.delete"))
    @app_commands.describe(download=T("download.download"))
    @require_staff()
    async def delete_cmd(self, interaction: discord.Interaction[Any], download: int) -> None:
        d = await self.service.get(download, interaction.guild_id)
        tr = self.tr(interaction)
        await interaction.response.send_message(view=build_confirm(
            self.bot.theme, tr, title=tr("downloads.delete_title"), description=tr("downloads.delete_text", title=d.title),
            confirm_id=cid("dl.del_ok", d.id), cancel_id=cid("dismiss"), danger=True), ephemeral=True)

    delete_cmd.autocomplete("download")(_autocomplete)

    @group.command(name="list", description=T("download.list"))
    @require_staff()
    async def list_cmd(self, interaction: discord.Interaction[Any]) -> None:
        await interaction.response.send_message(view=await self.render_list(interaction, 0), ephemeral=True)

    @group.command(name="post", description=T("download.post"))
    @app_commands.describe(download=T("download.download"), channel=T("shop.channel"))
    @require_staff()
    async def post_cmd(self, interaction: discord.Interaction[Any], download: int,
                       channel: discord.TextChannel | None = None) -> None:
        d = await self.service.get(download, interaction.guild_id)
        if channel is not None:
            if d.is_posted and d.channel_id != channel.id:
                await self.unpost(d)
            d = await self.service.update(d.id, channel_id=channel.id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        d = await self.post(d, interaction.user)
        await interaction.followup.send(view=self.bot.notice(kind="success", title=self.tr(interaction)(
            "downloads.posted", channel=f"<#{d.channel_id}>")), ephemeral=True)

    post_cmd.autocomplete("download")(_autocomplete)

    @group.command(name="info", description=T("download.info"))
    @app_commands.describe(download=T("download.download"))
    @require_staff()
    async def info_cmd(self, interaction: discord.Interaction[Any], download: int) -> None:
        d = await self.service.get(download, interaction.guild_id)
        tr = self.tr(interaction)
        b = CV2(self.bot.theme)
        b.heading(d.title, emoji="download", subline=f"#{d.id}")
        b.bullets([
            Bullet(tr("downloads.clicks_label"), format_number(d.click_count, tr.locale), emoji="stats"),
            Bullet(tr("downloads.created"), ts(d.created_at, "f"), emoji="calendar"),
            Bullet(tr("downloads.created_by"), f"<@{d.created_by}>" if d.created_by else "—", emoji="user"),
            Bullet(tr("downloads.setup.status"),
                   f"https://discord.com/channels/{d.guild_id}/{d.channel_id}/{d.message_id}" if d.is_posted
                   else tr("downloads.setup.draft"), emoji="info"),
        ])
        b.actions(button(self.bot.theme, tr("downloads.manage"), emoji="settings", custom_id=cid("dl.setup", d.id)))
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    info_cmd.autocomplete("download")(_autocomplete)
