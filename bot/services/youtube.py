"""YouTube helpers: video ID parsing for all common URL formats and thumbnail resolution."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import aiohttp

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com",
          "www.youtube-nocookie.com"}
_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/", "/e/")


def parse_video_id(url: str | None) -> str | None:
    """Extract the 11 character video ID.

    Supports ``youtu.be/<id>``, ``youtube.com/watch?v=<id>``, ``/shorts/<id>``, ``/embed/<id>``, ``/live/<id>``,
    ``m.``/``music.``/``nocookie`` hosts and any extra query (``?feature=shared``, ``&t=42`` …).
    """
    if not url:
        return None
    url = url.strip()
    if _ID_RE.match(url):
        return url
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    candidate: str | None = None
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host in _HOSTS:
        if parsed.path == "/watch" or parsed.path.startswith("/watch"):
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        else:
            for prefix in _PATH_PREFIXES:
                if parsed.path.startswith(prefix):
                    candidate = parsed.path[len(prefix):].split("/")[0]
                    break
    if candidate and _ID_RE.match(candidate):
        return candidate
    return None


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def thumbnail_candidates(video_id: str) -> tuple[str, str]:
    base = f"https://img.youtube.com/vi/{video_id}"
    return f"{base}/maxresdefault.jpg", f"{base}/hqdefault.jpg"


async def resolve_thumbnail(session: aiohttp.ClientSession | None, video_id: str) -> str:
    """``maxresdefault.jpg`` if it exists (HEAD request), otherwise ``hqdefault.jpg``."""
    maxres, fallback = thumbnail_candidates(video_id)
    if session is None:
        return fallback
    try:
        async with session.head(maxres, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                return maxres
    except (aiohttp.ClientError, TimeoutError):
        return fallback
    return fallback
