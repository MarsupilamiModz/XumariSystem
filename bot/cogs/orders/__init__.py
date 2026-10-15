from __future__ import annotations

from typing import TYPE_CHECKING

from .cog import OrdersCog

if TYPE_CHECKING:
    from ...core.bot import XumariBot


async def setup(bot: XumariBot) -> None:
    await bot.add_cog(OrdersCog(bot))
