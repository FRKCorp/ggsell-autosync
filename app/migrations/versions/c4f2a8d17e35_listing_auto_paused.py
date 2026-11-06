"""listings.auto_paused: lot paused by the system because FZ has nothing to sell

Revision ID: c4f2a8d17e35
Revises: b7e41c2a9d10
Create Date: 2026-11-06 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4f2a8d17e35'
down_revision: Union[str, None] = 'b7e41c2a9d10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('listings', sa.Column('auto_paused', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('listings', 'auto_paused')
