"""``/cv2`` – interactive Components V2 layout builder with templates stored as JSON in the database."""

from __future__ import annotations

import io
import json
import re
from typing import Any

import discord
from discord import app_commands, ui
from pydantic import ValidationError

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import NotFound, UserError
from ...core.i18n import Translator
from ...core.logger import get_logger
from ...core.permissions import ensure_admin, require_admin
from ...core.router import ComponentContext, cid, component
from ...core.utils import truncate, ts
from ...services.cv2 import (
    BLOCK_TYPES,
    MAX_BLOCKS,
    BannerBlock,
    ButtonsBlock,
    HeadingBlock,
    ImageBlock,
    SeparatorBlock,
    Template,
    TemplateData,
    TemplateService,
    TextBlock,
    fits,
    make_block,
    render_template,
)
from ...ui.builder import CV2, StaticLayoutView, button, select_option, string_select
from ...ui.layouts import build_confirm
from ...ui.modals import FormModal, ModalField, SelectField, TextField

_log = get_logger("cv2")
EDITOR_ACCENT = 0x2B2D31
MESSAGE_LINK = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
BLOCK_EMOJI = {"heading": "text", "text": "message", "banner": "image", "image": "image", "separator": "separator",
               "buttons": "button"}


class CV2Cog(XCog, name="CV2Builder"):
    feature = "cv2"

    group = app_commands.Group(name="cv2", description=T("cv2"), guild_only=True,
                               default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.templates = TemplateService(bot.db)

    # ================================================================== rendering
    def _summary(self, tr: Translator, block: Any) -> str:
        if isinstance(block, HeadingBlock):
            return block.text
        if isinstance(block, TextBlock):
            return block.content.replace("\n", " ")
        if isinstance(block, BannerBlock):
            return block.source
        if isinstance(block, ImageBlock):
            return block.url
        if isinstance(block, SeparatorBlock):
            return tr("cv2.blocks.separator_large" if block.large else "cv2.blocks.separator_small")
        if isinstance(block, ButtonsBlock):
            return ", ".join(i.label for i in block.items)
        return ""

    async def render_builder(self, interaction: discord.Interaction[Any], template: Template, *,
                             focus: int | None = None, notice: str | None = None) -> ui.LayoutView:
        tr = self.tr(interaction)
        view = StaticLayoutView()
        if template.data.blocks:
            render_template(self.bot.theme, template.data, view=view)
        else:
            CV2(self.bot.theme, view=view).quote(tr("cv2.builder.empty")).build()
        e = CV2(self.bot.theme, accent=EDITOR_ACCENT, view=view)
        e.heading(tr("cv2.builder.title", name=template.name), emoji="cv2", level=3,
                  subline=tr("cv2.builder.subline", blocks=len(template.data.blocks), max=MAX_BLOCKS,
                             accent=template.data.accent or self.bot.config.design.accent_color))
        if notice:
            e.small(notice)
        blocks = template.data.blocks
        if focus is not None and 0 <= focus < len(blocks):
            block = blocks[focus]
            e.quote(f"**#{focus + 1} · {tr(f'cv2.blocks.{block.type}')}**\n{truncate(self._summary(tr, block), 300)}")
            e.actions(
                button(self.bot.theme, tr("common.back"), emoji="back", custom_id=cid("cv2.back", template.id)),
                button(self.bot.theme, tr("common.edit"), emoji="edit", custom_id=cid("cv2.edit", template.id, focus)),
                button(self.bot.theme, tr("cv2.builder.up"), emoji="first", custom_id=cid("cv2.up", template.id, focus),
                       disabled=focus == 0),
                button(self.bot.theme, tr("cv2.builder.down"), emoji="last",
                       custom_id=cid("cv2.down", template.id, focus), disabled=focus >= len(blocks) - 1),
                button(self.bot.theme, tr("common.delete"), emoji="delete", custom_id=cid("cv2.rm", template.id, focus),
                       style=discord.ButtonStyle.danger),
            )
            return e.build()
        other = tr.other()
        e.actions(string_select(
            cid("cv2.add", template.id),
            [select_option(self.bot.theme, tr(f"cv2.blocks.{k}"), k, description=other(f"cv2.blocks.{k}"),
                           emoji=BLOCK_EMOJI[k]) for k in BLOCK_TYPES],
            placeholder=tr("cv2.builder.add"), disabled=len(blocks) >= MAX_BLOCKS,
        ))
        if blocks:
            e.actions(string_select(
                cid("cv2.pick", template.id),
                [select_option(self.bot.theme, f"#{i + 1} {tr(f'cv2.blocks.{b.type}')}", str(i),
                               description=truncate(self._summary(tr, b), 90) or None, emoji=BLOCK_EMOJI[b.type])
                 for i, b in enumerate(blocks)],
                placeholder=tr("cv2.builder.pick"),
            ))
        e.actions(
            button(self.bot.theme, tr("cv2.builder.color"), emoji="color", custom_id=cid("cv2.color", template.id)),
            button(self.bot.theme, tr("cv2.builder.rename"), emoji="edit", custom_id=cid("cv2.rename", template.id)),
            button(self.bot.theme, tr("cv2.builder.copy"), emoji="save", custom_id=cid("cv2.copy", template.id)),
        )
        e.actions(ui.ChannelSelect(custom_id=cid("cv2.send", template.id), placeholder=tr("cv2.builder.send"),
                                   channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                                   disabled=not blocks))
        e.actions(
            button(self.bot.theme, tr("cv2.builder.update_link"), emoji="link", custom_id=cid("cv2.update", template.id),
                   disabled=not blocks),
            button(self.bot.theme, tr("cv2.builder.sync"), emoji="refresh", custom_id=cid("cv2.sync", template.id),
                   disabled=not blocks),
            button(self.bot.theme, tr("common.delete"), emoji="delete", custom_id=cid("cv2.del", template.id),
                   style=discord.ButtonStyle.danger),
        )
        return e.build()

    async def _template(self, interaction: discord.Interaction[Any], template_id: int) -> Template:
        ensure_admin(self.bot, interaction.user)
        return await self.templates.get(template_id, interaction.guild_id)

    async def _store(self, interaction: discord.Interaction[Any], template: Template, data: TemplateData, *,
                     focus: int | None = None, notice: str | None = None) -> None:
        """Validate size, save and re-render the builder."""
        if not fits(self.bot.theme, data):
            raise UserError("cv2.errors.too_large")
        template.data = data
        template = await self.templates.save(template, interaction.user.id)
        await interaction.response.edit_message(
            view=await self.render_builder(interaction, template, focus=focus, notice=notice))

    # ================================================================== block modals
    def _block_fields(self, tr: Translator, kind: str, block: Any = None) -> list[ModalField]:
        def opt(label_key: str, value: str, selected: bool) -> discord.SelectOption:
            return select_option(self.bot.theme, tr(label_key), value, default=selected)

        if kind == "heading":
            return [
                TextField(key="text", label=tr("cv2.fields.heading"), max_length=200,
                          default=block.text if block else None),
                TextField(key="emoji", label=tr("cv2.fields.emoji"), required=False, max_length=60,
                          description=tr("cv2.fields.emoji_hint"), default=block.emoji if block else None),
                TextField(key="subline", label=tr("cv2.fields.subline"), required=False, max_length=200,
                          default=block.subline if block else None),
            ]
        if kind == "text":
            style = block.style if block else "plain"
            return [
                TextField(key="content", label=tr("cv2.fields.content"), paragraph=True, max_length=3000,
                          description=tr("cv2.fields.markdown_hint"), default=block.content if block else None),
                SelectField(key="style", label=tr("cv2.fields.style"), options=[
                    opt("cv2.styles.plain", "plain", style == "plain"),
                    opt("cv2.styles.quote", "quote", style == "quote"),
                    opt("cv2.styles.small", "small", style == "small"),
                ]),
            ]
        if kind == "banner":
            return [TextField(key="source", label=tr("cv2.fields.banner"), max_length=500,
                              description=tr("cv2.fields.banner_hint"), default=block.source if block else None)]
        if kind == "image":
            return [
                TextField(key="url", label=tr("cv2.fields.image_url"), max_length=500, placeholder="https://",
                          default=block.url if block else None),
                TextField(key="description", label=tr("cv2.fields.alt"), required=False, max_length=200,
                          default=block.description if block else None),
            ]
        if kind == "separator":
            large = block.large if block else False
            visible = block.visible if block else True
            return [
                SelectField(key="size", label=tr("cv2.fields.size"), options=[
                    opt("cv2.styles.small_space", "small", not large), opt("cv2.styles.large_space", "large", large)]),
                SelectField(key="visible", label=tr("cv2.fields.visible"), options=[
                    opt("common.yes", "yes", visible), opt("common.no", "no", not visible)]),
            ]
        lines = "\n".join(f"{i.label} | {i.url}" + (f" | {i.emoji}" if i.emoji else "") for i in block.items) \
            if block else None
        return [TextField(key="lines", label=tr("cv2.fields.buttons"), paragraph=True, max_length=1500,
                          description=tr("cv2.fields.buttons_hint"), placeholder="Website | https://example.com | link",
                          default=lines)]

    def _block_modal(self, interaction: discord.Interaction[Any], template: Template, kind: str,
                     index: int | None) -> FormModal:
        tr = self.tr(interaction)
        existing = template.data.blocks[index] if index is not None else None

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            current = await self._template(modal_interaction, template.id)
            block = make_block(kind, values)
            blocks = list(current.data.blocks)
            if index is None:
                if len(blocks) >= MAX_BLOCKS:
                    raise UserError("cv2.errors.too_many", max=MAX_BLOCKS)
                blocks.append(block)
            elif 0 <= index < len(blocks):
                blocks[index] = block
            else:
                raise NotFound()
            await self._store(modal_interaction, current, TemplateData(accent=current.data.accent, blocks=blocks),
                              focus=index)

        return FormModal(self.bot, title=tr(f"cv2.blocks.{kind}"), fields=self._block_fields(tr, kind, existing),
                         on_submit=submitted)

    # ================================================================== builder components
    @component("cv2.add")
    async def on_add(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        kind = ctx.values[0] if ctx.values else "text"
        if kind not in BLOCK_TYPES:
            raise UserError("cv2.errors.invalid_block")
        await interaction.response.send_modal(self._block_modal(interaction, template, kind, None))

    @component("cv2.pick")
    async def on_pick(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        await interaction.response.edit_message(
            view=await self.render_builder(interaction, template, focus=int(ctx.values[0])))

    @component("cv2.back")
    async def on_back(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        await interaction.response.edit_message(view=await self.render_builder(interaction, template))

    @component("cv2.edit")
    async def on_edit(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        index = ctx.int_arg(1)
        if not 0 <= index < len(template.data.blocks):
            raise NotFound()
        await interaction.response.send_modal(
            self._block_modal(interaction, template, template.data.blocks[index].type, index))

    async def _move(self, interaction: discord.Interaction[Any], ctx: ComponentContext, delta: int) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        index = ctx.int_arg(1)
        blocks = list(template.data.blocks)
        target = index + delta
        if not (0 <= index < len(blocks) and 0 <= target < len(blocks)):
            raise NotFound()
        blocks[index], blocks[target] = blocks[target], blocks[index]
        await self._store(interaction, template, TemplateData(accent=template.data.accent, blocks=blocks), focus=target)

    @component("cv2.up")
    async def on_up(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        await self._move(interaction, ctx, -1)

    @component("cv2.down")
    async def on_down(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        await self._move(interaction, ctx, 1)

    @component("cv2.rm")
    async def on_remove(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        blocks = list(template.data.blocks)
        index = ctx.int_arg(1)
        if not 0 <= index < len(blocks):
            raise NotFound()
        blocks.pop(index)
        await self._store(interaction, template, TemplateData(accent=template.data.accent, blocks=blocks))

    @component("cv2.color")
    async def on_color(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            current = await self._template(modal_interaction, template.id)
            try:
                data = TemplateData(accent=str(values.get("accent") or "").strip() or None, blocks=current.data.blocks)
            except ValidationError as exc:
                raise UserError("cv2.errors.color") from exc
            await self._store(modal_interaction, current, data)

        await interaction.response.send_modal(FormModal(self.bot, title=tr("cv2.builder.color"), fields=[
            TextField(key="accent", label=tr("cv2.fields.accent"), required=False, max_length=7,
                      placeholder=self.bot.config.design.accent_color, default=template.data.accent,
                      description=tr("cv2.fields.accent_hint")),
        ], on_submit=submitted))

    @component("cv2.rename")
    async def on_rename(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            await self.templates.rename(template, str(values["name"]))
            updated = await self.templates.get(template.id)
            await modal_interaction.response.edit_message(view=await self.render_builder(modal_interaction, updated))

        await interaction.response.send_modal(FormModal(self.bot, title=tr("cv2.builder.rename"), fields=[
            TextField(key="name", label=tr("cv2.fields.name"), max_length=60, default=template.name)],
            on_submit=submitted))

    @component("cv2.copy")
    async def on_copy(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            copy = await self.templates.create(modal_interaction.guild_id or 0, str(values["name"]),
                                               modal_interaction.user.id, template.data)
            await modal_interaction.response.edit_message(view=await self.render_builder(
                modal_interaction, copy, notice=tr("cv2.builder.copied", name=template.name)))

        await interaction.response.send_modal(FormModal(self.bot, title=tr("cv2.builder.copy"), fields=[
            TextField(key="name", label=tr("cv2.fields.name"), max_length=60, default=f"{template.name} (2)")],
            on_submit=submitted))

    @component("cv2.send")
    async def on_send(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        if not ctx.resolved_values or interaction.guild is None:
            return
        channel = interaction.guild.get_channel(ctx.resolved_values[0].id)
        if not isinstance(channel, (discord.TextChannel, discord.NewsChannel)):
            raise UserError("errors.invalid_input")
        message = await self.send(template, channel, interaction.user)
        await interaction.response.edit_message(view=await self.render_builder(
            interaction, template, notice=self.tr(interaction)("cv2.builder.sent", url=message.jump_url)))

    async def send(self, template: Template, channel: discord.abc.Messageable, user: discord.abc.User) -> discord.Message:
        if not template.data.blocks:
            raise UserError("cv2.errors.empty")
        message = await channel.send(view=render_template(self.bot.theme, template.data),
                                     allowed_mentions=discord.AllowedMentions.none())
        await self.templates.record_message(template, message, user.id)
        guild = self.bot.get_guild(template.guild_id)
        await self.bot.audit.log(guild, "commands", "cv2.send", title=self.gtr(template.guild_id)("cv2.log.sent"),
                                 emoji="cv2", actor=user, lines=[f"**{template.name}** · {message.jump_url}"])
        return message

    @component("cv2.update")
    async def on_update(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        tr = self.tr(interaction)

        async def submitted(modal_interaction: discord.Interaction[Any], values: dict[str, Any]) -> None:
            message = await self._resolve_message(modal_interaction, str(values["link"]))
            await message.edit(view=render_template(self.bot.theme, template.data))
            await self.templates.record_message(template, message, modal_interaction.user.id)
            await modal_interaction.response.edit_message(view=await self.render_builder(
                modal_interaction, template, notice=tr("cv2.builder.updated", url=message.jump_url)))

        await interaction.response.send_modal(FormModal(self.bot, title=tr("cv2.builder.update_link"), fields=[
            TextField(key="link", label=tr("cv2.fields.link"), max_length=200,
                      placeholder="https://discord.com/channels/…", description=tr("cv2.fields.link_hint"))],
            on_submit=submitted))

    async def _resolve_message(self, interaction: discord.Interaction[Any], link: str) -> discord.Message:
        match = MESSAGE_LINK.search(link)
        if match is None or int(match.group(1)) != interaction.guild_id:
            raise UserError("cv2.errors.link")
        channel = self.bot.get_channel(int(match.group(2)))
        if not isinstance(channel, (discord.TextChannel, discord.NewsChannel, discord.Thread)):
            raise UserError("cv2.errors.link")
        try:
            message = await channel.fetch_message(int(match.group(3)))
        except discord.HTTPException as exc:
            raise UserError("cv2.errors.link") from exc
        if message.author.id != self.bot.user.id:  # type: ignore[union-attr]
            raise UserError("cv2.errors.not_own")
        return message

    @component("cv2.sync")
    async def on_sync(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        await interaction.response.defer()
        updated = 0
        for row in await self.templates.messages(template):
            channel = self.bot.get_channel(row["channel_id"])
            if not isinstance(channel, (discord.TextChannel, discord.NewsChannel, discord.Thread)):
                await self.templates.forget_message(row["message_id"])
                continue
            try:
                await channel.get_partial_message(row["message_id"]).edit(
                    view=render_template(self.bot.theme, template.data))
                updated += 1
            except discord.NotFound:
                await self.templates.forget_message(row["message_id"])
            except discord.HTTPException:
                _log.info("Could not update message %s from template %s", row["message_id"], template.id)
        await interaction.edit_original_response(view=await self.render_builder(
            interaction, template, notice=self.tr(interaction)("cv2.builder.synced", count=updated)))

    @component("cv2.del")
    async def on_delete(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        tr = self.tr(interaction)
        await interaction.response.edit_message(view=build_confirm(
            self.bot.theme, tr, title=tr("cv2.delete_title"), description=tr("cv2.delete_text", name=template.name),
            confirm_id=cid("cv2.del_ok", template.id), cancel_id=cid("cv2.back", template.id), danger=True))

    @component("cv2.del_ok")
    async def on_delete_ok(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        template = await self._template(interaction, ctx.int_arg(0))
        await self.templates.delete(template)
        await interaction.response.edit_message(view=self.bot.notice(
            kind="success", title=self.tr(interaction)("cv2.deleted", name=template.name)))

    # ================================================================== slash commands
    async def _template_choices(self, interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[int]]:
        rows = await self.templates.list_templates(interaction.guild_id or 0)
        return [app_commands.Choice(name=truncate(r["name"], 100), value=r["id"])
                for r in rows if current.lower() in r["name"].lower()][:25]

    @group.command(name="new", description=T("cv2.new"))
    @app_commands.describe(name=T("cv2.name"))
    @require_admin()
    async def new_cmd(self, interaction: discord.Interaction[Any], name: app_commands.Range[str, 1, 60]) -> None:
        template = await self.templates.create(interaction.guild_id or 0, name, interaction.user.id)
        await interaction.response.send_message(view=await self.render_builder(interaction, template), ephemeral=True)

    @group.command(name="edit", description=T("cv2.edit"))
    @app_commands.describe(template=T("cv2.template"))
    @require_admin()
    async def edit_cmd(self, interaction: discord.Interaction[Any], template: int) -> None:
        item = await self.templates.get(template, interaction.guild_id)
        await interaction.response.send_message(view=await self.render_builder(interaction, item), ephemeral=True)

    edit_cmd.autocomplete("template")(_template_choices)

    @group.command(name="send", description=T("cv2.send"))
    @app_commands.describe(template=T("cv2.template"), channel=T("shop.channel"))
    @require_admin()
    async def send_cmd(self, interaction: discord.Interaction[Any], template: int,
                       channel: discord.TextChannel | None = None) -> None:
        item = await self.templates.get(template, interaction.guild_id)
        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            raise UserError("errors.invalid_input")
        message = await self.send(item, target, interaction.user)
        await self.bot.respond_notice(interaction, title=self.tr(interaction)("cv2.builder.sent", url=message.jump_url))

    send_cmd.autocomplete("template")(_template_choices)

    @group.command(name="list", description=T("cv2.list"))
    @require_admin()
    async def list_cmd(self, interaction: discord.Interaction[Any]) -> None:
        tr = self.tr(interaction)
        rows = await self.templates.list_templates(interaction.guild_id or 0)
        b = CV2(self.bot.theme)
        b.banner("cv2")
        b.heading(tr("cv2.list_title"), emoji="template")
        if rows:
            b.text("\n".join(f"{self.bot.icons['template']} **{r['name']}** · "
                             f"{tr('cv2.list_line', blocks=r['blocks'], sent=r['sent'])} · {ts(r['updated_at'], 'R')}"
                             for r in rows[:30]))
        else:
            b.quote(tr("cv2.list_empty"))
        await interaction.response.send_message(view=b.build(), ephemeral=True)

    @group.command(name="export", description=T("cv2.export"))
    @app_commands.describe(template=T("cv2.template"))
    @require_admin()
    async def export_cmd(self, interaction: discord.Interaction[Any], template: int) -> None:
        item = await self.templates.get(template, interaction.guild_id)
        payload = json.dumps({"name": item.name, **item.data.model_dump(mode="json")}, ensure_ascii=False, indent=2)
        file_name = f"cv2-{item.id}.json"
        b = CV2(self.bot.theme).heading(item.name, emoji="template", level=3).file(file_name)
        await interaction.response.send_message(view=b.build(), ephemeral=True,
                                                file=discord.File(io.BytesIO(payload.encode()), filename=file_name))

    export_cmd.autocomplete("template")(_template_choices)

    @group.command(name="import", description=T("cv2.import"))
    @app_commands.describe(file=T("cv2.file"), name=T("cv2.name"))
    @require_admin()
    async def import_cmd(self, interaction: discord.Interaction[Any], file: discord.Attachment,
                         name: app_commands.Range[str, 1, 60] | None = None) -> None:
        if file.size > 256 * 1024:
            raise UserError("cv2.errors.import")
        try:
            raw = json.loads((await file.read()).decode("utf-8"))
            data = TemplateData.model_validate({k: v for k, v in raw.items() if k in ("accent", "blocks")})
        except (ValueError, ValidationError, AttributeError) as exc:
            raise UserError("cv2.errors.import") from exc
        if not fits(self.bot.theme, data):
            raise UserError("cv2.errors.too_large")
        template = await self.templates.create(interaction.guild_id or 0, name or str(raw.get("name") or file.filename),
                                               interaction.user.id, data)
        await interaction.response.send_message(view=await self.render_builder(interaction, template), ephemeral=True)
