from __future__ import annotations

from aiogram import Router

from app.bot.handlers import analyze, history, payment, start


def build_root_router() -> Router:
    router = Router(name="root")
    router.include_router(start.router)
    router.include_router(analyze.router)
    router.include_router(payment.router)
    router.include_router(history.router)
    return router
