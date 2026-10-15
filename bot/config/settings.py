"""Typed configuration for XumariSystem.

Two sources are combined:

* ``Secrets`` – environment variables / ``.env`` (tokens, keys, DSN). Never stored in YAML.
* ``AppConfig`` – ``config.yaml`` (IDs, colours, banners, emojis, feature flags, texts).

Every section has sensible defaults, so the bot boots with a nearly empty ``config.yaml``.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Locale = Literal["de", "en"]
_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
_TIME_RANGE_RE = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------------------
# Secrets (.env)
# --------------------------------------------------------------------------------------
class Secrets(BaseSettings):
    """Values loaded from the environment / ``.env`` file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = ""
    database_url: str = "postgresql://xumari:xumari@localhost:5432/xumari"
    config_path: Path = Path("bot/config/config.yaml")
    log_level: str = "INFO"
    log_dir: Path = Path("logs")
    # Overrides ``api.mode`` from config.yaml (useful for docker-compose split deployments).
    api_mode: Literal["embedded", "external", "disabled"] | None = None

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_public_base_url: str = ""

    ai_api_key: str = ""

    @property
    def r2_configured(self) -> bool:
        return all((self.r2_account_id, self.r2_access_key_id, self.r2_secret_access_key, self.r2_bucket))


# --------------------------------------------------------------------------------------
# Core sections
# --------------------------------------------------------------------------------------
class BotSection(_Model):
    name: str = "XumariSystem"
    shop_name: str = "Xumari"
    default_locale: Locale = "de"
    use_client_locale: bool = True
    timezone: str = "Europe/Berlin"
    guild_ids: list[int] = Field(default_factory=list, description="Guilds for instant command sync.")
    owner_ids: list[int] = Field(default_factory=list)
    sync_commands_on_start: bool = True
    status_text: str = "xumari.shop"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r} (IANA name expected, e.g. Europe/Berlin)") from exc
        return value


class FeatureFlags(_Model):
    tickets: bool = True
    shop: bool = True
    orders: bool = True
    payments: bool = True
    downloads: bool = True
    economy: bool = True
    games: bool = True
    levels: bool = True
    invites: bool = True
    community: bool = True
    admin: bool = True
    stats: bool = True
    cv2: bool = True
    deals: bool = True
    logging: bool = True

    def as_dict(self) -> dict[str, bool]:
        return self.model_dump()


class DesignSection(_Model):
    accent_color: str = "#4A90E2"
    banners: dict[str, str] = Field(default_factory=dict)
    emojis: dict[str, str] = Field(default_factory=dict)

    @field_validator("accent_color")
    @classmethod
    def _validate_hex(cls, value: str) -> str:
        if not _HEX_RE.match(value):
            raise ValueError(f"invalid hex colour: {value!r}")
        return value if value.startswith("#") else f"#{value}"

    @property
    def accent(self) -> int:
        return int(self.accent_color.lstrip("#"), 16)


class RolesSection(_Model):
    admin: list[int] = Field(default_factory=list)
    staff: list[int] = Field(default_factory=list)
    designer: list[int] = Field(default_factory=list)
    designers_are_staff: bool = True
    customer: int | None = None


class LoggingSection(_Model):
    default_channel_id: int | None = None
    channels: dict[str, int] = Field(
        default_factory=dict,
        description="Category -> channel id (commands, buttons, modals, members, roles, messages, "
        "tickets, orders, payments, downloads, economy, moderation, community).",
    )
    log_commands: bool = True
    log_buttons: bool = True
    log_modals: bool = True
    log_member_events: bool = True
    log_message_events: bool = True
    log_role_events: bool = True
    store_in_db: bool = True
    ignored_channel_ids: list[int] = Field(default_factory=list)


class DatabaseSection(_Model):
    min_pool_size: int = 2
    max_pool_size: int = 10
    auto_migrate: bool = True


class ApiSection(_Model):
    mode: Literal["embedded", "external", "disabled"] = "embedded"
    host: str = "0.0.0.0"
    port: int = 8080
    public_url: str = "http://localhost:8080"
    cors_origins: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------------------
class TicketQuestion(_Model):
    key: str
    label_de: str
    label_en: str
    placeholder_de: str = ""
    placeholder_en: str = ""
    style: Literal["short", "paragraph"] = "short"
    required: bool = True
    min_length: int | None = None
    max_length: int = 1000


