"""Entry point: ``python -m bot.main`` (``--check`` validates config + locales and exits)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

from .config.settings import Secrets, load_config
from .core.logger import setup_logging


def _self_check() -> int:
    from .core.bot import LOCALES_DIR
    from .core.i18n import I18n

    secrets = Secrets()
    config = load_config(secrets.config_path)
    i18n = I18n(LOCALES_DIR, config.bot.default_locale)
    missing = i18n.missing_keys()
    print(f"config: OK ({secrets.config_path})")
    print(f"locales: {', '.join(i18n.locales)}")
    if missing:
        for locale, keys in missing.items():
            print(f"  missing in {locale}: {len(keys)} keys, e.g. {keys[:5]}")
        return 1
    print("locales: complete")
    return 0


async def _run() -> None:
    from .core.bot import XumariBot

    secrets = Secrets()
    setup_logging(secrets.log_level, secrets.log_dir)
    log = logging.getLogger("xumari")
    config = load_config(secrets.config_path)
    if not secrets.discord_token:
        log.critical("DISCORD_TOKEN is not set – see .env.example")
        raise SystemExit(2)
    bot = XumariBot(config, secrets)
    async with bot:
        await bot.start(secrets.discord_token)


def main() -> None:
    parser = argparse.ArgumentParser(description="XumariSystem Discord bot")
    parser.add_argument("--check", action="store_true", help="validate config and locales, then exit")
    args = parser.parse_args()
    if args.check:
        sys.exit(_self_check())
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
