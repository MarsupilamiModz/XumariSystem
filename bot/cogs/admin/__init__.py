from __future__ import annotations

from typing import TYPE_CHECKING

from .controlpanel import ControlPanelCog
from .system import SystemCog

if TYPE_CHECKING:
    from ...core.bot import XumariBot


async def setup(bot: XumariBot) -> None:
    await bot.add_cog(SystemCog(bot))
    await bot.add_cog(ControlPanelCog(bot))
