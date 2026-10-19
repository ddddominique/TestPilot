"""Create persistent test runs for Phase 4.1."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_create_test_runs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "test_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pr_url", sa.String(2048), nullable=False),
        sa.Column("repository", sa.String(256), nullable=False),
        sa.Column("pr_number", sa.BigInteger(), nullable=False),
        sa.Column("head_sha", sa.String(40), nullable=True),
        sa.Column("status", sa.String(32), server_default="PENDING", nullable=False),
        sa.Column("framework", sa.String(16), nullable=True),
        sa.Column("passed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("generated_tests", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("execution_output", sa.Text(), nullable=True),
        sa.Column("error_details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("pr_number > 0", name="ck_test_runs_pr_number_positive"),
        sa.CheckConstraint("passed >= 0 AND failed >= 0 AND skipped >= 0", name="ck_test_runs_counts_nonnegative"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PASSED', 'TEST_FAILED', 'COLLECTION_ERROR', 'SYNTAX_ERROR', "
            "'DEPENDENCY_ERROR', 'TIMEOUT', 'INFRASTRUCTURE_ERROR', 'UNSUPPORTED')",
            name="ck_test_runs_status",
        ),
    )
    op.create_index("ix_test_runs_created_at_id", "test_runs", ["created_at", "id"])


def downgrade():
    op.drop_index("ix_test_runs_created_at_id", table_name="test_runs")
    op.drop_table("test_runs")
