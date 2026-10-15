from __future__ import annotations

from typing import TYPE_CHECKING

from .cog import CV2Cog

if TYPE_CHECKING:
    from ...core.bot import XumariBot


async def setup(bot: XumariBot) -> None:
    await bot.add_cog(CV2Cog(bot))
