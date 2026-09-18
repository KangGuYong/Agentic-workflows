"""Secrets and retention bookkeeping (2b design §4.1, §7).

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE secrets (
    workspace_id uuid NOT NULL,
    name text NOT NULL CHECK (length(name) <= 64),
    ciphertext bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, name)
);

ALTER TABLE runs ADD COLUMN purged_at timestamptz;
-- The retention sweep asks for finished runs that have not been purged yet, ordered by finished_at
-- (oldest first, matching the reaper's other batches -- engine/worker/reaper.py). Without purged_at in
-- the index it would rescan everything it has already cleaned on every sweep, forever.
CREATE INDEX runs_retention_idx ON runs (finished_at) WHERE purged_at IS NULL AND finished_at IS NOT NULL;
"""

DOWN = """
DROP INDEX runs_retention_idx;
ALTER TABLE runs DROP COLUMN purged_at;
DROP TABLE secrets;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
