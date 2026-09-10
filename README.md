# LuckyNum ("Денежный номер")

A Telegram bot that gives an entertainment-oriented numerology analysis of
banknote serial numbers. Users submit a serial number, get a free teaser
result from a deterministic scoring engine, and can purchase a full
AI-narrated personalized report.

🇷🇺 **О проекте (кратко):** LuckyNum — развлекательный Telegram-бот
«Денежный номер». Пользователь присылает серийный номер купюры, получает
бесплатный предварительный результат на основе детерминированного
алгоритма, а за небольшую плату — полный персональный отчёт, сгенерированный
AI на основе уже посчитанных данных. Это развлекательный продукт: результаты
не являются финансовым советом или научным прогнозом.

> **Entertainment disclaimer**: LuckyNum is an entertainment product. It
> never claims that numerology is scientifically predictive, that a banknote
> "causes" wealth or luck, or that results constitute financial advice. All
> AI-generated copy is instructed to reinforce this.

---

## How it works

1. `/start` — bot introduces itself.
2. User sends a serial number (digits only, leading zeros preserved).
3. A **deterministic** Python analysis engine computes digit sums, patterns,
   and four bounded sub-scores (money / luck / growth / stability) plus an
   overall 0–100 score, with a full explainable breakdown.
4. The bot shows a **free teaser** — interesting, but deliberately partial.
5. User taps "Получить полный отчёт" and pays (mock provider locally, real
   Telegram Payments, or YooKassa, depending on configuration).
6. On confirmed payment, the structured analysis is handed to OpenAI, which
   narrates it into a polished report — **the AI never computes anything**,
   it only writes prose around numbers that already exist.
7. The report is cached on the analysis row, so retries/duplicate taps never
   trigger a second OpenAI call or a second charge.

## Architecture

```
app/
├── main.py              # entrypoint: long-polling Telegram bot
├── config.py             # pydantic-settings, loads .env
├── logging.py            # structured stdout logging, never logs secrets
│
├── bot/                  # Telegram layer only — no business logic here
│   ├── handlers/          # start, analyze, payment, history
│   ├── keyboards/         # inline keyboards
│   ├── middlewares/       # DB session + user-resolution middleware
│   ├── states/            # aiogram FSM states
│   ├── texts.py           # Russian UX copy
│   └── utils.py           # aiogram Optional/Union helpers
│
├── analysis/              # deterministic business logic (pure functions)
│   ├── engine.py           # validate_serial_number(), analyze()
│   ├── rules.py             # the numerology "rule book" (entertainment)
│   ├── scoring.py           # pattern detection + bounded scoring
│   ├── models.py             # AnalysisResult, ScoreBreakdown (pydantic)
│   └── interpreter.py        # deterministic teaser/fallback text
│
├── ai/                    # OpenAI integration — narration only
│   ├── client.py            # thin OpenAI SDK wrapper
│   ├── prompts.py            # system + user prompt templates
│   └── report_generator.py    # AnalysisResult -> polished report text
│
├── payments/               # payment abstraction
│   ├── provider.py           # PaymentProvider ABC, Mock + Telegram impls
│   └── service.py             # idempotent application-level payment flow
│
└── database/                # SQLAlchemy 2.x async models + repositories
    ├── models.py               # User, Analysis, Payment, Event
    ├── repositories.py          # all queries live here
    ├── session.py                # async engine/session
    └── migrations/                # Alembic
```

Layering rule enforced throughout: Telegram handlers call into
`analysis` / `payments` / `ai` / `database.repositories` — they never contain
scoring logic, SQL, or prompt text themselves.

## Features

- Deterministic, versioned numerology analysis engine (`ALGORITHM_VERSION`)
  with a fully explainable score breakdown.
