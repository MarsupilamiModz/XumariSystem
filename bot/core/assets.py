"""Banner / image registry. URLs come from ``config.yaml`` → ``design.banners`` (see ASSETS.md)."""

from __future__ import annotations

BANNER_KEYS: tuple[str, ...] = (
    "tickets",
    "ticket_welcome",
    "review",
    "order",
    "shop",
    "downloads",
    "giveaway",
    "welcome",
    "economy",
    "games",
    "levels",
    "invites",
    "captcha",
    "controlpanel",
    "logs",
    "faq",
    "partnership",
    "showcase",
    "queue",
    "payment",
    "legal",
    "reminder",
    "modmail",
    "stats",
    "roles",
    "cv2",
)


class AssetRegistry:
    def __init__(self, banners: dict[str, str] | None = None) -> None:
        self._banners = {k: v for k, v in (banners or {}).items() if isinstance(v, str) and v.startswith("http")}

    def banner(self, key: str | None) -> str | None:
        """Return the banner URL for ``key`` or ``None`` (layout then simply omits the banner)."""
        if not key:
            return None
        return self._banners.get(key)

    def configured(self) -> dict[str, str]:
        return dict(self._banners)