class TicketCategory(_Model):
    key: str
    name_de: str
    name_en: str
    description_de: str = ""
    description_en: str = ""
    emoji: str = "ticket"
    discord_category_id: int | None = None
    support_role_ids: list[int] = Field(default_factory=list)
    questions: list[TicketQuestion] = Field(default_factory=list)
    ai_validation: bool = False
    is_order: bool = False
    channel_prefix: str = "ticket"
    ping_support_roles: bool = True

    @field_validator("key")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9_]{1,24}", value):
            raise ValueError("category key must match [a-z0-9_]{1,24}")
        return value

    @field_validator("questions")
    @classmethod
    def _max_questions(cls, value: list[TicketQuestion]) -> list[TicketQuestion]:
        if len(value) > 5:
            raise ValueError("Discord modals support at most 5 questions per category")
        return value

    def name(self, locale: str) -> str:
        return self.name_de if locale == "de" else self.name_en

    def description(self, locale: str) -> str:
        return self.description_de if locale == "de" else self.description_en


class InactivitySection(_Model):
    enabled: bool = True
    close_after_hours: float = 72
    reminder_before_hours: float = 24
    delete_after_close_hours: float = 48
    check_interval_minutes: int = 10


class TranscriptSection(_Model):
    enabled: bool = True
    storage: Literal["r2", "local", "discord"] = "local"
    local_dir: Path = Path("data/transcripts")
    dm_user: bool = True
    send_file_to_log: bool = True


