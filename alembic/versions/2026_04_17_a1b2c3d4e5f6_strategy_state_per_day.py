"""strategy_state_per_day

Adds `as_of_date` to strategy_states and makes (strategy_id, as_of_date) the PK.
This way restart loads today's state only — yesterday's flags can't leak forward
(the Apr 16 IC re-entry root cause).

Revision ID: a1b2c3d4e5f6
Revises: 6334855c1ad2
Create Date: 2026-04-17 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '6334855c1ad2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old single-key constraint, add as_of_date, recreate composite PK.
    op.drop_constraint('strategy_states_pkey', 'strategy_states', type_='primary')
    op.add_column(
        'strategy_states',
        sa.Column('as_of_date', sa.Date(), nullable=False, server_default=sa.text("CURRENT_DATE")),
    )
    op.create_primary_key(
        'strategy_states_pkey', 'strategy_states', ['strategy_id', 'as_of_date']
    )


def downgrade() -> None:
    op.drop_constraint('strategy_states_pkey', 'strategy_states', type_='primary')
    op.drop_column('strategy_states', 'as_of_date')
    op.create_primary_key(
        'strategy_states_pkey', 'strategy_states', ['strategy_id']
    )
