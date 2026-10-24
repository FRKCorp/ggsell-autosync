"""group listings: listing_variants, listings.is_group/ggsell_option_id, orders.position_id

Revision ID: b7e41c2a9d10
Revises: 9fd28d8963b9
Create Date: 2026-10-24 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e41c2a9d10'
down_revision: Union[str, None] = '9fd28d8963b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'listing_variants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('listing_id', sa.Integer(), nullable=False),
        sa.Column('position_id', sa.Integer(), nullable=False),
        sa.Column('ggsell_variant_id', sa.Integer(), nullable=True),
        sa.Column('price_rub', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('price_source_usd_at_sync', sa.Numeric(precision=16, scale=8), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
        sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['listing_id'], ['listings.id']),
        sa.ForeignKeyConstraint(['position_id'], ['positions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ggsell_variant_id'),
        sa.UniqueConstraint('position_id'),
    )
    op.create_index(op.f('ix_listing_variants_listing_id'), 'listing_variants', ['listing_id'], unique=False)
    op.add_column('listings', sa.Column('is_group', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('listings', sa.Column('ggsell_option_id', sa.Integer(), nullable=True))
    op.add_column('orders', sa.Column('position_id', sa.Integer(), nullable=True))
    op.create_foreign_key('orders_position_id_fkey', 'orders', 'positions', ['position_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('orders_position_id_fkey', 'orders', type_='foreignkey')
    op.drop_column('orders', 'position_id')
    op.drop_column('listings', 'ggsell_option_id')
    op.drop_column('listings', 'is_group')
    op.drop_index(op.f('ix_listing_variants_listing_id'), table_name='listing_variants')
    op.drop_table('listing_variants')
