"""Generic modal builder (text inputs and selects wrapped in ``ui.Label``)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord
from discord import ui

if TYPE_CHECKING:
    from ..core.bot import XumariBot

SubmitCallback = Callable[[discord.Interaction[Any], dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class TextField:
    key: str
    label: str
    description: str | None = None
    placeholder: str | None = None
    default: str | None = None
    paragraph: bool = False
    required: bool = True
    min_length: int | None = None
    max_length: int | None = None


@dataclass(slots=True)
class SelectField:
    key: str
    label: str
    options: Sequence[discord.SelectOption]
    description: str | None = None
    placeholder: str | None = None
    min_values: int = 1
    max_values: int = 1
    required: bool = True


@dataclass(slots=True)
class EntityField:
    """User / role / channel picker inside a modal (values → list of resolved objects)."""

    key: str
    label: str
    kind: str = "user"  # user | role | channel
    description: str | None = None
    placeholder: str | None = None
    min_values: int = 1
    max_values: int = 1
    required: bool = True
    channel_types: Sequence[discord.ChannelType] = ()


ModalField = TextField | SelectField | EntityField


class FormModal(ui.Modal):
    """Modal created from field specs. ``on_submit`` receives ``{key: value}`` (selects → list[str])."""

    def __init__(
        self,
        bot: XumariBot,
        *,
        title: str,
        fields: Sequence[ModalField],
        on_submit: SubmitCallback,
        timeout: float | None = 900,
    ) -> None:
        super().__init__(title=title[:45], timeout=timeout)
        self.bot = bot
        self._callback = on_submit
        self._inputs: dict[str, ui.Item[Any]] = {}
        for spec in fields[:5]:
            component: ui.Item[Any]
            if isinstance(spec, EntityField):
                kwargs: dict[str, Any] = {
                    "placeholder": spec.placeholder,
                    "min_values": spec.min_values if spec.required else 0,
                    "max_values": spec.max_values,
                    "required": spec.required,
                }
                if spec.kind == "role":
                    component = ui.RoleSelect(**kwargs)
                elif spec.kind == "channel":
                    component = ui.ChannelSelect(channel_types=list(spec.channel_types), **kwargs)
                else:
                    component = ui.UserSelect(**kwargs)
            elif isinstance(spec, TextField):
                component = ui.TextInput(
                    style=discord.TextStyle.paragraph if spec.paragraph else discord.TextStyle.short,
                    placeholder=(spec.placeholder or None) and spec.placeholder[:100],
                    default=(spec.default or None) and spec.default[:4000],
                    required=spec.required,
                    min_length=spec.min_length,
                    max_length=spec.max_length,
                )
            else:
                options = list(spec.options)[:25]
                component = ui.Select(
                    options=options,
                    placeholder=spec.placeholder,
                    min_values=min(spec.min_values, len(options)) if spec.required else 0,
                    max_values=min(spec.max_values, len(options)),
                    required=spec.required,
                )
            self._inputs[spec.key] = component
            description = (spec.description or None) and spec.description[:100]
            self.add_item(ui.Label(text=spec.label[:45], description=description, component=component))

    async def on_submit(self, interaction: discord.Interaction[Any]) -> None:
        values: dict[str, Any] = {}
        for key, component in self._inputs.items():
            if isinstance(component, ui.TextInput):
                values[key] = component.value.strip()
            else:
                values[key] = list(getattr(component, "values", []))
        await self._callback(interaction, values)

    async def on_error(self, interaction: discord.Interaction[Any], error: Exception) -> None:
        await self.bot.errors.handle(interaction, error)
