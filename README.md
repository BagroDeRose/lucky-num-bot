# LuckyNum ("Денежный номер")

A Telegram bot that gives an entertainment-oriented numerology analysis of
banknote serial numbers. Users submit a serial number, get a free teaser
result from a deterministic scoring engine, and can purchase a full
AI-narrated personalized report.

🇷🇺 **О проекте (кратко):** LuckyNum — Telegram-бот «Денежный номер».
Пользователь присылает серийный номер купюры, получает бесплатный
предварительный результат на основе детерминированного алгоритма, а за
небольшую плату — полный персональный разбор, сгенерированный AI на основе
уже посчитанных данных.

> **Product positioning**: LuckyNum presents its numerology reading as a
> confident, symbolic interpretation — it never phrases anything as a
> guaranteed real-world outcome (results are framed as tendencies and
> possibilities, e.g. "тяготеет к...", never "принесёт деньги"). By
> deliberate product decision, the bot's copy does not carry an
> "entertainment only" / "not financial advice" disclaimer footer — see
> app/bot/texts.py and app/ai/prompts.py.

---

## How it works

1. `/start` — bot introduces itself.
2. User sends a serial number (digits only, leading zeros preserved).
3. The bot asks once for a date of birth (skippable). It is remembered on the
   user, so later banknotes reuse it without asking again.
4. A **deterministic** Python analysis engine computes digit sums, patterns,
   and four bounded sub-scores (money / luck / growth / stability) plus an
   overall 0–100 score, with a full explainable breakdown. With a birth date
   it also derives the life-path digit and how it meets the serial — see
   "Date-of-birth personalization" below.
5. The bot shows a **free teaser** — interesting, but deliberately partial.
6. User taps "Открыть полный разбор" and pays (mock provider locally, real
   Telegram Payments, or YooKassa, depending on configuration).
7. On confirmed payment, the structured analysis is handed to OpenAI, which
   narrates it into a polished report — **the AI never computes anything**,
   it only writes prose around numbers that already exist.
8. The report is cached on the analysis row, so retries/duplicate taps never
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
│   ├── birth.py             # birth-date validation + derived life-path facts
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
- Optional date-of-birth personalization (see below) — deterministic,
  bounded, and fully skippable.
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
- Alembic migrations, async SQLAlchemy 2.x, pytest suite (280+ tests), ruff +
  mypy clean.

## Date-of-birth personalization

Optional layer on top of the serial-number analysis. An analysis without a
birth date is scored *exactly* as before — the whole feature contributes
nothing when no date is present, which is what keeps historical results and
non-participating users unaffected.

**The calculation** (implemented in `app/analysis/birth.py`, documented in
`app/analysis/rules.py`):

1. **Life-path digit** — the birth date's digits in `DD MM YYYY` order are
   summed and reduced to a single digit using the *same* reduction the serial
   number already uses (`reduce_to_single_digit`). No new arithmetic is
   introduced: `07.03.1990` → `0+7+0+3+1+9+9+0 = 29` → `2+9 = 11` → `1+1 = 2`.
   Its meaning is read from the existing `DIGIT_MEANINGS` table.
