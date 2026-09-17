"""Deterministic rendering for leaderboard, statistics, promo and reward
messages.

Every rank, count, date, discount and limit shown here comes straight from
application data. The AI model is never involved in these messages.

Privacy: users are shown by their public Telegram @username only. A user
without one appears under a neutral label — never by Telegram ID, internal
ID or anything derived from them. All interpolated user data is HTML-escaped
(the bot's parse mode is HTML).
"""

from __future__ import annotations

import datetime as dt
import html
from dataclasses import dataclass

from app.database.models import DeliveryStatus, PromoCode, PromoType
from app.engagement.leaderboard import Standing, UserStats
from app.engagement.periods import WeekPeriod, as_utc, last_valid_day

ANONYMOUS_RESEARCHER = "Исследователь без ника"
SEPARATOR = "────────────────"
_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

DELIVERY_LABELS = {
    DeliveryStatus.PENDING: "⏳ в очереди",
    DeliveryStatus.SENDING: "📤 отправляется",
    DeliveryStatus.SENT: "✅ отправлено",
    DeliveryStatus.FAILED: "❌ не доставлено",
    DeliveryStatus.SKIPPED: "⏭ пропущено",
}

PROMO_TYPE_LABELS = {
    PromoType.WEEKLY_PUBLIC: "еженедельный",
    PromoType.TOP_REWARD: "награда TOP-5",
    PromoType.ADMIN_MANUAL: "ручной",
}


def display_name(username: str | None) -> str:
    return f"@{html.escape(username)}" if username else ANONYMOUS_RESEARCHER


def plural_reports(count: int) -> str:
    """"1 разбор", "3 разбора", "11 разборов"."""
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} разборов"
    last = count % 10
    if last == 1:
        return f"{count} разбор"
    if 2 <= last <= 4:
        return f"{count} разбора"
    return f"{count} разборов"


def _ranked_line(rank: int, username: str | None, count: int) -> str:
    medal = _MEDALS.get(rank)
    name = display_name(username)
    return f"{rank}. {medal} {name} — {count}" if medal else f"{rank}. {name} — {count}"


def format_day(day: dt.date) -> str:
    return f"{day:%d.%m}"


# --- user-facing ----------------------------------------------------------


def render_user_stats(stats: UserStats) -> str:
    lines = [
        "📊 <b>Моя статистика</b>",
        "",
        f"🔬 Разборов всего: {stats.lifetime_reports}",
        f"📅 На этой неделе ({stats.period.label}): {stats.week_reports}",
    ]
    if stats.week_rank is not None:
        lines.append(f"🏆 Место в рейтинге недели: #{stats.week_rank}")
    if stats.weekly_streak >= 1:
        lines.append(f"🔥 Недель подряд с разборами: {stats.weekly_streak}")
    if stats.lifetime_reports == 0:
        lines += ["", "Первый полный разбор — и вы в рейтинге исследователей."]
    elif stats.week_rank is None:
        lines += ["", "На этой неделе вы ещё не в рейтинге — хватит одного полного разбора."]
    return "\n".join(lines)


TOP_LIST_SIZE = 10


def render_top(
    standings: list[Standing], period: WeekPeriod, viewer_user_id: int | None
) -> str:
    lines = ["🏆 <b>Топ исследователей</b>", f"📅 {period.label}", ""]
    shown = standings[:TOP_LIST_SIZE]
    if shown:
        lines += [_ranked_line(s.rank, s.username, s.report_count) for s in shown]
    else:
        lines.append("На этой неделе ещё никто не завершил полный разбор — первое место свободно.")

    viewer = next((s for s in standings if s.user_id == viewer_user_id), None)
    lines += ["", SEPARATOR]
    if viewer is None:
        lines.append("👤 Вас пока нет в рейтинге этой недели — хватит одного полного разбора.")
    elif viewer.rank <= TOP_LIST_SIZE:
        lines.append(f"👤 Ваша позиция: #{viewer.rank} — {viewer.report_count}")
    else:
        lines.append(f"👤 <b>Ваша позиция: #{viewer.rank} — {viewer.report_count}</b>")
    lines.append(SEPARATOR)
    lines.append("TOP-5 недели получают персональный промокод со скидкой.")
    return "\n".join(lines)


ASK_PROMO_CODE = "🎟 Введите промокод:"
PROMO_ACTIVATED = (
    "✅ <b>Промокод активирован!</b>\n\n"
    "Скидка {discount}% применится к вашему следующему полному разбору."
)
PROMO_INVALID = "❌ Такого промокода нет или срок его действия закончился."
PROMO_ALREADY_USED = "ℹ️ Вы уже активировали этот промокод."
PROMO_EXHAUSTED = "⚡ Лимит активаций этого промокода исчерпан."
PROMO_NOT_FOR_YOU = "❌ Этот промокод недоступен для вашего аккаунта."


# --- rewards and channel ---------------------------------------------------


def render_reward_message(
    *, period: WeekPeriod, rank: int, report_count: int, promo: PromoCode
) -> str:
    return "\n".join(
        [
            "🏆 <b>Вы в TOP-5 исследователей недели!</b>",
            "",
            f"На неделе {period.label} вы завершили {plural_reports(report_count)} "
            f"и заняли #{rank} место.",
            "",
            "🎁 Ваша награда — персональный промокод:",
            f"<code>{html.escape(promo.code)}</code>",
            "",
            f"Скидка {promo.discount_percent}% на следующий полный разбор.",
            "Промокод личный, действует один раз "
            f"до {format_day(last_valid_day(promo.valid_until))} включительно.",
            "",
            "Ввести его можно кнопкой «🎟 Промокод» в меню.",
            "",
            "Спасибо, что исследуете с LuckyNum 🔮",
        ]
    )


@dataclass(frozen=True)
class RankedName:
    rank: int
    username: str | None
    report_count: int


def render_channel_post(
    *, period: WeekPeriod, top: list[RankedName], promo: PromoCode
) -> str:
    lines = ["🏆 <b>Топ исследователей недели</b>", f"📅 {period.label}", ""]
    if top:
        lines += [_ranked_line(t.rank, t.username, t.report_count) for t in top]
    else:
        lines.append("На прошлой неделе рейтинг пуст — первое место всё ещё свободно.")
    lines += [
        "",
        "🎁 <b>Промокод недели</b>",
        "",
        f"Скидка {promo.discount_percent}% на один полный разбор.",
        "",
        f"🎟 <code>{html.escape(promo.code)}</code>",
        "",
        f"⚡ Не больше {promo.max_activations} активаций.",
        f"📅 Действует до {format_day(last_valid_day(promo.valid_until))} включительно.",
        "",
        "Ввести промокод можно в боте: кнопка «🎟 Промокод».",
    ]
    return "\n".join(lines)


# --- admin ---------------------------------------------------------------


def render_promo_line(promo: PromoCode, now: dt.datetime) -> str:
    live = promo.is_active and as_utc(promo.valid_until) > as_utc(now)
    state = "🟢" if live else "⚪️"
    return (
        f"{state} <code>{html.escape(promo.code)}</code> — {promo.discount_percent}% · "
        f"{promo.activations_count}/{promo.max_activations} · "
        f"до {format_day(last_valid_day(promo.valid_until))} · "
        f"{PROMO_TYPE_LABELS.get(PromoType(promo.promo_type), promo.promo_type)}"
    )
