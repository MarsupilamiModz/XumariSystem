from __future__ import annotations

from typing import TYPE_CHECKING

from .giveaways import GiveawaysCog
from .modmail import ModmailCog
from .reactionroles import ReactionRolesCog
from .utility import UtilityCog
from .verification import VerificationCog

if TYPE_CHECKING:
    from ...core.bot import XumariBot


async def setup(bot: XumariBot) -> None:
    for cog in (GiveawaysCog, VerificationCog, ReactionRolesCog, UtilityCog, ModmailCog):
        await bot.add_cog(cog(bot))
