"""The XumariSystem bot class: wires config, database, i18n, registries, router and cogs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from migrations.runner import run_migrations

from ..config.settings import AppConfig, Secrets
from ..ui.builder import Theme
from ..ui.layouts import NoticeKind, build_notice
from ..ui.translatable import RendererRegistry, handle_language_switch
from .assets import AssetRegistry
from .audit import AuditService
from .command_i18n import CommandTranslator
from .controlpanel import ControlSectionRegistry
from .db import Database
from .emojis import EmojiRegistry
from .error_handler import ErrorHandler
from .errors import FeatureDisabled
from .events import EventBridge
from .features import FeatureService
from .i18n import I18n, Translator
from .router import ComponentContext, ComponentRouter, RouteRegistry
from .utils import utcnow

_log = logging.getLogger("xumari.bot")

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

#: feature flag → extension module. Disabled features are never imported.
EXTENSIONS: dict[str, str] = {
    "logging": "bot.cogs.logging",
    "admin": "bot.cogs.admin",
    "tickets": "bot.cogs.tickets",
    "shop": "bot.cogs.shop",
    "orders": "bot.cogs.orders",
    "payments": "bot.cogs.payments",
    "stats": "bot.cogs.stats",
    "economy": "bot.cogs.economy",
    "games": "bot.cogs.games",
    "levels": "bot.cogs.levels",
    "invites": "bot.cogs.invites",
    "community": "bot.cogs.community",
    "downloads": "bot.cogs.downloads",
    "cv2": "bot.cogs.cv2",
    "deals": "bot.cogs.deals",
}


class XumariTree(app_commands.CommandTree["XumariBot"]):
    async def interaction_check(self, interaction: discord.Interaction[XumariBot]) -> bool:
        command = interaction.command
        binding = getattr(command, "binding", None)
        feature = getattr(binding, "feature", None)
        if feature and not interaction.client.features.is_enabled(interaction.guild_id, feature):
            raise FeatureDisabled()
        return True

    async def on_error(
        self, interaction: discord.Interaction[XumariBot], error: app_commands.AppCommandError
    ) -> None:
        await interaction.client.errors.handle(interaction, error)


class _EmbeddedUvicorn:
    """Runs the FastAPI app inside the bot's event loop without hijacking signal handlers."""

    def __init__(self, app: Any, host: str, port: int) -> None:
        import uvicorn

        class _Server(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self):  # type: ignore[override]
                yield

            def install_signal_handlers(self) -> None:  # pragma: no cover - older uvicorn versions
                return None

        self.server = _Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on"))
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.server.serve(), name="xumari-api")

    async def stop(self) -> None:
        self.server.should_exit = True
        if self.task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self.task, timeout=10)


