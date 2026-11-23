"""Enable API keys with endpoint restrictions for existing installations.

Revision ID: 8a64de10b730
Revises: 42e2978c7933
"""

import time

from alembic import op
import sqlalchemy as sa


revision = '8a64de10b730'
down_revision = '42e2978c7933'
branch_labels = None
depends_on = None


def upgrade() -> None:
    config = sa.table(
        'config',
        sa.column('key', sa.Text()),
        sa.column('value', sa.JSON()),
        sa.column('updated_at', sa.BigInteger()),
    )
    # Fresh installs receive these values from DEFAULT_CONFIG. Preserve any custom allowlist.
    op.execute(
        config.update()
        .where(config.c.key.in_(('auth.enable_api_keys', 'auth.api_key.endpoint_restrictions')))
        .values(value=True, updated_at=int(time.time()))
    )


def downgrade() -> None:
    # The previous values cannot be reconstructed without undoing later administrator edits.
    pass
