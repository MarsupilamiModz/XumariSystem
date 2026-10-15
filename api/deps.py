"""Shared dependencies: API key authentication with scopes and lazily created services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeVar

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from bot.core.command_i18n import LOCALES_DIR
from bot.core.i18n import I18n
from bot.services.apikeys import ApiKey, ApiKeyService
from bot.services.customers import CustomerDataService
from bot.services.invoices import InvoiceService
from bot.services.orders import OrderService
from bot.services.payments import PaymentRepository, StripeService
from bot.services.shop import ShopService

from .state import ApiState, get_state

T = TypeVar("T")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="API key created with /apikey create")


def _service(state: ApiState, name: str, factory: Callable[[], T]) -> T:
    if name not in state.services:
        state.services[name] = factory()
    return state.services[name]  # type: ignore[no-any-return]


class Services:
    def __init__(self, state: ApiState) -> None:
        self.state = state

    @property
    def i18n(self) -> I18n:
        return _service(self.state, "i18n", lambda: I18n(LOCALES_DIR, self.state.config.bot.default_locale))

    @property
    def shop(self) -> ShopService:
        return _service(self.state, "shop", lambda: ShopService(self.state.db))

    @property
    def orders(self) -> OrderService:
        return _service(self.state, "orders", lambda: OrderService(self.state.db, self.state.config))

    @property
    def payments(self) -> PaymentRepository:
        return _service(self.state, "payments", lambda: PaymentRepository(self.state.db))

    @property
    def stripe(self) -> StripeService:
        return _service(self.state, "stripe", lambda: StripeService(self.state.config, self.state.secrets))

    @property
    def invoices(self) -> InvoiceService:
        return _service(self.state, "invoices", lambda: InvoiceService(self.state.db, self.state.config, self.i18n))

    @property
    def customers(self) -> CustomerDataService:
        return _service(self.state, "customers", lambda: CustomerDataService(self.state.db))

    @property
    def keys(self) -> ApiKeyService:
        return _service(self.state, "keys", lambda: ApiKeyService(self.state.db))


def get_services(state: Annotated[ApiState, Depends(get_state)]) -> Services:
    return Services(state)


ServicesDep = Annotated[Services, Depends(get_services)]


def require_scope(scope: str) -> Callable[..., Awaitable[ApiKey]]:
    async def dependency(
        request: Request,
        services: ServicesDep,
        api_key: Annotated[str | None, Depends(_api_key_header)] = None,
    ) -> ApiKey:
        if not api_key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-API-Key header")
        key = await services.keys.verify(api_key)
        if key is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or revoked API key")
        if not key.allows(scope):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"missing scope {scope}")
        request.state.api_key = key
        return key

    return dependency


def guild_of(key: ApiKey) -> int:
    if key.guild_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key is not bound to a guild")
    return key.guild_id


def jsonable(value: Any) -> Any:
    """Discord snowflakes exceed JS number precision → serialise every large int as string."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53:
        return str(value)
    return value