class OpeningHoursSection(_Model):
    enabled: bool = True
    block_outside: bool = False
    schedule: dict[str, str | None] = Field(
        default_factory=lambda: {
            "mon": "09:00-18:00",
            "tue": "09:00-18:00",
            "wed": "09:00-18:00",
            "thu": "09:00-18:00",
            "fri": "09:00-18:00",
            "sat": None,
            "sun": None,
        }
    )
    holidays: list[dt.date] = Field(default_factory=list)

    @field_validator("schedule")
    @classmethod
    def _validate_schedule(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        for day, span in value.items():
            if day not in WEEKDAYS:
                raise ValueError(f"unknown weekday {day!r}, use {WEEKDAYS}")
            if span is not None and not _TIME_RANGE_RE.match(span):
                raise ValueError(f"invalid time range {span!r}, expected HH:MM-HH:MM")
        return value


class AIValidationSection(_Model):
    enabled: bool = False
    endpoint: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout_seconds: float = 20
    fail_open: bool = True
    prompt: str = (
        "You validate support ticket requests for a design agency. Check whether the answers are complete, "
        "meaningful and not spam. Reply ONLY with JSON: {\"valid\": true|false, \"reason\": \"short reason in "
        "the user's language\"}."
    )


class ReviewSection(_Model):
    channel_id: int | None = None
    react_emoji: str = "💙"
    dm_on_completion: bool = True


class TicketsSection(_Model):
    panel_channel_id: int | None = None
    categories: list[TicketCategory] = Field(default_factory=list)
    closed_category_id: int | None = None
    max_open_per_user: int = 3
    pin_welcome: bool = True
    inactivity: InactivitySection = Field(default_factory=InactivitySection)
    transcripts: TranscriptSection = Field(default_factory=TranscriptSection)
    opening_hours: OpeningHoursSection = Field(default_factory=OpeningHoursSection)
    ai: AIValidationSection = Field(default_factory=AIValidationSection)
    reviews: ReviewSection = Field(default_factory=ReviewSection)

    @model_validator(mode="after")
    def _unique_keys(self) -> TicketsSection:
        keys = [c.key for c in self.categories]
        if len(keys) != len(set(keys)):
            raise ValueError("ticket category keys must be unique")
        if len(keys) > 25:
            raise ValueError("at most 25 ticket categories are supported (select menu limit)")
        return self

    def category(self, key: str) -> TicketCategory | None:
        return next((c for c in self.categories if c.key == key), None)


# --------------------------------------------------------------------------------------
# Shop / Orders / Payments
# --------------------------------------------------------------------------------------
class ExpressSection(_Model):
    enabled: bool = True
    surcharge_percent: float = 50.0
    surcharge_fixed_cents: int = 0
    delivery_days: int = 3


class SeasonalDesign(_Model):
    key: str
    name_de: str
    name_en: str
    emoji: str = "season"
    price_cents: int = 0

    def name(self, locale: str) -> str:
        return self.name_de if locale == "de" else self.name_en


class FaqEntry(_Model):
    question_de: str
    question_en: str
    answer_de: str
    answer_en: str
    emoji: str = "info"


class PartnershipSection(_Model):
    requirements_de: list[str] = Field(default_factory=list)
    requirements_en: list[str] = Field(default_factory=list)
    ticket_category: str | None = "partnership"


class ShopSection(_Model):
    currency: str = "EUR"
    standard_delivery_days: int = 7
    express: ExpressSection = Field(default_factory=ExpressSection)
    seasonal_designs: list[SeasonalDesign] = Field(default_factory=list)
    status_emojis: dict[str, str] = Field(
        default_factory=lambda: {
            "awaiting_payment": "💳",
            "paid": "💰",
            "queued": "📋",
            "in_progress": "🎨",
            "pending_answer": "⏳",
            "completed": "✅",
            "cancelled": "✖️",
        }
    )
    queue_channel_id: int | None = None
    showcase_channel_id: int | None = None
    faq: list[FaqEntry] = Field(default_factory=list)
    faq_ticket_category: str | None = "support"
    partnership: PartnershipSection = Field(default_factory=PartnershipSection)
    default_todos_de: list[str] = Field(default_factory=lambda: ["Briefing geprüft", "Entwurf erstellt", "Feedback eingearbeitet", "Finale Dateien geliefert"])
    default_todos_en: list[str] = Field(default_factory=lambda: ["Briefing reviewed", "Draft created", "Feedback applied", "Final files delivered"])


class StripeSection(_Model):
    mode: Literal["checkout", "payment_link"] = "checkout"
    success_url: str = "{api_url}/payment/success?order={order_id}"
    cancel_url: str = "{api_url}/payment/cancel?order={order_id}"
    session_expires_minutes: int = 1440


class InvoiceCompany(_Model):
    name: str = "Xumari Design"
    owner: str = ""
    address_lines: list[str] = Field(default_factory=lambda: ["Musterstraße 1", "12345 Musterstadt", "Deutschland"])
    email: str = "info@example.com"
    website: str = ""
    tax_number: str = ""
    vat_id: str = ""
    bank_lines: list[str] = Field(default_factory=list)


class InvoiceSection(_Model):
    enabled: bool = True
    number_prefix: str = "RE"
    small_business: bool = True
    vat_rate_percent: float = 19.0
    company: InvoiceCompany = Field(default_factory=InvoiceCompany)
    send_on_payment: bool = True
    font_path: Path | None = None


class PaymentsSection(_Model):
    stripe: StripeSection = Field(default_factory=StripeSection)
    invoice: InvoiceSection = Field(default_factory=InvoiceSection)
    legal_dir: Path = Path("legal")


# --------------------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------------------
class DownloadCategory(_Model):
    key: str
    name_de: str
    name_en: str
    emoji: str = "category"

    def name(self, locale: str) -> str:
        return self.name_de if locale == "de" else self.name_en


class DownloadsSection(_Model):
    default_channel_id: int | None = None
    track_clicks: bool = False
    send_video_embed: bool = False
    categories: list[DownloadCategory] = Field(
        default_factory=lambda: [
            DownloadCategory(key="script", name_de="Script", name_en="Script", emoji="script"),
            DownloadCategory(key="vehicle", name_de="Fahrzeug", name_en="Vehicle", emoji="vehicle"),
            DownloadCategory(key="map", name_de="Map", name_en="Map", emoji="map"),
            DownloadCategory(key="graphics", name_de="Grafiken", name_en="Graphics", emoji="graphics"),
        ]
    )

    def category(self, key: str | None) -> DownloadCategory | None:
        return next((c for c in self.categories if c.key == key), None)


# --------------------------------------------------------------------------------------
# Economy / Games
# --------------------------------------------------------------------------------------
class EconomyAction(_Model):
    min_amount: int
    max_amount: int
    cooldown_seconds: int
    success_rate: float = 1.0
    fine_percent: float = 0.0


class EconomySection(_Model):
    currency_name: str = "Coins"
    currency_emoji: str = "coin"
    start_balance: int = 100
    daily: EconomyAction = Field(default_factory=lambda: EconomyAction(min_amount=150, max_amount=250, cooldown_seconds=86400))
    work: EconomyAction = Field(default_factory=lambda: EconomyAction(min_amount=40, max_amount=120, cooldown_seconds=3600))
    crime: EconomyAction = Field(
        default_factory=lambda: EconomyAction(min_amount=100, max_amount=400, cooldown_seconds=7200, success_rate=0.45, fine_percent=0.25)
    )
    rob: EconomyAction = Field(
        default_factory=lambda: EconomyAction(min_amount=0, max_amount=0, cooldown_seconds=10800, success_rate=0.4, fine_percent=0.2)
    )
    rob_max_steal_percent: float = 0.3
    rob_min_victim_balance: int = 200
    leaderboard_size: int = 10


class GamesSection(_Model):
    min_bet: int = 10
    max_bet: int = 10000
    cooldown_seconds: int = 5
    game_timeout_seconds: int = 120
    blackjack_payout: float = 2.0
    blackjack_natural_payout: float = 2.5
    mines_grid_size: int = 4
    mines_default_mines: int = 3
    mines_house_edge: float = 0.03


# --------------------------------------------------------------------------------------
# Levels / Invites
# --------------------------------------------------------------------------------------
class BoostSection(_Model):
    enabled: bool = True
    role_ids: list[int] = Field(default_factory=list)
    channel_id: int | None = None


class LevelsSection(_Model):
    xp_min: int = 15
    xp_max: int = 25
    cooldown_seconds: int = 60
    blacklisted_channel_ids: list[int] = Field(default_factory=list)
    formula: str = "5 * level ** 2 + 50 * level + 100"
    level_up_channel_id: int | None = None
    announce_level_up: bool = True
    rewards: dict[int, int] = Field(default_factory=dict, description="level -> role id")
    stack_rewards: bool = True
    leaderboard_size: int = 10
    boost: BoostSection = Field(default_factory=BoostSection)

    @field_validator("formula")
    @classmethod
    def _valid_formula(cls, value: str) -> str:
        from ..core.safe_eval import SafeExpression

        SafeExpression(value, ("level",))(level=1)
        return value


class InvitesSection(_Model):
    log_channel_id: int | None = None
    fake_account_age_days: int = 7
    leaderboard_size: int = 10


# --------------------------------------------------------------------------------------
# Community
# --------------------------------------------------------------------------------------
class WelcomeSection(_Model):
    enabled: bool = True
    channel_id: int | None = None
    auto_role_ids: list[int] = Field(default_factory=list)
    dm_user: bool = False


class CaptchaSection(_Model):
    enabled: bool = False
    mode: Literal["image", "button"] = "image"
    channel_id: int | None = None
    verified_role_id: int | None = None
    unverified_role_id: int | None = None
    code_length: int = 6
    max_attempts: int = 3
    kick_after_minutes: int = 0


class NicknameFilterSection(_Model):
    enabled: bool = False
    allowed_pattern: str = r"^[\w\s\-\.äöüÄÖÜß']+$"
    blocked_words: list[str] = Field(default_factory=list)
    replacement: str = "Member {id4}"


class MemberCounterSection(_Model):
    enabled: bool = False
    channel_id: int | None = None
    name_format: str = "👥 Mitglieder: {count}"
    count_bots: bool = False
    update_interval_minutes: int = 10


class GhostPingSection(_Model):
    enabled: bool = True
    max_age_seconds: int = 120
    channel_id: int | None = None


class ModmailSection(_Model):
    enabled: bool = False
    guild_id: int | None = None
    channel_id: int | None = None
    staff_role_ids: list[int] = Field(default_factory=list)


class RoleSyncPair(_Model):
    source_guild_id: int
    source_role_id: int
    target_guild_id: int
    target_role_id: int


class GiveawaySection(_Model):
    default_channel_id: int | None = None
    check_interval_seconds: int = 30


class CommunitySection(_Model):
    welcome: WelcomeSection = Field(default_factory=WelcomeSection)
    captcha: CaptchaSection = Field(default_factory=CaptchaSection)
    nickname_filter: NicknameFilterSection = Field(default_factory=NicknameFilterSection)
    member_counter: MemberCounterSection = Field(default_factory=MemberCounterSection)
    ghost_ping: GhostPingSection = Field(default_factory=GhostPingSection)
    modmail: ModmailSection = Field(default_factory=ModmailSection)
    role_sync: list[RoleSyncPair] = Field(default_factory=list)
    giveaways: GiveawaySection = Field(default_factory=GiveawaySection)


# --------------------------------------------------------------------------------------
# Root
# --------------------------------------------------------------------------------------
class AppConfig(_Model):
    bot: BotSection = Field(default_factory=BotSection)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    design: DesignSection = Field(default_factory=DesignSection)
    roles: RolesSection = Field(default_factory=RolesSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    api: ApiSection = Field(default_factory=ApiSection)
    tickets: TicketsSection = Field(default_factory=TicketsSection)
    shop: ShopSection = Field(default_factory=ShopSection)
    payments: PaymentsSection = Field(default_factory=PaymentsSection)
    downloads: DownloadsSection = Field(default_factory=DownloadsSection)
    economy: EconomySection = Field(default_factory=EconomySection)
    games: GamesSection = Field(default_factory=GamesSection)
    levels: LevelsSection = Field(default_factory=LevelsSection)
    invites: InvitesSection = Field(default_factory=InvitesSection)
    community: CommunitySection = Field(default_factory=CommunitySection)


def load_config(path: Path) -> AppConfig:
    """Load ``config.yaml``; a missing file yields the default configuration."""
    if not path.exists():
        return AppConfig()
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return AppConfig.model_validate(raw)
