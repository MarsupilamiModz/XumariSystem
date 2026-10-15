from fastapi import FastAPI

from . import customers, health, invoices, orders, public, transcripts, webhooks


def register_routers(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(orders.router)
    app.include_router(customers.router)
    app.include_router(invoices.router)
    app.include_router(webhooks.router)
    app.include_router(public.router)
    app.include_router(transcripts.router)
