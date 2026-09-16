"""Regression coverage for the production KeyError:

    File "app/bot/handlers/payment.py", in cb_get_report
        intent.extra["confirmation_url"]
    KeyError: 'confirmation_url'

Root cause (proven from the code, not guessed): PaymentIntent.extra is
in-memory only — the payments table has no column for it — so the
confirmation URL minted by YooKassa exists solely for the duration of the
request that created the payment. PaymentService.start_payment's *reuse*
branch rebuilt the intent from the stored Payment row with
extra={"payload": ...} only, so the second tap of "Открыть полный разбор"
on an unpaid analysis (an extremely common user action) handed the handler
an intent with no confirmation_url at all.

The fix reuses the same payment and re-reads its URL from YooKassa with a
read-only GET; when that genuinely cannot be recovered, the user gets a
recovery path instead of a crash — and never a second payment.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.engine import analyze
from app.bot import texts
from app.bot.handlers import payment as payment_module
from app.bot.handlers.payment import cb_get_report
from app.bot.keyboards.main import payment_recheck_kb
from app.database import repositories as repo
from app.database.models import Payment, PaymentStatus
from app.payments.provider import PaymentIntent, YooKassaPaymentProvider
from app.payments.service import PaymentService

CONFIRMATION_URL = "https://yoomoney.ru/checkout/pay/test-confirmation"


def _make_callback(callback_data: str) -> AsyncMock:
    callback = AsyncMock()
    callback.data = callback_data
    return callback


class _FakeYooKassa(YooKassaPaymentProvider):
    """Counts real API interactions so tests can assert that reusing a
    pending payment never mints a second one.
    """

    def __init__(
        self,
        *,
        status_payload: dict | None = None,
        status_raises: bool = False,
        create_latency: float = 0.0,
    ) -> None:
        self.create_latency = create_latency
        self.create_calls = 0
        self.status_calls = 0
        self._status_payload = status_payload
        self._status_raises = status_raises

    async def create_payment(self, *, amount: int, currency: str, description: str) -> PaymentIntent:
        self.create_calls += 1
        # A real YooKassa POST takes hundreds of ms; modelling that latency is
        # what opens the genuine race window between two concurrent taps.
        await asyncio.sleep(self.create_latency)
        return PaymentIntent(
            provider_payment_id=f"yk-{self.create_calls}",
            amount=amount,
            currency=currency,
            description=description,
            extra={"confirmation_url": CONFIRMATION_URL},
        )

    async def check_status(self, provider_payment_id: str) -> dict:
        self.status_calls += 1
        if self._status_raises:
            raise RuntimeError("YooKassa API connection error")
        if self._status_payload is not None:
            return self._status_payload
        return {
            "id": provider_payment_id,
            "status": "pending",
            "paid": False,
            "amount": {"value": "99.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "confirmation_url": CONFIRMATION_URL},
        }


async def _setup(session: AsyncSession, monkeypatch, telegram_id: int, provider: _FakeYooKassa):
    user = await repo.get_or_create_user(session, telegram_id=telegram_id, username="u")
    await session.flush()
    analysis = await repo.create_analysis(session, user_id=user.id, result=analyze("2200373"))
    await session.commit()

    service = PaymentService()
    service.provider = provider
    monkeypatch.setattr(payment_module, "payment_service", service)
    monkeypatch.setattr(payment_module.settings, "payment_provider", "yookassa")
    return user, analysis


def _sent(callback: AsyncMock) -> list[str]:
    return [c.args[1] for c in callback.bot.send_message.await_args_list]


def _markups(callback: AsyncMock) -> list[object]:
    return [c.kwargs.get("reply_markup") for c in callback.bot.send_message.await_args_list]


async def test_first_tap_shows_pay_button_with_confirmation_url(
    session: AsyncSession, monkeypatch
) -> None:
    """Baseline: the create path still works exactly as before."""
    provider = _FakeYooKassa()
    user, analysis = await _setup(session, monkeypatch, 801, provider)

    callback = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(callback, session, user)

    assert provider.create_calls == 1
    urls = [
        button.url
        for markup in _markups(callback)
        if markup is not None
        for row in markup.inline_keyboard
        for button in row
        if button.url
    ]
    assert CONFIRMATION_URL in urls


async def test_second_tap_reuses_payment_and_recovers_url_without_new_payment(
    session: AsyncSession, monkeypatch
) -> None:
    """THE production bug: tapping "Открыть полный разбор" a second time on
    an unpaid analysis previously raised KeyError('confirmation_url').

    It must now (a) not raise, (b) re-read the URL from the existing
    payment, and (c) NOT create a second YooKassa payment.
    """
    provider = _FakeYooKassa()
    user, analysis = await _setup(session, monkeypatch, 802, provider)

    first = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(first, session, user)

    second = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(second, session, user)  # used to raise KeyError here

    assert provider.create_calls == 1, "a reused pending payment must not mint a second one"
    assert provider.status_calls == 1, "the URL must be recovered by re-reading the payment"

    rows = (await session.execute(select(Payment).where(Payment.analysis_id == analysis.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == PaymentStatus.PENDING

    urls = [
        button.url
        for markup in _markups(second)
        if markup is not None
        for row in markup.inline_keyboard
        for button in row
        if button.url
    ]
    assert urls == [CONFIRMATION_URL]


async def test_unrecoverable_url_offers_recovery_instead_of_crashing_or_recharging(
    session: AsyncSession, monkeypatch
) -> None:
    """When the URL genuinely cannot be recovered (provider unreachable),
    the user must get an honest recovery path — never a crash, and never a
    second payment for the same analysis.
    """
    provider = _FakeYooKassa(status_raises=True)
    user, analysis = await _setup(session, monkeypatch, 803, provider)

    first = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(first, session, user)

    second = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(second, session, user)

    assert provider.create_calls == 1
    assert texts.PAYMENT_LINK_UNAVAILABLE in _sent(second)
    assert payment_recheck_kb(analysis.id) in _markups(second)

    rows = (await session.execute(select(Payment).where(Payment.analysis_id == analysis.id))).scalars().all()
    assert len(rows) == 1, "the existing payment record must be preserved untouched"
    assert rows[0].status == PaymentStatus.PENDING


async def test_terminal_payment_without_confirmation_block_takes_recovery_path(
    session: AsyncSession, monkeypatch
) -> None:
    """YooKassa omits `confirmation` once a payment is canceled/succeeded.
    That must be treated as "no link to hand over", not as an error and not
    as a reason to charge again.
    """
    provider = _FakeYooKassa(
        status_payload={
            "id": "yk-1",
            "status": "canceled",
            "paid": False,
            "amount": {"value": "99.00", "currency": "RUB"},
        }
    )
    user, analysis = await _setup(session, monkeypatch, 804, provider)

    await cb_get_report(_make_callback(f"get_report:{analysis.id}"), session, user)
    second = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(second, session, user)

    assert provider.create_calls == 1
    assert texts.PAYMENT_LINK_UNAVAILABLE in _sent(second)


async def test_empty_confirmation_url_is_treated_as_missing(
    session: AsyncSession, monkeypatch
) -> None:
    """create_payment defaults a missing URL to "" — Telegram rejects a URL
    button with an empty href, so an empty string must take the same
    recovery path rather than producing an unsendable keyboard.
    """

    class _EmptyUrlProvider(_FakeYooKassa):
        async def create_payment(self, *, amount: int, currency: str, description: str) -> PaymentIntent:
            self.create_calls += 1
            return PaymentIntent(
                provider_payment_id=f"yk-empty-{self.create_calls}",
                amount=amount,
                currency=currency,
                description=description,
                extra={"confirmation_url": ""},
            )

    provider = _EmptyUrlProvider()
    user, analysis = await _setup(session, monkeypatch, 805, provider)

    callback = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(callback, session, user)

    assert texts.PAYMENT_LINK_UNAVAILABLE in _sent(callback)
    assert provider.create_calls == 1


async def test_many_repeated_taps_create_exactly_one_payment(
    session: AsyncSession, monkeypatch
) -> None:
    """Repeated button presses (the behaviour that produced the crash) must
    stay idempotent: one payment row, one YooKassa payment, no duplicates.
    """
    provider = _FakeYooKassa()
    user, analysis = await _setup(session, monkeypatch, 806, provider)

    for _ in range(5):
        await cb_get_report(_make_callback(f"get_report:{analysis.id}"), session, user)

    assert provider.create_calls == 1
    rows = (await session.execute(select(Payment).where(Payment.analysis_id == analysis.id))).scalars().all()
    assert len(rows) == 1


async def test_reuse_path_never_calls_openai(session: AsyncSession, monkeypatch) -> None:
    """The fix must not introduce any AI call path: creating/reusing a
    payment is unrelated to report generation.
    """
    from app.ai import client as ai_client

    def fail_if_called(*args, **kwargs):
        raise AssertionError("payment creation/reuse must never touch OpenAI")

    monkeypatch.setattr(ai_client, "get_openai_client", fail_if_called)

    provider = _FakeYooKassa()
    user, analysis = await _setup(session, monkeypatch, 807, provider)

    await cb_get_report(_make_callback(f"get_report:{analysis.id}"), session, user)
    await cb_get_report(_make_callback(f"get_report:{analysis.id}"), session, user)


async def test_paid_analysis_ignores_payment_creation_entirely(
    session: AsyncSession, monkeypatch
) -> None:
    """An already-paid analysis must go straight to report delivery: no
    payment created, no confirmation URL lookup, existing report reused.
    """
    provider = _FakeYooKassa()
    user, analysis = await _setup(session, monkeypatch, 808, provider)
    await repo.mark_analysis_paid(session, analysis)
    await repo.save_report(session, analysis, "Готовый отчёт.")
    await session.commit()

    callback = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(callback, session, user)

    assert provider.create_calls == 0
    assert provider.status_calls == 0
    assert "Готовый отчёт." in _sent(callback)


async def test_concurrent_taps_do_not_create_two_yookassa_payments(tmp_path, monkeypatch) -> None:
    """Financial safety: two rapid taps on "Открыть полный разбор" arrive as
    two separate Telegram updates, each with its own DB session. Both can
    reach start_payment before either has committed a pending payment row —
    and each create_payment() call mints a brand-new provider_payment_id, so
    the unique constraint cannot catch it. Without serialization this bills
    the shop for two real YooKassa payments for one analysis.
    """
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database.models import Analysis, Base, User
    from app.database.session import _configure_sqlite_connection

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    event.listens_for(engine.sync_engine, "connect")(_configure_sqlite_connection)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    provider = _FakeYooKassa(create_latency=0.05)
    service = PaymentService()
    service.provider = provider
    monkeypatch.setattr(payment_module, "payment_service", service)
    monkeypatch.setattr(payment_module.settings, "payment_provider", "yookassa")
    monkeypatch.setattr(payment_module, "_payment_creation_locks", {}, raising=False)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as setup:
        user = await repo.get_or_create_user(setup, telegram_id=820, username="u")
        await setup.flush()
        analysis = await repo.create_analysis(setup, user_id=user.id, result=analyze("2200373"))
        await setup.commit()
        analysis_id, user_id = analysis.id, user.id

    # Pre-load independent sessions so both tasks genuinely start from
    # "no pending payment exists" — loading lazily would let SQLite's
    # single-writer lock serialize the reads and hide the race.
    loaded = []
    for _ in range(2):
        s = session_factory()
        loaded.append((s, await s.get(Analysis, analysis_id), await s.get(User, user_id)))

    async def tap(s, _analysis, _user):
        try:
            await cb_get_report(_make_callback(f"get_report:{analysis_id}"), s, _user)
        finally:
            await s.close()

    await asyncio.gather(*(tap(s, a, u) for s, a, u in loaded))

    async with session_factory() as check:
        rows = (await check.execute(select(Payment).where(Payment.analysis_id == analysis_id))).scalars().all()

    await engine.dispose()

    assert provider.create_calls == 1, (
        f"created {provider.create_calls} real YooKassa payments for one analysis"
    )
    assert len(rows) == 1, f"{len(rows)} pending payment rows for one analysis"


async def test_concurrent_start_payment_mints_exactly_one_yookassa_payment(
    tmp_path, monkeypatch
) -> None:
    """Genuinely reproduces the race (not a sequential stand-in): three
    concurrent start_payment calls, each on its own session, against a
    provider with realistic network latency.

    Verified to FAIL before the fix — create_payment() was called twice for
    two concurrent callers, meaning two real YooKassa payments for one
    analysis, only one of which we tracked. A user paying the untracked one
    would have been charged and received nothing, since confirm_payment
    resolves callbacks strictly by provider_payment_id.
    """
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database.models import Analysis, Base
    from app.database.session import _configure_sqlite_connection
    from app.payments import service as service_module

    monkeypatch.setattr(service_module, "_payment_creation_locks", {})

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race2.db'}")
    event.listens_for(engine.sync_engine, "connect")(_configure_sqlite_connection)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    provider = _FakeYooKassa(create_latency=0.05)
    service = PaymentService()
    service.provider = provider

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as setup:
        user = await repo.get_or_create_user(setup, telegram_id=821, username="u")
        await setup.flush()
        analysis = await repo.create_analysis(setup, user_id=user.id, result=analyze("2200373"))
        await setup.commit()
        analysis_id, user_id = analysis.id, user.id

    sessions = [session_factory() for _ in range(3)]
    analyses = [await s.get(Analysis, analysis_id) for s in sessions]

    async def start(s, an):
        try:
            payment, _ = await service.start_payment(s, user_id=user_id, analysis=an)
            await s.commit()
            return payment.id
        finally:
            await s.close()

    returned_ids = await asyncio.gather(
        *(start(s, a) for s, a in zip(sessions, analyses, strict=True))
    )

    async with session_factory() as check:
        rows = (
            await check.execute(select(Payment).where(Payment.analysis_id == analysis_id))
        ).scalars().all()
    await engine.dispose()

    assert provider.create_calls == 1, (
        f"{provider.create_calls} real YooKassa payments minted for one analysis"
    )
    assert len(rows) == 1
    assert len(set(returned_ids)) == 1, "all concurrent callers must share one payment"
