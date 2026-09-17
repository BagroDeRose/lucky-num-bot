"""researcher leaderboard, promo codes and weekly rewards

Adds:
- analyses.report_completed_at — the canonical completed-report instant the
  weekly leaderboard counts, backfilled for existing reports;
- promo_codes, promo_code_redemptions;
- weekly_leaderboards, weekly_leaderboard_entries, weekly_rewards.

Robust to both deployment orders. app.main runs init_db() (create_all) on
startup, which creates brand-new *tables* but never adds a *column* to an
existing table. If the new bot version was started before this migration,
the new tables may already exist; they are then left as they are and only
the missing column and backfill are applied, so `alembic upgrade head`
succeeds either way.

Backfill rule for existing paid reports (report IS NOT NULL): the earliest
"report_generation_success" event for that analysis — the moment the report
was saved — else analyses.created_at when no such event exists. Analyses
without a report are left NULL (they are not completed reports).

Revision ID: c3e9a1f4d2b6
Revises: b7c41d2e9f08
Create Date: 2026-09-16

"""
from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c3e9a1f4d2b6'
down_revision: str | None = 'b7c41d2e9f08'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def _as_datetime(value: object) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(str(value))


def _backfill_report_completed_at() -> None:
    bind = op.get_bind()
    analyses = sa.table(
        "analyses",
        sa.column("id", sa.Integer),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("report", sa.Text),
        sa.column("report_completed_at", sa.DateTime(timezone=True)),
    )
    events = sa.table(
        "events",
        sa.column("name", sa.String),
        sa.column("payload", sa.Text),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    # Earliest success event per analysis. Payload JSON is parsed in Python
    # so the same code runs on SQLite and PostgreSQL.
    saved_at: dict[int, dt.datetime] = {}
    rows = bind.execute(
        sa.select(events.c.payload, events.c.created_at).where(
            events.c.name == "report_generation_success"
        )
    )
    for payload, created_at in rows:
        data = payload if isinstance(payload, dict) else json.loads(payload or "{}")
        analysis_id = data.get("analysis_id") if isinstance(data, dict) else None
        when = _as_datetime(created_at)
        if isinstance(analysis_id, int) and when is not None:
            current = saved_at.get(analysis_id)
            if current is None or when < current:
                saved_at[analysis_id] = when

    pending = bind.execute(
        sa.select(analyses.c.id, analyses.c.created_at).where(
            analyses.c.report.is_not(None), analyses.c.report_completed_at.is_(None)
        )
    ).all()
    for analysis_id, created_at in pending:
        completed = saved_at.get(analysis_id) or _as_datetime(created_at)
        bind.execute(
            sa.update(analyses)
            .where(analyses.c.id == analysis_id)
            .values(report_completed_at=completed)
        )


def upgrade() -> None:
    if "report_completed_at" not in _columns("analyses"):
        op.add_column(
            "analyses", sa.Column("report_completed_at", sa.DateTime(timezone=True), nullable=True)
        )
    if "ix_analyses_report_completed_at" not in _indexes("analyses"):
        op.create_index(
            "ix_analyses_report_completed_at", "analyses", ["report_completed_at"], unique=False
        )
    _backfill_report_completed_at()

    tables = _tables()

    if "promo_codes" not in tables:
        op.create_table(
            "promo_codes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(length=32), nullable=False),
            sa.Column("promo_type", sa.String(length=20), nullable=False),
            sa.Column("discount_percent", sa.Integer(), nullable=False),
            sa.Column("max_activations", sa.Integer(), nullable=False),
            sa.Column("activations_count", sa.Integer(), nullable=False),
            sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("target_user_id", sa.Integer(), nullable=True),
            sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "discount_percent >= 1 AND discount_percent <= 99",
                name="ck_promo_discount_range",
            ),
            sa.CheckConstraint("max_activations >= 1", name="ck_promo_max_activations_positive"),
            sa.CheckConstraint(
                "activations_count >= 0 AND activations_count <= max_activations",
                name="ck_promo_activations_within_limit",
            ),
            sa.ForeignKeyConstraint(["target_user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_promo_codes_code"), "promo_codes", ["code"], unique=True)
        op.create_index(op.f("ix_promo_codes_promo_type"), "promo_codes", ["promo_type"], unique=False)
        op.create_index(
            op.f("ix_promo_codes_target_user_id"), "promo_codes", ["target_user_id"], unique=False
        )

    if "promo_code_redemptions" not in tables:
        op.create_table(
            "promo_code_redemptions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("promo_code_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("payment_id", sa.Integer(), nullable=True),
            sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["payment_id"], ["payments.id"]),
            sa.ForeignKeyConstraint(["promo_code_id"], ["promo_codes.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("payment_id"),
            sa.UniqueConstraint("promo_code_id", "user_id", name="uq_redemption_promo_user"),
        )
        op.create_index(
            op.f("ix_promo_code_redemptions_promo_code_id"),
            "promo_code_redemptions",
            ["promo_code_id"],
            unique=False,
        )
        op.create_index(
            op.f("ix_promo_code_redemptions_status"),
            "promo_code_redemptions",
            ["status"],
            unique=False,
        )
        op.create_index(
            op.f("ix_promo_code_redemptions_user_id"),
            "promo_code_redemptions",
            ["user_id"],
            unique=False,
        )

    if "weekly_leaderboards" not in tables:
        op.create_table(
            "weekly_leaderboards",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("week_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("week_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("public_promo_id", sa.Integer(), nullable=True),
            sa.Column("channel_status", sa.String(length=16), nullable=False),
            sa.Column("channel_attempts", sa.Integer(), nullable=False),
            sa.Column("channel_message_id", sa.BigInteger(), nullable=True),
            sa.Column("channel_error", sa.String(length=255), nullable=True),
            sa.Column("channel_posted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["public_promo_id"], ["promo_codes.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("public_promo_id"),
            sa.UniqueConstraint("week_start"),
        )

    if "weekly_leaderboard_entries" not in tables:
        op.create_table(
            "weekly_leaderboard_entries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("leaderboard_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("rank", sa.Integer(), nullable=False),
            sa.Column("report_count", sa.Integer(), nullable=False),
            sa.Column("last_report_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("username_snapshot", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["leaderboard_id"], ["weekly_leaderboards.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("leaderboard_id", "rank", name="uq_entry_leaderboard_rank"),
            sa.UniqueConstraint("leaderboard_id", "user_id", name="uq_entry_leaderboard_user"),
        )
        op.create_index(
            op.f("ix_weekly_leaderboard_entries_leaderboard_id"),
            "weekly_leaderboard_entries",
            ["leaderboard_id"],
            unique=False,
        )
        op.create_index(
            op.f("ix_weekly_leaderboard_entries_user_id"),
            "weekly_leaderboard_entries",
            ["user_id"],
            unique=False,
        )

    if "weekly_rewards" not in tables:
        op.create_table(
            "weekly_rewards",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("leaderboard_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("rank", sa.Integer(), nullable=False),
            sa.Column("report_count", sa.Integer(), nullable=False),
            sa.Column("promo_code_id", sa.Integer(), nullable=False),
            sa.Column("delivery_status", sa.String(length=16), nullable=False),
            sa.Column("delivery_attempts", sa.Integer(), nullable=False),
            sa.Column("delivery_error", sa.String(length=255), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["leaderboard_id"], ["weekly_leaderboards.id"]),
            sa.ForeignKeyConstraint(["promo_code_id"], ["promo_codes.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("leaderboard_id", "rank", name="uq_reward_leaderboard_rank"),
            sa.UniqueConstraint("leaderboard_id", "user_id", name="uq_reward_leaderboard_user"),
            sa.UniqueConstraint("promo_code_id"),
        )
        op.create_index(
            op.f("ix_weekly_rewards_leaderboard_id"), "weekly_rewards", ["leaderboard_id"], unique=False
        )
        op.create_index(op.f("ix_weekly_rewards_user_id"), "weekly_rewards", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_weekly_rewards_user_id"), table_name="weekly_rewards")
    op.drop_index(op.f("ix_weekly_rewards_leaderboard_id"), table_name="weekly_rewards")
    op.drop_table("weekly_rewards")
    op.drop_index(
        op.f("ix_weekly_leaderboard_entries_user_id"), table_name="weekly_leaderboard_entries"
    )
    op.drop_index(
        op.f("ix_weekly_leaderboard_entries_leaderboard_id"), table_name="weekly_leaderboard_entries"
    )
    op.drop_table("weekly_leaderboard_entries")
    op.drop_table("weekly_leaderboards")
    op.drop_index(op.f("ix_promo_code_redemptions_user_id"), table_name="promo_code_redemptions")
    op.drop_index(op.f("ix_promo_code_redemptions_status"), table_name="promo_code_redemptions")
    op.drop_index(
        op.f("ix_promo_code_redemptions_promo_code_id"), table_name="promo_code_redemptions"
    )
    op.drop_table("promo_code_redemptions")
    op.drop_index(op.f("ix_promo_codes_target_user_id"), table_name="promo_codes")
    op.drop_index(op.f("ix_promo_codes_promo_type"), table_name="promo_codes")
    op.drop_index(op.f("ix_promo_codes_code"), table_name="promo_codes")
    op.drop_table("promo_codes")
    op.drop_index("ix_analyses_report_completed_at", table_name="analyses")
    with op.batch_alter_table("analyses") as batch:
        batch.drop_column("report_completed_at")
