"""File storage backends: Cloudflare R2 (S3 compatible, aioboto3) or the local filesystem."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..config.settings import Secrets

_log = logging.getLogger("xumari.storage")


class R2Storage:
    def __init__(self, secrets: Secrets) -> None:
        self._secrets = secrets

    @property
    def endpoint(self) -> str:
        return f"https://{self._secrets.r2_account_id}.r2.cloudflarestorage.com"

    async def upload(self, key: str, data: bytes, content_type: str) -> str:
        import aioboto3

        session = aioboto3.Session()
        async with session.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self._secrets.r2_access_key_id,
            aws_secret_access_key=self._secrets.r2_secret_access_key,
            region_name="auto",
        ) as client:
            await client.put_object(Bucket=self._secrets.r2_bucket, Key=key, Body=data, ContentType=content_type)
        base = self._secrets.r2_public_base_url.rstrip("/")
        return f"{base}/{key}" if base else f"{self.endpoint}/{self._secrets.r2_bucket}/{key}"


class LocalStorage:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def write(self, file_name: str, data: bytes) -> Path:
        path = self.directory / file_name

        def _write() -> None:
            self.directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_write)
        return path
