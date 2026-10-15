from __future__ import annotations

from typing import TYPE_CHECKING

from .blacklist import BlacklistCog
from .cog import TicketsCog
from .reviews import ReviewsCog

if TYPE_CHECKING:
    from ...core.bot import XumariBot


async def setup(bot: XumariBot) -> None:
    await bot.add_cog(TicketsCog(bot))
    await bot.add_cog(BlacklistCog(bot))
    await bot.add_cog(ReviewsCog(bot))
