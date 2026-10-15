"""``/deal`` – turns an affiliate link into a premium embed with a "view deal" link button."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands, ui

from ...core.cog import XCog
from ...core.command_i18n import T
from ...core.errors import UserError
from ...core.permissions import require_admin
from ...core.utils import is_url
from ...ui.modals import FormModal, TextField

DEAL_COLOR = 0xFF0000


class DealsCog(XCog, name="Deals"):
    feature = "deals"

    @staticmethod
    def build_deal(title: str, description: str, image_url: str, link: str,
                   label: str) -> tuple[discord.Embed, ui.View]:
        embed = discord.Embed(title=title, description=description, url=link, color=DEAL_COLOR)
        embed.set_image(url=image_url)
        view = ui.View(timeout=None)
        view.add_item(ui.Button(label=label, url=link, style=discord.ButtonStyle.link))
        return embed, view

    @app_commands.command(name="deal", description=T("deal"))
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @require_admin()
    async def deal(self, interaction: discord.Interaction[Any]) -> None:
        tr = self.tr(interaction)

        async def submitted(inter: discord.Interaction[Any], values: dict[str, Any]) -> None:
            image_url, link = values["image_url"], values["link"]
            if not (is_url(image_url) and is_url(link)):
                raise UserError("errors.invalid_url")
            channel = inter.channel
            if not isinstance(channel, discord.abc.Messageable):
                raise UserError("errors.generic")
            embed, view = self.build_deal(values["title"], values["description"], image_url, link,
                                          self.gtr(inter.guild_id)("deals.button"))
            await channel.send(embed=embed, view=view)
            await self.bot.respond_notice(inter, title=self.tr(inter)("deals.posted"))

        fields = [
            TextField(key="title", label=tr("deals.fields.title"), max_length=256),
            TextField(key="description", label=tr("deals.fields.description"), paragraph=True, max_length=4000),
            TextField(key="image_url", label=tr("deals.fields.image_url"), max_length=500, placeholder="https://"),
            TextField(key="link", label=tr("deals.fields.link"), max_length=500, placeholder="https://"),
        ]
        await interaction.response.send_modal(
            FormModal(self.bot, title=tr("deals.modal_title"), fields=fields, on_submit=submitted))
