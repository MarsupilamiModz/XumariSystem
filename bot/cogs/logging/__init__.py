from __future__ import annotations

from typing import TYPE_CHECKING

from .events import LoggingCog

if TYPE_CHECKING:
    from ...core.bot import XumariBot


async def setup(bot: XumariBot) -> None:
    await bot.add_cog(LoggingCog(bot))
