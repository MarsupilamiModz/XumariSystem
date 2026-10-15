"""Versioned SQL migration runner.

Files named ``NNN_description.sql`` in this directory are applied in order, each in its own
transaction. Applied versions are recorded in ``schema_migrations`` together with a checksum.
A PostgreSQL advisory lock prevents bot and API from migrating concurrently.

CLI::

    python -m migrations.runner            # apply pending migrations
    python -m migrations.runner --status   # show applied / pending
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent
LOCK_ID = 841_120_001

_log = logging.getLogger("xumari.migrations")


def _migration_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


async def _ensure_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT PRIMARY KEY,
            checksum    TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


async def run_migrations(pool_or_conn: asyncpg.Pool | asyncpg.Connection) -> list[str]:
    """Apply all pending migrations. Returns the list of newly applied versions."""
    if isinstance(pool_or_conn, asyncpg.Pool):
        async with pool_or_conn.acquire() as conn:
            return await _apply(conn)
    return await _apply(pool_or_conn)


async def _apply(conn: asyncpg.Connection) -> list[str]:
    applied_now: list[str] = []
    await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
    try:
        await _ensure_table(conn)
        rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
        applied = {r["version"]: r["checksum"] for r in rows}
        for path in _migration_files():
            version = path.stem
            sql = path.read_text(encoding="utf-8")
            checksum = _checksum(sql)
            if version in applied:
                if applied[version] != checksum:
                    _log.warning("Migration %s changed after it was applied (checksum mismatch)", version)
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)", version, checksum
                )
            applied_now.append(version)
            _log.info("Applied migration %s", version)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)
    return applied_now


async def _status(conn: asyncpg.Connection) -> None:
    await _ensure_table(conn)
    applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
    for path in _migration_files():
        state = "applied" if path.stem in applied else "pending"
        print(f"  [{state:>7}] {path.stem}")


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="XumariSystem migration runner")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", ""), help="PostgreSQL DSN")
    parser.add_argument("--status", action="store_true", help="list applied/pending migrations")
    args = parser.parse_args(argv)
    dsn = args.dsn
    if not dsn:
        from bot.config.settings import Secrets

        dsn = Secrets().database_url
    conn = await asyncpg.connect(dsn)
    try:
        if args.status:
            await _status(conn)
        else:
            applied = await run_migrations(conn)
            print("Applied: " + (", ".join(applied) if applied else "nothing – database is up to date"))
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_main(sys.argv[1:])))
