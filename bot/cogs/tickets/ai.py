"""Optional AI pre-check of ticket answers via any OpenAI compatible ``/chat/completions`` endpoint."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from ...core.bot import XumariBot

_log = logging.getLogger("xumari.ai")


@dataclass(slots=True)
class AIVerdict:
    valid: bool
    reason: str
    checked: bool


class AIValidator:
    def __init__(self, bot: XumariBot) -> None:
        self.bot = bot

    @property
    def available(self) -> bool:
        cfg = self.bot.config.tickets.ai
        return cfg.enabled and bool(cfg.endpoint)

    async def validate(self, *, category: str, answers: list[dict[str, str]], locale: str) -> AIVerdict:
        cfg = self.bot.config.tickets.ai
        if not self.available or self.bot.http_session is None:
            return AIVerdict(True, "", False)
        language = "German" if locale == "de" else "English"
        user_content = "\n".join(
            [f"Ticket category: {category}", f"Answer language for 'reason': {language}", ""]
            + [f"Q: {a['question']}\nA: {a['answer'] or '(empty)'}" for a in answers]
        )
        payload = {
            "model": cfg.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": cfg.prompt},
                {"role": "user", "content": user_content},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.bot.secrets.ai_api_key:
            headers["Authorization"] = f"Bearer {self.bot.secrets.ai_api_key}"
        url = cfg.endpoint.rstrip("/") + "/chat/completions"
        try:
            async with self.bot.http_session.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=cfg.timeout_seconds)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            verdict = json.loads(content)
            return AIVerdict(bool(verdict.get("valid", True)), str(verdict.get("reason", "")).strip()[:500], True)
        except (aiohttp.ClientError, TimeoutError, KeyError, IndexError, ValueError, TypeError):
            _log.warning("AI validation failed (fail_open=%s)", cfg.fail_open, exc_info=True)
            return AIVerdict(cfg.fail_open, "", False)
