from __future__ import annotations

from typing import TYPE_CHECKING

from .cog import BoostCog, LevelsCog

if TYPE_CHECKING:
    from ...core.bot import XumariBot


async def setup(bot: XumariBot) -> None:
    await bot.add_cog(LevelsCog(bot))
    await bot.add_cog(BoostCog(bot))
