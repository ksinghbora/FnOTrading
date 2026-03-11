"""widen_instrument_name_columns

Revision ID: c2e068eaa83c
Revises: 9a0245401415
Create Date: 2026-03-10 21:55:44.762563

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2e068eaa83c'
down_revision: Union[str, None] = '9a0245401415'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('instruments', 'name',
               existing_type=sa.VARCHAR(length=100),
               type_=sa.String(length=200),
               existing_nullable=False)
    op.alter_column('instruments', 'underlying',
               existing_type=sa.VARCHAR(length=50),
               type_=sa.String(length=200),
               existing_nullable=False)


def downgrade() -> None:
    op.alter_column('instruments', 'underlying',
               existing_type=sa.String(length=200),
               type_=sa.VARCHAR(length=50),
               existing_nullable=False)
    op.alter_column('instruments', 'name',
               existing_type=sa.String(length=200),
               type_=sa.VARCHAR(length=100),
               existing_nullable=False)