2. **Resonance** — only objectively checkable relations are used:
   `same_number` (the serial reduces to the same digit), `present` (the digit
   literally occurs among the serial's digits), or `absent`.
3. **Bounded bonus** — resonance adds at most `PERSONAL_RESONANCE_CAP` (2) to
   the birth digit's own dominant category/ies, selected by the same rule the
   existing digit-emphasis bonus uses. The cap is deliberately lower than
   `DIGIT_EMPHASIS_CAP` (3) and far below a digit's base profile (up to 6), so
   personalization adjusts a reading without ever dominating it. A regression
   test asserts no sub-score can move by more than the cap.

Same date + same serial always produce the same result: no randomness, no
hidden state, no dependence on OpenAI.

**Privacy / data minimization:**

- The date is stored once on the user (`users.birth_date`, nullable) so it is
  asked once rather than per banknote. Each analysis stores only the *derived*
  numbers it was computed with, so changing the date never rewrites history.
- The raw date is **never** sent to OpenAI — only `birth_number`,
  its meaning, `birth_resonance` and `birth_digit_in_serial_count`. Tests
  assert the date is not reconstructible from the AI payload.
- The raw date is **never** written to the event log or application logs;
  events record only whether an analysis was personalized.
- Users can decline ("Без даты рождения") and still get the full
  serial-number product, or change the date later with `/birthdate`.

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
schema changes should go through Alembic. Both commands below read
`DATABASE_URL` from `.env` — works against SQLite (local dev) or PostgreSQL
(production) with no code changes:

```bash
# apply all migrations
alembic upgrade head

# after changing app/database/models.py, generate a new migration
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

### Upgrading a database that was created by `init_db()`

`init_db()` creates tables directly from the models and does **not** record an
Alembic revision, so a database that has only ever been started that way has
no `alembic_version` row. Running `alembic upgrade head` against it fails with
`table users already exists`, because Alembic tries to replay the initial
migration. `init_db()` also only creates missing *tables* — it never adds a
column to an existing one, so a new release's column is simply absent and the
bot fails on its first query with `no such column`.

Stamp the existing schema once, then upgrade normally from then on:

```bash
# one-time: record that the initial schema is already present (no DDL runs)
alembic stamp 24944e1e5e5f

# now apply everything newer, e.g. the users.birth_date column
alembic upgrade head
```

Verified against a copy of a real database created by `init_db()`: stamping
plus upgrading added `users.birth_date` and preserved every existing row
(users, analyses, payments, events) and every payment status unchanged.

### PostgreSQL (production)

SQLite is the local-dev/test default, but it only allows **one writer at a
time** — under real concurrent Telegram traffic (multiple updates being
handled at once, each opening its own DB session) this surfaces as
`sqlite3.OperationalError: database is locked`. PostgreSQL is the intended
production database.

The application code is already database-agnostic — `app/database/models.py`
and `app/database/repositories.py` use only portable SQLAlchemy types, and
`app/database/session.py`'s only SQLite-specific behavior (foreign-key
enforcement, WAL mode) is dialect-guarded and simply does nothing on
PostgreSQL. **Switching is a `DATABASE_URL` change, not a code change.**

1. Provision a PostgreSQL database (a managed instance, or `apt install
   postgresql` / Docker `postgres` image on your VPS) and create a database
   + user for the bot.
2. Set `DATABASE_URL` in `.env`:
   ```
   DATABASE_URL=postgresql+asyncpg://lucky_num:<password>@<host>:5432/lucky_num
   ```
   (the `asyncpg` driver is already a project dependency — nothing extra to
   install).
3. Run migrations against it: `alembic upgrade head`.
4. Start the bot as usual: `python -m app.main`.

SQLite remains fully supported for local development and is what the test
suite always uses (an isolated in-memory database per test, unrelated to
whatever `DATABASE_URL` is set to locally).

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

280+ tests cover: input validation, date-of-birth validation and its bounded deterministic effect on scoring, deterministic scoring/pattern detection
and score bounds (including a regression test against digit-frequency
double-counting), algorithm determinism, repository operations (including
user-scoped access control and SQLite foreign-key enforcement), payment
idempotency and amount/currency tamper protection across all three
providers, `pre_checkout_query` validation, YooKassa provider behavior
(all HTTP calls mocked), AI report generation (OpenAI is **fully mocked** —
no real API calls in the test suite, with timeout/malformed/empty-response
and retry paths also tested), Telegram routing edge cases (e.g. a slash
command while the FSM is mid-flow, or editing a message to identical
content), callback-query handlers acknowledging promptly *before* any slow
external call so Telegram never invalidates them, concurrent-write behavior
against a real file-based SQLite database (WAL mode, busy_timeout, a
deterministic unique-constraint race, and a payment-confirmation race
verifying `mark_payment_paid` transitions exactly once under concurrent
callers), Telegram HTML sanitization (Markdown
leakage, tag balancing, message-length truncation), and the AI-facing report
payload (deterministic facts preserved and internal scoring mechanics never
exposed), and a full free-to-paid integration flow.

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
| `AI_MAX_GENERATION_ATTEMPTS_PER_ANALYSIS` | No | Defaults to `5`. Caps real OpenAI attempts (successes + failures) per *paid* analysis — bounds worst-case AI spend if generation keeps failing and the user keeps tapping "Попробовать ещё раз" on the same already-paid analysis. |
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
- Prompts live in `app/ai/prompts.py`. The system prompt's *instructions*
  are written in English (models tend to follow structured instructions
  more reliably that way) but explicitly require **Russian** output. It
  defines a fixed, premium report structure (hook → main number walkthrough
  → digit-by-digit story → detected patterns → four profile sections →
  verdict), requires Telegram HTML (`<b>`/`<i>`) rather than Markdown, bans
  a short list of overused filler words/phrases, and forbids exposing raw
  scoring mechanics or guaranteeing outcomes — while explicitly *not* adding
  an "entertainment only" disclaimer footer (a deliberate product choice).
- `app.ai.report_generator._build_ai_payload()` curates what the model ever
  sees: deterministic facts (digits, sums, patterns, scores) only — never
  the internal `score_breakdown` (the "+6", "+1" style contributions), so
  the model structurally cannot echo scoring internals back to the user.
- The model's raw output is passed through `app.formatting.to_telegram_html`
  (converts any Markdown the model reaches for anyway into HTML, escapes
  everything else, degrades to plain text if tags end up unbalanced) and
  `truncate_telegram_html` (enforces Telegram's 4096-character message
  limit) before being stored/sent — see that module for why this lives
  outside `app.bot` despite being Telegram-specific.
- If the API call fails or the key is missing, `generate_report` raises
  `ReportGenerationError`; the payment handler catches this, keeps the paid
  state intact, and offers a "🔄 Попробовать ещё раз" retry button — a
  paying user is never left without recourse.
- `generate_report_with_fallback` (used where a guaranteed non-empty result
  is preferred over a retry prompt) falls back to a plain deterministic
  report template (`interpreter.render_fallback_full_report`) that mirrors
  the same section structure without the AI's richer prose.

### Financial safety

- Every OpenAI call requires `analysis.paid=True` (see every call site into
  `_deliver_report` in `app/bot/handlers/payment.py`) — routine AI volume is
  inherently gated by real payment revenue, not something the app needs to
  separately throttle.
- Exactly one OpenAI completion is ever made per successful report: an
  in-process, per-analysis `asyncio.Lock` prevents concurrent duplicate
  generation, and an already-generated report (`analysis.report`) is always
  reused instead of regenerating — including after a process restart, since
  that check is DB-persisted, not dependent on any in-memory state.
- A failed generation never auto-retries; the user must explicitly tap
  "Попробовать ещё раз". `AI_MAX_GENERATION_ATTEMPTS_PER_ANALYSIS` (default
  `5`) caps how many real attempts (successes + failures) can be made on one
  *already-paid* analysis, bounding worst-case AI spend on any single
  analysis to a small, predictable multiple of one report's cost — the one
  previously uncapped vector, since nothing else limited repeated retries
  after a persistent failure.
- Project-wide OpenAI spending caps/alerts (a hard ceiling on total monthly
  usage) are **not** configurable from this repository — that must be set
  in the OpenAI platform's own usage limits, not the application.

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
cp .env.example .env   # fill in real values, including DATABASE_URL for
                        # PostgreSQL — see "PostgreSQL (production)" above
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
- SQLite is for local development/tests only; production deployments must
  set `DATABASE_URL` to PostgreSQL (see "PostgreSQL (production)" above) —
  SQLite allows only one writer at a time and will raise "database is
  locked" under real concurrent Telegram traffic.
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