- Free teaser vs. paid full report, designed for conversion ("is it
  interesting?" free vs. "why is it interesting?" paid).
- Provider-agnostic payment abstraction with a working mock provider (no
  credentials needed), a ready-to-enable native Telegram Payments provider,
  and a real YooKassa REST API integration.
- Idempotent payment state machine (`pending` → `paid`/`failed`/`refunded`);
  duplicate callbacks/Telegram retries never double-charge or double-unlock.
- AI report generation strictly narrates pre-computed structured data;
  reports are cached so retries never re-trigger OpenAI calls.
- Lightweight funnel-analytics event log + a CLI to summarize it.
- `/history` with per-user, ownership-scoped access to past analyses.
- Alembic migrations, async SQLAlchemy 2.x, pytest suite (90+ tests), ruff +
  mypy clean.

## Requirements

- Python 3.12+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (Optional, required for AI reports) an OpenAI API key
- (Optional, for real payments) either a Telegram Payments provider token,
  or a YooKassa shop ID + secret key

## Setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -e ".[dev]"
cp .env.example .env
# then edit .env — see "Environment variables" below
```

## Database setup / migrations

The app auto-creates tables on startup via `init_db()` for convenience, but
schema changes should go through Alembic:

```bash
# apply all migrations (creates lucky_num.db locally by default)
alembic upgrade head

# after changing app/database/models.py, generate a new migration
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

## Running locally

```bash
python -m app.main
```

The bot starts in long-polling mode. Stop it with Ctrl+C (graceful shutdown
closes the bot session).

## Testing

```bash
pytest
```

90+ tests cover: input validation, deterministic scoring/pattern detection
and score bounds (including a regression test against digit-frequency
double-counting), algorithm determinism, repository operations (including
user-scoped access control and SQLite foreign-key enforcement), payment
idempotency and amount/currency tamper protection across all three
providers, `pre_checkout_query` validation, YooKassa provider behavior
(all HTTP calls mocked), AI report generation (OpenAI is **fully mocked** —
no real API calls in the test suite, with timeout/malformed/empty-response
and retry paths also tested), Telegram routing edge cases (e.g. a slash
command while the FSM is mid-flow), and a full free-to-paid integration
flow.

## Linting / type checking

```bash
ruff check .
mypy app
```

Both are clean as of this commit.

## Funnel analytics

Basic event counts (`start`, `analysis_completed`, `payment_success`, etc.)
can be summarized with:

```bash
python scripts/funnel_stats.py
```

No admin dashboard is included by design — this is an MVP.

## Environment variables

See [`.env.example`](.env.example) for the full list. Summary:

| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | **Yes** | From @BotFather. Bot will not start without it. |
| `OPENAI_API_KEY` | For paid reports | Without it, paid reports fail gracefully with a retry option; teaser and payment flow still work. |
| `OPENAI_MODEL` | No | Defaults to `gpt-3.5-turbo`. |
| `PAYMENT_PROVIDER` | No | `mock` (default, local testing), `telegram`, or `yookassa`. |
| `PAYMENT_TOKEN` | If `PAYMENT_PROVIDER=telegram` | Native Telegram Payments provider token from BotFather → Payments. Unrelated to YooKassa. |
| `YOOKASSA_SHOP_ID` | If `PAYMENT_PROVIDER=yookassa` | YooKassa shop (merchant) ID — the *shop* credential pair that actually charges the user. |
| `YOOKASSA_SHOP_API_KEY` | If `PAYMENT_PROVIDER=yookassa` | YooKassa shop secret key. |
| `YOOKASSA_AGENT_ID` | No | YooKassa *agent* (payouts — sending money out) ID. Accepted for forward-compatibility only; this MVP has no payout feature and never uses it. |
| `YOOKASSA_AGENT_API_KEY` | No | Same caveat as above. |
| `DATABASE_URL` | No | Defaults to a local SQLite file. |
| `PRICE_RUB` | No | Integer price of the full report. Defaults to 99. The server is always the source of truth for this — never trusted from client/callback input. |
| `CURRENCY` | No | Defaults to `RUB`. |
| `LOG_LEVEL` | No | Defaults to `INFO`. |

## Payment integration point

`app/payments/provider.py` defines the `PaymentProvider` interface
(`create_payment` / `verify_payment` / `parse_callback`). Three
implementations ship today:

- **`MockPaymentProvider`** (default): "payment" completes instantly when the
  user taps a button. Zero external dependencies — use this for local
  development and demos.
- **`TelegramPaymentProvider`**: uses native Telegram Payments
  (`bot.send_invoice` / `pre_checkout_query` / `successful_payment`).
  Telegram itself connects to a real payment provider configured via
  BotFather. **To go live: set `PAYMENT_PROVIDER=telegram` and `PAYMENT_TOKEN`
  — no code changes required.**
- **`YooKassaPaymentProvider`**: a real integration against the YooKassa
  REST API (`api.yookassa.ru/v3`), using the *shop* credentials
  (`YOOKASSA_SHOP_ID` / `YOOKASSA_SHOP_API_KEY`) — the merchant-facing API
  that actually charges the user. YooKassa's separate *agent* credentials
  (a payouts API, for sending money out) are accepted as configuration but
  deliberately **not** used anywhere in the charging flow, since this MVP
  has no payout/agent feature.
  - Flow: `create_payment()` creates a YooKassa payment with `confirmation.
    type=redirect` and returns a `confirmation_url`; the bot sends that as a
    URL button plus a "✅ Я оплатил, проверить статус" button. This MVP has
    no public HTTPS endpoint for YooKassa's webhook, so confirmation is via
    **polling**: tapping that button calls `GET /payments/{id}` and the
    authoritative status/amount from that response — never anything the
    client or Telegram claims — is what unlocks the report.
    `YooKassaPaymentProvider.parse_callback()` consumes that same Payment
    JSON shape, so a webhook receiver can be added later purely as an
    additional trigger for the existing confirmation path, with no parsing
    changes.
  - **To go live**: set `PAYMENT_PROVIDER=yookassa`, `YOOKASSA_SHOP_ID`, and
    `YOOKASSA_SHOP_API_KEY` to your live (non-`test_`) shop credentials — no
    code changes required.

A bespoke acquiring provider can be added later by implementing the same
`PaymentProvider` interface; the rest of the app (handlers, `PaymentService`,
DB schema) is provider-agnostic and would not need to change.

## OpenAI configuration

- `OPENAI_API_KEY` and `OPENAI_MODEL` in `.env`.
- Prompts live in `app/ai/prompts.py`. The system prompt hard-codes the
  entertainment/no-financial-advice constraints and instructs the model to
  use *only* the structured facts it's given — never invent scores or
  patterns.
- If the API call fails or the key is missing, `generate_report` raises
  `ReportGenerationError`; the payment handler catches this, keeps the paid
  state intact, and offers a "🔄 Повторить генерацию отчёта" retry button —
  a paying user is never left without recourse.
- `generate_report_with_fallback` (used where a guaranteed non-empty result
  is preferred over a retry prompt) falls back to a plain deterministic
  report template (`interpreter.render_fallback_full_report`).

## Security notes

- Bot token, OpenAI key, and payment token are read only from environment
  variables via pydantic-settings; none are logged.
- `.env` is git-ignored; `.env.example` contains no real values.
- All queries go through SQLAlchemy's parameterized query builder — no raw
  SQL string interpolation anywhere.
- `Analysis`/history lookups are always scoped by `user_id`, so one Telegram
  user can never read another's analyses.
- Payment callbacks are matched against a server-generated
  `provider_payment_id`, and `mark_payment_paid` is a no-op if the payment is
  already `paid` — duplicate/replayed callbacks cannot double-unlock content.
- Handlers catch expected failure modes (validation errors, AI errors,
  unknown payment callbacks) and reply with a friendly Russian message;
  unexpected exceptions are logged, never shown to the user as a stack trace.

## Deployment notes (simple Linux VPS)

No Docker/Kubernetes is required for this MVP; a plain systemd service is
sufficient:

```bash
# one-time setup
python -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # fill in real values
.venv/bin/alembic upgrade head

# start
.venv/bin/python -m app.main
```

Example `systemd` unit (`/etc/systemd/system/luckynum.service`):

```ini
[Unit]
Description=LuckyNum Telegram bot
After=network.target

[Service]
WorkingDirectory=/opt/lucky-num
EnvironmentFile=/opt/lucky-num/.env
ExecStart=/opt/lucky-num/.venv/bin/python -m app.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`Bot.session.close()` runs in a `finally` block in `app/main.py`, so
`systemctl stop`/SIGTERM shuts down cleanly. Run `alembic upgrade head`
again after pulling any release that changed `app/database/models.py`.

## Project structure

See [Architecture](#architecture) above.

## Future roadmap

The engine is intentionally shaped to make these additive rather than
rewrites:

- `analyze_many()` already exists in `app/analysis/engine.py` for future
  multi-banknote comparison/ranking features (not exposed in the bot UX
  yet).
- Additional "number domains" (phone numbers, car plates, apartment/dates)
  could reuse `rules.py` + `scoring.py` patterns with their own rule tables.
- YooKassa is already wired in behind `PaymentProvider`; a different gateway
  can be added the same way without touching handlers.
- Algorithm versioning (`ALGORITHM_VERSION`) is stored per-analysis so a
  future v2.0 scoring model won't corrupt/reinterpret historical results.

## Known limitations (MVP)

- No Docker/CI config included — kept intentionally minimal per project
  scope; add if/when deployment needs grow.
- `MockPaymentProvider` moves no real money; it exists purely to exercise
  the full flow before real payment credentials are available.
- No admin web dashboard; use `scripts/funnel_stats.py` for basic funnel
  numbers.
- SQLite is used for MVP simplicity; for meaningfully concurrent production
  load, migrate `DATABASE_URL` to Postgres (SQLAlchemy/Alembic already
  support this — no application code changes needed beyond the URL and
  driver).
- `payments.status` supports `refunded` in the schema, but no code path sets
  it yet — there is no refund UI/webhook in this MVP. If a payment is
  refunded through YooKassa's own dashboard, `analysis.paid`/`analysis.
  report` are **not** automatically revoked; building that revocation flow
  is a reasonable next step once a real refund need arises.
- YooKassa confirmation is polling-based (see "Payment integration point"),
  not webhook-based, since this MVP has no public HTTPS endpoint. This works
  fine for a low-volume bot but adds a manual "check status" tap; a webhook
  receiver (a small aiohttp web app run alongside the bot) is the natural
  production upgrade and would need no changes to `parse_callback`.
