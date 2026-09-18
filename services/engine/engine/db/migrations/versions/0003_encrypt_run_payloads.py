"""Encrypt the run payload columns (2b design §2, §9).

Revision ID: 0003
Revises: 0002

runs.inputs is the run's initial state, so it cannot be redacted at write time the way node_runs is —
the worker has to read the real value back. Encrypting at rest keeps a database dump from handing over
every run's input, and the read path still redacts before anything reaches a client.

PR #1 and PR #2 are unmerged and nothing is deployed, so there is no data to carry across: the columns
are dropped and recreated rather than converted.
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE runs DROP COLUMN inputs;
ALTER TABLE runs DROP COLUMN outputs;
ALTER TABLE runs ADD COLUMN inputs bytea;
ALTER TABLE runs ADD COLUMN outputs bytea;
"""

DOWN = """
ALTER TABLE runs DROP COLUMN inputs;
ALTER TABLE runs DROP COLUMN outputs;
ALTER TABLE runs ADD COLUMN inputs jsonb;
ALTER TABLE runs ADD COLUMN outputs jsonb;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