class XumariBot(commands.Bot):
    def __init__(self, config: AppConfig, secrets: Secrets) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.invites = True
        intents.presences = False
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=XumariTree,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
        )
        self.config = config
        self.secrets = secrets
        self.started_at = utcnow()

        self.db = Database(
            secrets.database_url,
            min_size=config.database.min_pool_size,
            max_size=config.database.max_pool_size,
        )
        self.icons = EmojiRegistry(config.design.emojis)
        self.assets = AssetRegistry(config.design.banners)
        self.theme = Theme(
            accent=config.design.accent, emojis=self.icons, assets=self.assets, shop_name=config.bot.shop_name
        )
        self.i18n = I18n(LOCALES_DIR, config.bot.default_locale)
        self.i18n.globals = {"shop": config.bot.shop_name, "bot": config.bot.name}
        self.i18n.emoji_lookup = self.icons.get

        self.features = FeatureService(config.features, self.db)
        self.routes = RouteRegistry()
        self.renderers = RendererRegistry()
        self.control_sections = ControlSectionRegistry()
        self.errors = ErrorHandler(self)
        self.audit = AuditService(self)
        self.event_bridge = EventBridge(self)
        self.http_session: aiohttp.ClientSession | None = None
        self._guild_locales: dict[int, str] = {}
        self._api: _EmbeddedUvicorn | None = None

    # ------------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        await self.db.connect()
        if self.config.database.auto_migrate:
            applied = await run_migrations(self.db.pool)
            if applied:
                _log.info("Applied migrations: %s", ", ".join(applied))
        await self.features.load_overrides()
        await self._load_guild_locales()

        await self.tree.set_translator(CommandTranslator())
        self.routes.register("lang", self._route_language, feature=None, owner="core")
        self.routes.register("dismiss", self._route_dismiss, feature=None, owner="core")
        self.add_dynamic_items(ComponentRouter)

        for feature, module in EXTENSIONS.items():
            if not self.features.loaded(feature):
                _log.info("Feature %r disabled – not loading %s", feature, module)
                continue
            await self.load_extension(module)
            _log.info("Loaded %s", module)

        await self.event_bridge.start()
        self._start_api()
        if self.config.bot.sync_commands_on_start:
            await self.sync_commands()

    async def _route_language(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        await handle_language_switch(interaction, ctx.args)

    async def _route_dismiss(self, interaction: discord.Interaction[Any], ctx: ComponentContext) -> None:
        """Generic "cancel" button for ephemeral confirmations."""
        tr = self.translator_for(interaction)
        await interaction.response.edit_message(view=self.notice(kind="info", title=tr("common.cancelled")))

    def api_mode(self) -> str:
        return self.secrets.api_mode or self.config.api.mode

    def _start_api(self) -> None:
        if self.api_mode() != "embedded":
            return
        from api.main import create_app

        app = create_app(config=self.config, secrets=self.secrets, db=self.db)
        self._api = _EmbeddedUvicorn(app, self.config.api.host, self.config.api.port)
        self._api.start()
        _log.info("Embedded API listening on %s:%s", self.config.api.host, self.config.api.port)

    async def sync_commands(self) -> int:
        if self.config.bot.guild_ids:
            total = 0
            for guild_id in self.config.bot.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                total = len(await self.tree.sync(guild=guild))
            _log.info("Synced %d commands to %d guild(s)", total, len(self.config.bot.guild_ids))
            return total
        synced = await self.tree.sync()
        _log.info("Synced %d global commands", len(synced))
        return len(synced)

    async def on_ready(self) -> None:
        assert self.user is not None
        _log.info("Logged in as %s (%s) – %d guild(s)", self.user, self.user.id, len(self.guilds))
        for guild in self.guilds:
            await self.db.ensure_guild(guild.id, guild.name)
        await self.change_presence(activity=discord.CustomActivity(name=self.config.bot.status_text))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.db.ensure_guild(guild.id, guild.name)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.db.execute("UPDATE guilds SET left_at = now() WHERE guild_id = $1", guild.id)

    async def close(self) -> None:
        await self.event_bridge.stop()
        if self._api is not None:
            await self._api.stop()
        await super().close()
        if self.http_session is not None:
            await self.http_session.close()
        await self.db.close()

    # ------------------------------------------------------------------ localisation
    async def _load_guild_locales(self) -> None:
        rows = await self.db.fetch("SELECT guild_id, locale FROM guilds")
        self._guild_locales = {r["guild_id"]: r["locale"] for r in rows}

    def guild_locale(self, guild_id: int | None) -> str:
        if guild_id is None:
            return self.config.bot.default_locale
        return self._guild_locales.get(guild_id, self.config.bot.default_locale)

    async def set_guild_locale(self, guild_id: int, locale: str) -> None:
        await self.db.ensure_guild(guild_id)
        await self.db.execute("UPDATE guilds SET locale = $2 WHERE guild_id = $1", guild_id, locale)
        self._guild_locales[guild_id] = locale

    def locale_for(self, interaction: discord.Interaction[Any]) -> str:
        if self.config.bot.use_client_locale:
            return self.i18n.resolve_locale(interaction.locale)
        return self.guild_locale(interaction.guild_id)

    def translator_for(self, interaction: discord.Interaction[Any]) -> Translator:
        return self.i18n.translator(self.locale_for(interaction))

    def guild_translator(self, guild_id: int | None) -> Translator:
        return self.i18n.translator(self.guild_locale(guild_id))

    # ------------------------------------------------------------------ responses
    def notice(self, *, kind: NoticeKind = "info", title: str, **kwargs: Any) -> discord.ui.LayoutView:
        return build_notice(self.theme, kind=kind, title=title, **kwargs)

    async def respond(
        self, interaction: discord.Interaction[Any], view: discord.ui.LayoutView, *, ephemeral: bool = True,
        **kwargs: Any,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(view=view, ephemeral=ephemeral, **kwargs)
        else:
            await interaction.response.send_message(view=view, ephemeral=ephemeral, **kwargs)

    async def respond_notice(
        self, interaction: discord.Interaction[Any], *, kind: NoticeKind = "success", title: str,
        description: str | None = None, ephemeral: bool = True,
    ) -> None:
        await self.respond(interaction, self.notice(kind=kind, title=title, description=description),
                           ephemeral=ephemeral)

    async def safe_dm(self, user: discord.abc.User, view: discord.ui.LayoutView, **kwargs: Any) -> bool:
        """Send a DM; returns False if the user blocks DMs."""
        try:
            await user.send(view=view, **kwargs)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False
