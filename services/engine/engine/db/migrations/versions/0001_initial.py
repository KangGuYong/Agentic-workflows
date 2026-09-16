"""Application tables (MVP design 3.3).

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE workflows (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    name text NOT NULL,
    draft_dsl jsonb NOT NULL,
    revision integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX workflows_workspace_idx ON workflows (workspace_id, updated_at DESC);

CREATE TABLE workflow_versions (
    id uuid PRIMARY KEY,
    workflow_id uuid NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    workspace_id uuid NOT NULL,
    version_no integer NOT NULL,
    dsl jsonb NOT NULL,
    dsl_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, dsl_hash),
    UNIQUE (workflow_id, version_no)
);

CREATE TABLE runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    workflow_id uuid NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    workflow_version_id uuid NOT NULL REFERENCES workflow_versions (id),
    status text NOT NULL CHECK (status IN ('queued','running','waiting','succeeded','failed','cancelled')),
    inputs jsonb,
    outputs jsonb,
    error jsonb,
    idempotency_key text,
    lease_owner text,
    lease_expires_at timestamptz,
    cancel_requested_at timestamptz,
    waiting_node_id text,
    waiting_exec_index integer,
    resume_payload jsonb,
    retry_count integer NOT NULL DEFAULT 0,
    recovery_count integer NOT NULL DEFAULT 0,
    event_seq bigint NOT NULL DEFAULT 0,
    active_ms bigint NOT NULL DEFAULT 0,
    store_run_data boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);
CREATE UNIQUE INDEX runs_idempotency_idx ON runs (workflow_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX runs_queue_idx ON runs (status, created_at);
CREATE INDEX runs_workflow_idx ON runs (workflow_id, created_at DESC);
CREATE INDEX runs_lease_idx ON runs (lease_expires_at) WHERE status = 'running';
CREATE INDEX runs_waiting_idx ON runs (updated_at) WHERE status = 'waiting';

CREATE TABLE node_runs (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    node_id text NOT NULL,
    exec_index integer NOT NULL,
    attempt integer NOT NULL,
    status text NOT NULL,
    input jsonb,
    output jsonb,
    error jsonb,
    meta jsonb,
    tokens_in integer NOT NULL DEFAULT 0,
    tokens_out integer NOT NULL DEFAULT 0,
    truncated boolean NOT NULL DEFAULT false,
    waited boolean NOT NULL DEFAULT false,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (run_id, node_id, exec_index, attempt)
);
CREATE INDEX node_runs_run_idx ON node_runs (run_id, started_at);

CREATE TABLE run_events (
    run_id uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq bigint NOT NULL,
    type text NOT NULL,
    node_id text,
    exec_index integer,
    attempt integer,
    payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);
"""


def upgrade() -> None:
    op.execute(TABLES)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS run_events, node_runs, runs, workflow_versions, workflows CASCADE")
