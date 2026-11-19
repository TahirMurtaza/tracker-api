"""initial schema

Revision ID: 0001
Revises: 
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')  # for the ILIKE search index
    op.create_table('exchange_rates',
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('valid_on', sa.Date(), nullable=False),
    sa.Column('rate_to_usd', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.CheckConstraint('rate_to_usd > 0', name='ck_rates_positive'),
    sa.PrimaryKeyConstraint('currency', 'valid_on')
    )
    op.create_table('outbox',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('type', sa.String(length=50), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_outbox_pending', 'outbox', ['id'], unique=False, postgresql_where=sa.text('processed_at IS NULL'))
    op.create_table('users',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('base_currency', sa.String(length=3), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('categories',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('monthly_budget', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('monthly_budget IS NULL OR monthly_budget > 0', name='ck_categories_budget_positive'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'name', name='uq_categories_user_name')
    )
    op.create_table('idempotency_keys',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('response_code', sa.Integer(), nullable=True),
    sa.Column('response_body', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'key')
    )
    op.create_index('ix_idempotency_created', 'idempotency_keys', ['created_at'], unique=False)
    op.create_table('import_jobs',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('file_sha256', sa.String(length=64), nullable=False),
    sa.Column('s3_key', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('total', sa.Integer(), nullable=False),
    sa.Column('done', sa.Integer(), nullable=False),
    sa.Column('failed', sa.Integer(), nullable=False),
    sa.Column('errors', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'file_sha256', name='uq_import_jobs_user_file')
    )
    op.create_index('ix_import_jobs_claimable', 'import_jobs', ['status', 'updated_at'], unique=False)
    op.create_table('refresh_tokens',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index('ix_refresh_tokens_user', 'refresh_tokens', ['user_id'], unique=False)
    op.create_table('expenses',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('category_id', sa.UUID(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('spent_on', sa.Date(), nullable=False),
    sa.Column('receipt_key', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('import_id', sa.UUID(), nullable=True),
    sa.Column('import_row', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount > 0', name='ck_expenses_amount_positive'),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['import_id'], ['import_jobs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_expenses_description_trgm', 'expenses', ['description'], unique=False, postgresql_using='gin', postgresql_ops={'description': 'gin_trgm_ops'})
    op.create_index('ix_expenses_user_cat_spent_id', 'expenses', ['user_id', 'category_id', sa.literal_column('spent_on DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_index('ix_expenses_user_spent_id', 'expenses', ['user_id', sa.literal_column('spent_on DESC'), sa.literal_column('id DESC')], unique=False)
    op.create_index('uq_expenses_import_row', 'expenses', ['import_id', 'import_row'], unique=True, postgresql_where=sa.text('import_id IS NOT NULL'))
    op.create_table('notifications',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('category_id', sa.UUID(), nullable=False),
    sa.Column('month', sa.Date(), nullable=False),
    sa.Column('level', sa.SmallInteger(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'category_id', 'month', 'level', name='uq_notifications_once')
    )
    op.create_index('ix_notifications_user_id_desc', 'notifications', ['user_id', sa.literal_column('id DESC')], unique=False)


def downgrade() -> None:
    op.drop_index('ix_notifications_user_id_desc', table_name='notifications')
    op.drop_table('notifications')
    op.drop_index('uq_expenses_import_row', table_name='expenses', postgresql_where=sa.text('import_id IS NOT NULL'))
    op.drop_index('ix_expenses_user_spent_id', table_name='expenses')
    op.drop_index('ix_expenses_user_cat_spent_id', table_name='expenses')
    op.drop_index('ix_expenses_description_trgm', table_name='expenses', postgresql_using='gin', postgresql_ops={'description': 'gin_trgm_ops'})
    op.drop_table('expenses')
    op.drop_index('ix_refresh_tokens_user', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_index('ix_import_jobs_claimable', table_name='import_jobs')
    op.drop_table('import_jobs')
    op.drop_index('ix_idempotency_created', table_name='idempotency_keys')
    op.drop_table('idempotency_keys')
    op.drop_table('categories')
    op.drop_table('users')
    op.drop_index('ix_outbox_pending', table_name='outbox', postgresql_where=sa.text('processed_at IS NULL'))
    op.drop_table('outbox')
    op.drop_table('exchange_rates')
