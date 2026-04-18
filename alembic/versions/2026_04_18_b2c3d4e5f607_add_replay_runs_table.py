"""add_replay_runs_table

Adds the replay_runs table — SQL form of the data/replay_runs.jsonl audit
trail written by src.backtest.day_replay.replay_day. JSONL stays as the
durable, dependency-free record (DATA_RELIABILITY_PLAN §7.7); this table
is the queryable mirror so analysts can answer questions like:

  - Has the golden-day hash drifted in the last 30 days?
  - What's the determinism rate by (code_sha, params_sha)?
  - How often did counterfactual fall back to live params?

When the TimescaleDB extension is enabled (P2), promote with a single
follow-up migration: SELECT create_hypertable('replay_runs', 'run_at').

Revision ID: b2c3d4e5f607
Revises: a1b2c3d4e5f6
Create Date: 2026-04-18 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f607'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'replay_runs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('target_date', sa.Date(), nullable=False),
        sa.Column('strategy', sa.String(length=50), nullable=False),
        sa.Column('underlying', sa.String(length=20), nullable=False),
        sa.Column('seed', sa.Integer(), nullable=False),
        sa.Column('code_sha', sa.String(length=40), nullable=False),
        sa.Column('params_sha', sa.String(length=64), nullable=False),
        sa.Column('params_source', sa.String(length=255), nullable=False),
        sa.Column('snapshot_dir', sa.String(length=255), nullable=True),
        sa.Column('total_pnl', sa.Float(), nullable=True),
        sa.Column('num_days', sa.Integer(), nullable=True),
        sa.Column('snapshot_coverage_pct', sa.Float(), nullable=True),
        sa.Column('fills_via_bid_ask', sa.Integer(), nullable=True),
        sa.Column('fills_via_ltp_slip', sa.Integer(), nullable=True),
        sa.Column('replay_hash', sa.String(length=64), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_replay_runs_run_at'), 'replay_runs', ['run_at'], unique=False)
    op.create_index(op.f('ix_replay_runs_target_date'), 'replay_runs', ['target_date'], unique=False)
    op.create_index(op.f('ix_replay_runs_strategy'), 'replay_runs', ['strategy'], unique=False)
    op.create_index(op.f('ix_replay_runs_replay_hash'), 'replay_runs', ['replay_hash'], unique=False)
    # Composite index for the most common query: "show me hashes for
    # strategy X across last N days" — covers the determinism-rate query.
    op.create_index(
        'ix_replay_runs_strategy_target_date',
        'replay_runs',
        ['strategy', 'target_date'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_replay_runs_strategy_target_date', table_name='replay_runs')
    op.drop_index(op.f('ix_replay_runs_replay_hash'), table_name='replay_runs')
    op.drop_index(op.f('ix_replay_runs_strategy'), table_name='replay_runs')
    op.drop_index(op.f('ix_replay_runs_target_date'), table_name='replay_runs')
    op.drop_index(op.f('ix_replay_runs_run_at'), table_name='replay_runs')
    op.drop_table('replay_runs')
