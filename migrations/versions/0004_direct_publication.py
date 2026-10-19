"""Remove the outbox; the API now publishes directly to Redis."""

from alembic import op
import sqlalchemy as sa

revision = "0004_direct_publication"
down_revision = "0003_async_dispatch"
branch_labels = None
depends_on = None


def upgrade():
    # Preserve all run identities, statuses, timestamps, and results.
    op.drop_table("run_dispatches")


def downgrade():
    op.create_table(
        "run_dispatches",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("test_runs.id"), primary_key=True),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(256), nullable=True),
    )
    op.create_index("ix_run_dispatches_available_at", "run_dispatches", ["available_at"])
    # Metadata was intentionally removed; downgrade does not republish old jobs.
