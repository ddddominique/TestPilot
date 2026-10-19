"""Separate lifecycle state from granular execution outcomes."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0002_run_lifecycle"
down_revision = "0001_create_test_runs"
branch_labels = None
depends_on = None

OUTCOMES = (
    "'PASSED', 'TEST_FAILED', 'COLLECTION_ERROR', 'SYNTAX_ERROR', "
    "'DEPENDENCY_ERROR', 'TIMEOUT', 'INFRASTRUCTURE_ERROR', 'UNSUPPORTED'"
)


def upgrade():
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    with op.batch_alter_table("test_runs") as batch:
        batch.add_column(sa.Column("execution_outcome", sa.String(32), nullable=True))
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("exit_code", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("duration_seconds", sa.Float(), nullable=True))
        batch.add_column(sa.Column("generation_summary", sa.Text(), nullable=True))
        batch.add_column(sa.Column("framework_selection", sa.String(16), nullable=True))
        batch.add_column(sa.Column("framework_evidence", json_type, server_default=sa.text("'[]'"), nullable=False))
        batch.add_column(sa.Column("warnings", json_type, server_default=sa.text("'[]'"), nullable=False))
        batch.drop_constraint("ck_test_runs_status", type_="check")
    # Preserve initial PENDING records and map any legacy outcome statuses.
    op.execute(sa.text(
        "UPDATE test_runs SET execution_outcome = status, "
        "status = CASE WHEN status = 'INFRASTRUCTURE_ERROR' THEN 'FAILED' ELSE 'COMPLETED' END, "
        "started_at = created_at, completed_at = updated_at WHERE status != 'PENDING'"
    ))
    with op.batch_alter_table("test_runs") as batch:
        batch.create_check_constraint("ck_test_runs_status", "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')")
        batch.create_check_constraint("ck_test_runs_execution_outcome", f"execution_outcome IS NULL OR execution_outcome IN ({OUTCOMES})")


def downgrade():
    # Running records cannot be safely made eligible for another execution.
    op.execute(sa.text("UPDATE test_runs SET status = 'FAILED' WHERE status = 'RUNNING'"))
    with op.batch_alter_table("test_runs") as batch:
        batch.drop_constraint("ck_test_runs_status", type_="check")
        batch.drop_constraint("ck_test_runs_execution_outcome", type_="check")
    op.execute(sa.text(
        "UPDATE test_runs SET status = COALESCE(execution_outcome, 'INFRASTRUCTURE_ERROR') "
        "WHERE status != 'PENDING'"
    ))
    with op.batch_alter_table("test_runs") as batch:
        for name in ("warnings", "framework_evidence", "framework_selection", "generation_summary",
                     "duration_seconds", "exit_code", "completed_at", "started_at", "execution_outcome"):
            batch.drop_column(name)
        batch.create_check_constraint("ck_test_runs_status", f"status IN ('PENDING', {OUTCOMES})")
