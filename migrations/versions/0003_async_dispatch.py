"""Durable asynchronous run dispatch and queued lifecycle state."""

from alembic import op
import sqlalchemy as sa

revision = "0003_async_dispatch"
down_revision = "0002_run_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("test_runs") as batch:
        batch.add_column(sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("task_id", sa.Uuid(), nullable=True))
        batch.create_unique_constraint("uq_test_runs_task_id", ["task_id"])
        batch.drop_constraint("ck_test_runs_status", type_="check")
        batch.create_check_constraint("ck_test_runs_status", "status IN ('PENDING', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')")
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


def downgrade():
    op.drop_table("run_dispatches")
    # Never turn queued attempts back into eligible PENDING records implicitly.
    op.execute(sa.text("UPDATE test_runs SET status = 'FAILED', completed_at = CURRENT_TIMESTAMP, "
                       "execution_outcome = 'INFRASTRUCTURE_ERROR', "
                       "error_details = 'Queued dispatch cancelled by schema downgrade.' WHERE status = 'QUEUED'"))
    with op.batch_alter_table("test_runs") as batch:
        batch.drop_constraint("ck_test_runs_status", type_="check")
        batch.create_check_constraint("ck_test_runs_status", "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')")
        batch.drop_constraint("uq_test_runs_task_id", type_="unique")
        batch.drop_column("task_id")
        batch.drop_column("queued_at")
