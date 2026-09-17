"""Aggregate numbers for the admin panel. Read-only."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Payment,
    PaymentStatus,
    PromoCode,
    PromoRedemption,
    RedemptionStatus,
    User,
)
from app.engagement.leaderboard import count_reports
from app.engagement.periods import as_utc, month_start, utcnow, week_containing


@dataclass(frozen=True)
class AdminOverview:
    users: int
    reports_total: int
    reports_week: int
    reports_month: int
    paid_payments: int
    revenue_rub: int
    promo_activations: int
    promo_consumed: int
    active_promos: int
    week_label: str


async def _scalar(session: AsyncSession, stmt: Select[Any]) -> int:
    return int((await session.execute(stmt)).scalar_one() or 0)


async def admin_overview(session: AsyncSession, now: dt.datetime | None = None) -> AdminOverview:
    now = as_utc(now or utcnow())
    week = week_containing(now)
    paid = Payment.status == PaymentStatus.PAID
    return AdminOverview(
        users=await _scalar(session, select(func.count(User.id))),
        reports_total=await count_reports(session),
        reports_week=await count_reports(session, since=week.start, until=week.end),
        reports_month=await count_reports(session, since=month_start(now)),
        paid_payments=await _scalar(session, select(func.count(Payment.id)).where(paid)),
        revenue_rub=await _scalar(session, select(func.coalesce(func.sum(Payment.amount), 0)).where(paid)),
        promo_activations=await _scalar(session, select(func.count(PromoRedemption.id))),
        promo_consumed=await _scalar(
            session,
            select(func.count(PromoRedemption.id)).where(
                PromoRedemption.status == RedemptionStatus.CONSUMED
            ),
        ),
        active_promos=await _scalar(
            session,
            select(func.count(PromoCode.id)).where(
                PromoCode.is_active.is_(True), PromoCode.valid_until > now
            ),
        ),
        week_label=week.label,
    )


def render_admin_overview(o: AdminOverview) -> str:
    return "\n".join(
        [
            "📈 <b>Статистика</b>",
            "",
            f"👥 Пользователей: {o.users}",
            f"🔬 Полных разборов: {o.reports_total}",
            f"📅 За неделю ({o.week_label}): {o.reports_week}",
            f"🗓 За месяц: {o.reports_month}",
            "",
            f"💳 Оплат: {o.paid_payments}",
            f"💰 Выручка: {o.revenue_rub} ₽",
            "",
            f"🎟 Активаций промокодов: {o.promo_activations}",
            f"✅ Использовано при оплате: {o.promo_consumed}",
            f"🟢 Активных промокодов: {o.active_promos}",
        ]
    )
