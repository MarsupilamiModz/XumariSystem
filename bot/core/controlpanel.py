"""Registry for /controlpanel sections. Cogs register their own section in ``on_load``::

    bot.control_sections.register(ControlSection("downloads", "download", 60, "downloads", self.render_section))
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from ..ui.builder import CV2
    from .i18n import Translator

SectionRenderer = Callable[[discord.Interaction[Any], "CV2", "Translator"], Awaitable[None]]


@dataclass(slots=True)
class ControlSection:
    key: str
    emoji: str
    order: int
    feature: str | None
    render: SectionRenderer


class ControlSectionRegistry:
    def __init__(self) -> None:
        self._sections: dict[str, ControlSection] = {}

    def register(self, section: ControlSection) -> None:
        self._sections[section.key] = section

    def unregister(self, key: str) -> None:
        self._sections.pop(key, None)

    def get(self, key: str) -> ControlSection | None:
        return self._sections.get(key)

    def available(self, is_enabled: Callable[[str | None], bool]) -> list[ControlSection]:
        return sorted((s for s in self._sections.values() if is_enabled(s.feature)), key=lambda s: s.order)
