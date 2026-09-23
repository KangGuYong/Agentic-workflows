"""Knowledge bases: files, ingest jobs and chunk vectors (knowledge-base design §3).

Revision ID: 0004
Revises: 0003

Vectors live only here. A node output never carries one (the 1 MB output cap and per-step checkpoint
copies make that a bad idea), so `kb_search` reads this table and returns text.
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UP = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE knowledge_bases (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    embed_model text NOT NULL,
    dim integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name)
);

CREATE TABLE kb_files (
    id uuid PRIMARY KEY,
    kb_id uuid NOT NULL REFERENCES knowledge_bases (id) ON DELETE CASCADE,
    filename text NOT NULL,
    media_type text NOT NULL,
    size bigint NOT NULL,
    content bytea NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','processing','ready','failed')),
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX kb_files_kb_idx ON kb_files (kb_id, created_at DESC);

-- One job per file; deleted when the file is ready or has finally failed. The lease columns mirror
-- runs.lease_owner/lease_expires_at so the ingester can reuse the worker's claim pattern.
CREATE TABLE ingest_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id uuid NOT NULL UNIQUE REFERENCES kb_files (id) ON DELETE CASCADE,
    attempt integer NOT NULL DEFAULT 0,
    lease_owner text,
    lease_until timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ingest_jobs_queue_idx ON ingest_jobs (next_attempt_at, created_at);

-- v1 pins 1024 dimensions (bge-m3). knowledge_bases.dim is checked against every embedding response.
CREATE TABLE kb_chunks (
    id bigserial PRIMARY KEY,
    kb_id uuid NOT NULL REFERENCES knowledge_bases (id) ON DELETE CASCADE,
    file_id uuid NOT NULL REFERENCES kb_files (id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    heading text,
    text text NOT NULL,
    embedding vector(1024) NOT NULL
);
CREATE INDEX kb_chunks_kb_idx ON kb_chunks (kb_id);
CREATE INDEX kb_chunks_embedding_idx ON kb_chunks USING hnsw (embedding vector_cosine_ops);
"""

DOWN = """
-- The extension stays: it is shared infrastructure, and dropping it here would break any other
-- relation an operator has since built on it. Dropping the tables removes every vector this
-- migration created.
DROP TABLE IF EXISTS kb_chunks;
DROP TABLE IF EXISTS ingest_jobs;
DROP TABLE IF EXISTS kb_files;
DROP TABLE IF EXISTS knowledge_bases;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
