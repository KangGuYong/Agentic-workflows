# 지식베이스(RAG) 실행 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 문서를 지식베이스에 올려 백그라운드에서 파싱·청킹·임베딩하고, 워크플로의 `kb_search`·`rerank` 노드가 그 내용을 검색·리랭킹해 LLM에 넘길 수 있게 한다.

**Architecture:** 적재는 엔진 실행 밖의 별도 `ingester` 프로세스(같은 이미지, worker와 같은 lease 방식)가 맡고, 벡터는 pgvector에만 산다. 워크플로 노드는 질문을 임베딩해 검색하거나(`kb_search`) 결과를 TEI로 재정렬할 뿐(`rerank`) 벡터를 실행 상태에 싣지 않는다. 외부 서비스(Ollama·MinerU·TEI)는 운영자가 GPU 호스트에 띄우고 엔진은 주소만 받는다.

**Tech Stack:** Python 3.12 · FastAPI · psycopg 3 · pgvector(`pgvector/pgvector:pg17`) · httpx · pytest + testcontainers / Next.js 16 · React 19 · RJSF · vitest · Playwright

**스펙:** `docs/superpowers/specs/2026-09-22-knowledge-base-design.md` · **브랜치:** `feat/knowledge-base` (이미 생성, 스펙 커밋 `090ca68`)

---

## 환경 메모 (모든 태스크 공통)

- `pnpm`이 설치되어 있지 않다. `apps/web`에서는 `npx vitest run`, `npx tsc --noEmit`, `npx eslint`를 쓴다.
- `npx tsc --noEmit`에는 **기존 오류 1건**이 있다: `app/layout.tsx(46,50): error TS2304: Cannot find name 'LayoutProps'`. 무시한다. 그 밖의 오류는 이번 변경의 것이다.
- web 기준선: vitest 63 files / 828 tests, eslint exit 0. engine 기준선: `uv run pytest -q` 1382 passed + `tests/test_worker_run.py::test_a_duplicate_attempt_is_treated_as_a_lost_lease` 1건은 전체 실행에서만 간헐 실패(단독 실행 시 통과).
- engine 테스트는 `services/engine`에서 `uv run pytest ...`. DB가 필요한 테스트(`pool`, `api`, `worker_factory` 픽스처)는 Docker(testcontainers)를 쓴다.
- 커밋 트레일러: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
- 주석은 기존 코드처럼 *왜*를 설명하는 짧은 문장으로 쓴다. 한국어 사용자 메시지, 영어 코드 주석.

## 파일 맵

| 파일 | 변경 | 책임 |
|---|---|---|
| `services/engine/engine/db/migrations/versions/0004_knowledge_base.py` | 생성 | pgvector 확장 + 4개 테이블 |
| `services/engine/tests/conftest.py` | 수정 | pgvector 이미지, TRUNCATE 목록 |
| `services/engine/engine/config.py`, `tests/helpers.py` | 수정 | 새 설정 6개 |
| `services/engine/engine/llm/{base,ollama,gateway,semaphore,scripted}.py` | 수정 | `embed()` |
| `services/engine/engine/kb/__init__.py` | 생성 | `IngestError` |
| `services/engine/engine/kb/chunk.py` | 생성 | 마크다운 청커(순수 함수) |
| `services/engine/engine/kb/mineru.py` | 생성 | MinerU 클라이언트 |
| `services/engine/engine/kb/rerank.py` | 생성 | TEI 리랭커 클라이언트 |
| `services/engine/engine/kb/store.py` | 생성 | 지식베이스·파일·작업·청크 DB 접근, 벡터 검색, `PostgresKnowledgeBases` 포트 |
| `services/engine/engine/ingest/{__init__,worker,main}.py` | 생성 | ingester 프로세스 |
| `services/engine/engine/nodes/{kb_search,rerank}.py`, `base.py`, `registry.py` | 생성/수정 | 노드 두 개와 포트 |
| `services/engine/engine/runtime/deps.py`, `compiler/wrapper.py`, `worker/{worker,main}.py` | 수정 | 포트 배선 |
| `services/engine/engine/api/routers/knowledge_bases.py`, `api/app.py` | 생성/수정 | API |
| `deploy/docker-compose.yml`, `deploy/.env.example`, `services/engine/README.md` | 수정 | ingester 서비스, 환경변수 |
| `apps/web/lib/engine/paths.ts`, `lib/engine/knowledgeBases.ts` | 수정/생성 | 프록시 허용, 브라우저 클라이언트 |
| `apps/web/components/knowledge/KnowledgeBasesScreen.tsx`, `app/knowledge-bases/page.tsx`, `components/shell/Shell.tsx` | 생성/수정 | 화면 |
| `apps/web/lib/panel/uiSchema.ts`, `components/panel/widgets.tsx` | 수정 | 지식베이스 드롭다운 |
| `apps/web/e2e/stack/knowledge-base.spec.ts` | 생성 | 스택 e2e |

---

### Task 1: pgvector 스키마

**Files:**
- Create: `services/engine/engine/db/migrations/versions/0004_knowledge_base.py`
- Modify: `services/engine/tests/conftest.py:12`, `:56`
- Test: `services/engine/tests/test_db_schema.py`

- [ ] **Step 1: 실패하는 테스트**

`tests/test_db_schema.py` 끝에 추가:
```python
async def test_knowledge_base_tables_exist_with_the_vector_extension(pool):
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        )).fetchall()
        ext = await (await conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()

    names = {row["table_name"] for row in rows}
    assert {"knowledge_bases", "kb_files", "ingest_jobs", "kb_chunks"} <= names
    assert ext is not None


async def test_deleting_a_file_cascades_to_its_chunks_and_job(pool):
    async with pool.connection() as conn:
        kb = await (await conn.execute(
            "INSERT INTO knowledge_bases (id, workspace_id, name, embed_model, dim)"
            " VALUES (gen_random_uuid(), %s, 'kb', 'bge-m3', 1024) RETURNING id",
            ("00000000-0000-0000-0000-000000000001",),
        )).fetchone()
        file = await (await conn.execute(
            "INSERT INTO kb_files (id, kb_id, filename, media_type, size, content, status)"
            " VALUES (gen_random_uuid(), %s, 'a.md', 'text/markdown', 1, %s, 'pending') RETURNING id",
            (kb["id"], b"x"),
        )).fetchone()
        await conn.execute("INSERT INTO ingest_jobs (file_id) VALUES (%s)", (file["id"],))
        await conn.execute(
            "INSERT INTO kb_chunks (kb_id, file_id, ordinal, heading, text, embedding)"
            " VALUES (%s, %s, 0, NULL, 'hi', %s::vector)",
            (kb["id"], file["id"], "[" + ",".join(["0"] * 1024) + "]"),
        )
        await conn.execute("DELETE FROM kb_files WHERE id=%s", (file["id"],))
        chunks = await (await conn.execute("SELECT count(*) AS n FROM kb_chunks")).fetchone()
        jobs = await (await conn.execute("SELECT count(*) AS n FROM ingest_jobs")).fetchone()
    assert (chunks["n"], jobs["n"]) == (0, 0)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_db_schema.py -q`
Expected: 새 테스트 2개 FAIL (테이블 없음).

- [ ] **Step 3: testcontainers 이미지와 TRUNCATE 목록**

`tests/conftest.py`:
```python
APP_TABLES = ("run_events", "node_runs", "runs", "workflow_versions", "workflows", "secrets",
              "kb_chunks", "ingest_jobs", "kb_files", "knowledge_bases")
```
그리고 `with PostgresContainer("postgres:17-alpine", driver=None) as container:` 를
`with PostgresContainer("pgvector/pgvector:pg17", driver=None) as container:` 로 바꾼다.

- [ ] **Step 4: 마이그레이션**

`engine/db/migrations/versions/0004_knowledge_base.py`:
```python
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
DROP TABLE IF EXISTS kb_chunks;
DROP TABLE IF EXISTS ingest_jobs;
DROP TABLE IF EXISTS kb_files;
DROP TABLE IF EXISTS knowledge_bases;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_db_schema.py -q`
Expected: 모두 PASS.

- [ ] **Step 6: 커밋**

```bash
git add services/engine/engine/db/migrations/versions/0004_knowledge_base.py services/engine/tests/conftest.py services/engine/tests/test_db_schema.py
git commit -m "feat(engine): knowledge base tables on pgvector" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: 설정값

**Files:**
- Modify: `services/engine/engine/config.py`, `services/engine/tests/helpers.py:15-22`
- Test: `services/engine/tests/test_config.py`

- [ ] **Step 1: 실패하는 테스트**

`tests/test_config.py` 끝에 추가:
```python
def test_knowledge_base_settings_have_defaults_and_read_the_environment(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)

    config = load_config()
    assert (config.mineru_base_url, config.rerank_base_url) == (None, None)
    assert (config.mineru_timeout_sec, config.kb_max_file_bytes, config.ingest_max_jobs) == (600.0, 50_000_000, 1)
    assert config.kb_embed_model == "bge-m3"

    monkeypatch.setenv("MINERU_BASE_URL", "http://mineru:8000/")
    monkeypatch.setenv("RERANK_BASE_URL", "http://tei:80")
    monkeypatch.setenv("KB_MAX_FILE_BYTES", "1024")
    monkeypatch.setenv("INGEST_MAX_JOBS", "2")
    monkeypatch.setenv("KB_EMBED_MODEL", "nomic-embed-text")
    config = load_config()
    assert (config.mineru_base_url, config.rerank_base_url) == ("http://mineru:8000/", "http://tei:80")
    assert (config.kb_max_file_bytes, config.ingest_max_jobs, config.kb_embed_model) == (1024, 2, "nomic-embed-text")


def test_a_zero_file_limit_is_refused(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("KB_MAX_FILE_BYTES", "0")
    with pytest.raises(ConfigError):
        load_config()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_config.py -q`  Expected: 2 FAIL (`AttributeError`).

- [ ] **Step 3: 구현**

`engine/config.py`의 `EngineConfig` 끝(`purge_batch: int` 아래)에 추가:
```python
    # Knowledge base (knowledge-base design §2). All optional: an engine without MinerU still ingests
    # .md/.txt, and one without a reranker runs every workflow that has no rerank node.
    mineru_base_url: str | None
    mineru_timeout_sec: float
    rerank_base_url: str | None
    kb_max_file_bytes: int
    ingest_max_jobs: int
    kb_embed_model: str
```
`load_config()`의 `return EngineConfig(` 안, `purge_batch=...` 뒤에 추가:
```python
        mineru_base_url=os.getenv("MINERU_BASE_URL") or None,
        mineru_timeout_sec=float(_bounded_int("MINERU_TIMEOUT_SEC", 600, minimum=1, maximum=86_400)),
        rerank_base_url=os.getenv("RERANK_BASE_URL") or None,
        # 1 byte .. 1 GB: 0 would refuse every upload, unbounded would let one upload fill the database.
        kb_max_file_bytes=_bounded_int("KB_MAX_FILE_BYTES", 50_000_000, minimum=1, maximum=1_000_000_000),
        ingest_max_jobs=_bounded_int("INGEST_MAX_JOBS", 1, minimum=1, maximum=64),
        kb_embed_model=os.getenv("KB_EMBED_MODEL") or "bge-m3",
```
`tests/helpers.py`의 `_BASE_CONFIG` 마지막 인자 `db_pool_max=10, retention_days=30, purge_batch=100,` 뒤에 추가:
```python
    mineru_base_url=None, mineru_timeout_sec=5.0, rerank_base_url=None, kb_max_file_bytes=1000,
    ingest_max_jobs=1, kb_embed_model="bge-m3",
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_config.py tests/test_nodes_basic.py -q`  Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add services/engine/engine/config.py services/engine/tests/helpers.py services/engine/tests/test_config.py
git commit -m "feat(engine): knowledge base settings" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `LLMClient.embed()`

**Files:**
- Modify: `services/engine/engine/llm/base.py`, `ollama.py`, `gateway.py`, `semaphore.py`, `scripted.py`
- Test: `services/engine/tests/test_llm_ollama.py`, `tests/test_llm_scripted.py`

- [ ] **Step 1: 실패하는 테스트**

`tests/test_llm_ollama.py` 끝에 추가:
```python
async def test_embed_posts_the_texts_and_returns_one_vector_each():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    vectors = await _raw(handler).embed(model="bge-m3", texts=["a", "b"])

    assert seen["url"] == "http://ollama:11434/api/embed"
    assert seen["body"] == {"model": "bge-m3", "input": ["a", "b"]}
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


async def test_embed_treats_a_wrong_count_or_shape_as_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1]]})

    with pytest.raises(NodeError) as caught:
        await _raw(handler).embed(model="bge-m3", texts=["a", "b"])
    assert caught.value.code == ErrorCode.LLM_UNAVAILABLE and caught.value.retryable


async def test_embed_maps_a_missing_model_to_a_non_retryable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    with pytest.raises(NodeError) as caught:
        await _raw(handler).embed(model="nope", texts=["a"])
    assert not caught.value.retryable
```
`tests/test_llm_scripted.py` 끝에 추가:
```python
async def test_scripted_embed_is_deterministic_and_orthogonal_for_different_texts():
    from engine.llm.scripted import ScriptedLLM

    llm = ScriptedLLM([])
    [a1, a2, b] = await llm.embed(model="m", texts=["같은 글", "같은 글", "다른 글"])

    assert len(a1) == 1024 and a1 == a2 and sum(a1) == 1.0
    assert sum(x * y for x, y in zip(a1, b)) == 0.0
    assert llm.embed_calls == [("m", ["같은 글", "같은 글", "다른 글"])]
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_llm_ollama.py tests/test_llm_scripted.py -q`  Expected: 4 FAIL.

- [ ] **Step 3: 구현**

`engine/llm/base.py` — `LLMClient`와 `RawLLM` 두 Protocol 각각에 메서드 추가:
```python
    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        """One vector per text, in order. Raises NodeError like `chat` does."""
        ...
```
`engine/llm/ollama.py` — `OllamaRaw`에 메서드 추가(`complete` 아래):
```python
    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        try:
            response = await self._client.post(f"{self._base}/api/embed", json={"model": model, "input": texts})
        except httpx.RequestError as exc:
            raise _unavailable(f"Ollama 연결 실패: {exc}") from exc
        if response.status_code >= 400:
            _raise_for_status(response.status_code, response.text[:200], model)
        data = _decode(response.text)
        vectors = data.get("embeddings")
        if (not isinstance(vectors, list) or len(vectors) != len(texts)
                or not all(isinstance(v, list) and all(isinstance(x, (int, float)) for x in v) for v in vectors)):
            raise _unavailable("Ollama 임베딩 응답 형식이 올바르지 않습니다")
        return [[float(x) for x in v] for v in vectors]
```
`engine/llm/gateway.py` — `LLMGateway`에 추가:
```python
    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        return await self._raw.embed(model=model, texts=texts)
```
`engine/llm/semaphore.py` — `SemaphoreLLM`에 추가:
```python
    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        async with self._semaphore.slot(model):
            return await self._inner.embed(model=model, texts=texts)
```
`engine/llm/scripted.py` — `__init__`에 `self.embed_calls: list[tuple[str, list[str]]] = []` 추가하고 클래스에 메서드 추가:
```python
    EMBED_DIM = 1024  # what the kb_chunks column holds

    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        """A one-hot vector per text, keyed by the text: equal texts are identical, different texts are
        orthogonal (almost always), so a search test can predict every cosine score."""
        self.embed_calls.append((model, list(texts)))
        vectors = []
        for text in texts:
            vector = [0.0] * self.EMBED_DIM
            vector[sum(map(ord, text)) % self.EMBED_DIM] = 1.0
            vectors.append(vector)
        return vectors
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_llm_ollama.py tests/test_llm_scripted.py tests/test_llm_gateway.py tests/test_llm_semaphore.py -q`  Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add services/engine/engine/llm services/engine/tests/test_llm_ollama.py services/engine/tests/test_llm_scripted.py
git commit -m "feat(engine): embed() on the LLM client, backed by Ollama /api/embed" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: 마크다운 청커

**Files:**
- Create: `services/engine/engine/kb/__init__.py`, `services/engine/engine/kb/chunk.py`
- Test: `services/engine/tests/test_kb_chunk.py`

- [ ] **Step 1: 실패하는 테스트**

`tests/test_kb_chunk.py`:
```python
from engine.kb.chunk import Chunk, chunk_markdown


def test_headings_split_sections_and_name_each_chunk():
    text = "# 제목\n\n첫 단락.\n\n## 소제목\n\n둘째 단락.\n"
    assert chunk_markdown(text) == [
        Chunk(heading="제목", text="첫 단락."),
        Chunk(heading="소제목", text="둘째 단락."),
    ]


def test_text_before_any_heading_has_no_heading():
    assert chunk_markdown("머리말\n\n# 제목\n본문") == [
        Chunk(heading=None, text="머리말"),
        Chunk(heading="제목", text="본문"),
    ]


def test_a_long_section_is_split_with_overlap_at_a_line_break():
    body = "\n".join(f"{i:03d} " + "가" * 20 for i in range(60))  # 60 lines of 24 chars
    chunks = chunk_markdown("# 긴 절\n" + body, max_chars=300, overlap=50)

    assert len(chunks) > 1
    assert all(c.heading == "긴 절" and len(c.text) <= 300 for c in chunks)
    # Every character of the section survives somewhere, and consecutive chunks overlap.
    assert "".join(c.text for c in chunks).replace("\n", "") != ""
    assert chunks[1].text[:24] in chunks[0].text
    assert chunks[-1].text.endswith("059 " + "가" * 20)


def test_whitespace_only_sections_are_dropped():
    assert chunk_markdown("# 빈 절\n\n\n# 다음\n내용") == [Chunk(heading="다음", text="내용")]


def test_empty_input_gives_no_chunks():
    assert chunk_markdown("   \n") == []
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kb_chunk.py -q`  Expected: ImportError.

- [ ] **Step 3: 구현**

`engine/kb/__init__.py`:
```python
"""Knowledge bases: ingestion helpers, the vector store and the clients the nodes use."""
from __future__ import annotations


class IngestError(Exception):
    """One file could not be ingested. `retryable` follows the same rule as NodeError: transport
    failures and 5xx are worth another attempt, a document the parser rejects is not."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
```
`engine/kb/chunk.py`:
```python
"""Markdown → chunks (knowledge-base design §4.2). Pure, so it is tested without a database."""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHARS = 1000
OVERLAP = 150
_HEADING = re.compile(r"^#{1,6}\s+(.*\S)\s*$")


@dataclass(frozen=True)
class Chunk:
    heading: str | None
    text: str


def chunk_markdown(markdown: str, *, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading: str | None = None
    lines: list[str] = []

    def flush() -> None:
        body = "\n".join(lines).strip()
        lines.clear()
        for piece in _split(body, max_chars, overlap):
            chunks.append(Chunk(heading, piece))

    for line in markdown.splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            heading = match.group(1)
        else:
            lines.append(line)
    flush()
    return chunks


def _split(body: str, max_chars: int, overlap: int) -> list[str]:
    """Windows of at most `max_chars`, cut at the last line break in the window when there is one past
    its midpoint, each starting `overlap` characters before the previous one ended."""
    pieces: list[str] = []
    start = 0
    while start < len(body):
        end = min(start + max_chars, len(body))
        if end < len(body):
            cut = body.rfind("\n", start + max_chars // 2, end)
            if cut != -1:
                end = cut
        piece = body[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(body):
            break
        start = max(end - overlap, start + 1)  # always advances, even when overlap >= the window
    return pieces
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kb_chunk.py -q`  Expected: PASS. 통과하지 않으면 `_split`의 겹침 계산을 테스트 기대에 맞춰 고친다(테스트를 고치지 않는다).

- [ ] **Step 5: 커밋**

```bash
git add services/engine/engine/kb services/engine/tests/test_kb_chunk.py
git commit -m "feat(engine): markdown chunker for knowledge base ingestion" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: MinerU 클라이언트

**Files:**
- Create: `services/engine/engine/kb/mineru.py`
- Test: `services/engine/tests/test_kb_mineru.py`

MinerU 2.x의 API 서버(`mineru-api`)는 `POST /file_parse`에 multipart(`files`)를 받고
`{"results": {"<파일명 stem>": {"md_content": "..."}}}`를 돌려준다. **Step 0에서 배포본과 대조**한다.

- [ ] **Step 0: 배포된 MinerU의 계약 확인**

운영자에게 받은 `MINERU_BASE_URL`로 `curl -s $MINERU_BASE_URL/openapi.json | python -m json.tool | grep -n "file_parse\|md_content"`를 실행한다. 경로가 `/file_parse`가 아니거나 응답 키가 `md_content`가 아니면 아래 코드의 `PARSE_PATH`와 `_markdown_of`만 그에 맞춰 바꾸고, 테스트의 handler도 같은 모양으로 맞춘다. 주소를 아직 받지 못했으면 아래 기본값으로 진행하고 커밋 메시지 본문에 "MinerU API contract assumed from mineru 2.x /file_parse; verify against the deployment"라고 적는다.

- [ ] **Step 1: 실패하는 테스트**

`tests/test_kb_mineru.py`:
```python
import httpx
import pytest

from engine.kb import IngestError
from engine.kb.mineru import MineruParser


def _parser(handler) -> MineruParser:
    return MineruParser("http://mineru:8000/", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_sends_the_file_as_multipart_and_returns_markdown():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200, json={"results": {"report": {"md_content": "# 보고서\n\n본문"}}})

    markdown = await _parser(handler).to_markdown("report.pdf", "application/pdf", b"%PDF-1.4")

    assert seen["url"] == "http://mineru:8000/file_parse"
    assert seen["content_type"].startswith("multipart/form-data")
    assert b'filename="report.pdf"' in seen["body"] and b"%PDF-1.4" in seen["body"]
    assert markdown == "# 보고서\n\n본문"


async def test_a_4xx_is_not_retryable_and_names_the_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="unsupported")

    with pytest.raises(IngestError) as caught:
        await _parser(handler).to_markdown("a.xyz", "application/octet-stream", b"?")
    assert not caught.value.retryable and "422" in caught.value.message


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("down")


@pytest.mark.parametrize("handler", [lambda r: httpx.Response(503), _down])
async def test_5xx_and_transport_failures_are_retryable(handler):
    with pytest.raises(IngestError) as caught:
        await _parser(handler).to_markdown("a.pdf", "application/pdf", b"x")
    assert caught.value.retryable


async def test_a_response_without_markdown_is_not_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": {}})

    with pytest.raises(IngestError) as caught:
        await _parser(handler).to_markdown("a.pdf", "application/pdf", b"x")
    assert not caught.value.retryable
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kb_mineru.py -q`  Expected: ImportError.

- [ ] **Step 3: 구현**

`engine/kb/mineru.py`:
```python
"""MinerU API client (knowledge-base design §2): a document in, markdown out.

The server address is an operator setting, not tenant data, so it is outside the http_request egress
policy on purpose -- the same standing as OLLAMA_BASE_URL.
"""
from __future__ import annotations

from typing import Any

import httpx

from engine.kb import IngestError

PARSE_PATH = "/file_parse"
CONNECT_TIMEOUT = 10.0


class MineruParser:
    def __init__(self, base_url: str, *, timeout: float = 600.0, client: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def to_markdown(self, filename: str, media_type: str, content: bytes) -> str:
        try:
            response = await self._client.post(
                f"{self._base}{PARSE_PATH}", files={"files": (filename, content, media_type)}
            )
        except httpx.RequestError as exc:
            raise IngestError(f"MinerU 연결 실패: {exc}", retryable=True) from exc
        if response.status_code >= 500:
            raise IngestError(f"MinerU 오류 HTTP {response.status_code}", retryable=True)
        if response.status_code >= 400:
            raise IngestError(f"문서를 읽지 못했습니다 (MinerU {response.status_code})", retryable=False)
        try:
            data = response.json()
        except ValueError:
            raise IngestError("MinerU 응답을 해석할 수 없습니다", retryable=True) from None
        markdown = _markdown_of(data)
        if not markdown or not markdown.strip():
            raise IngestError("문서에서 텍스트를 찾지 못했습니다", retryable=False)
        return markdown


def _markdown_of(data: Any) -> str | None:
    """mineru-api answers {"results": {"<stem>": {"md_content": ...}}}; one file in, one result out."""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, dict) or not results:
        return None
    first = next(iter(results.values()))
    text = first.get("md_content") if isinstance(first, dict) else None
    return text if isinstance(text, str) else None
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kb_mineru.py -q`  Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add services/engine/engine/kb/mineru.py services/engine/tests/test_kb_mineru.py
git commit -m "feat(engine): MinerU parser client" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: 지식베이스 저장소 (`engine/kb/store.py`)

**Files:**
- Create: `services/engine/engine/kb/store.py`
- Test: `services/engine/tests/test_kb_store.py`

- [ ] **Step 1: 실패하는 테스트**

`tests/test_kb_store.py`:
```python
"""The knowledge base store: rows, the ingest lease, and cosine search (knowledge-base design §3)."""
import pytest
from psycopg.errors import UniqueViolation

from engine.kb import store
from engine.llm.scripted import ScriptedLLM


def _one_hot(text: str) -> list[float]:
    vector = [0.0] * ScriptedLLM.EMBED_DIM
    vector[sum(map(ord, text)) % ScriptedLLM.EMBED_DIM] = 1.0
    return vector


async def _kb(pool, name="kb") -> str:
    async with pool.connection() as conn:
        return str((await store.create_kb(conn, name=name, embed_model="bge-m3", dim=1024))["id"])


async def test_a_knowledge_base_name_is_unique(pool):
    await _kb(pool, "같은 이름")
    async with pool.connection() as conn:
        with pytest.raises(UniqueViolation):
            await store.create_kb(conn, name="같은 이름", embed_model="bge-m3", dim=1024)


async def test_listing_counts_files_and_never_returns_content(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"# a")
        listed = await store.list_kbs(conn)
        files = await store.list_files(conn, kb_id)
    assert listed[0]["file_count"] == 1
    assert files[0]["status"] == "pending" and "content" not in files[0]


async def test_add_file_queues_exactly_one_job_and_claim_takes_it_once(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x"))["id"])
        first = await store.claim_job(conn, owner="w1", lease_sec=30)
        second = await store.claim_job(conn, owner="w2", lease_sec=30)
        row = await store.get_file(conn, file_id)
    assert str(first["file_id"]) == file_id and first["attempt"] == 1
    assert second is None
    assert row["status"] == "processing"


async def test_an_expired_lease_is_claimed_again_with_a_higher_attempt(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x")
        job = await store.claim_job(conn, owner="dead", lease_sec=30)
        await conn.execute("UPDATE ingest_jobs SET lease_until = now() - interval '1 second' WHERE id=%s", (job["id"],))
        again = await store.claim_job(conn, owner="alive", lease_sec=30)
    assert again is not None and again["attempt"] == 2


async def test_release_for_retry_delays_the_next_claim(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x")
        job = await store.claim_job(conn, owner="w", lease_sec=30)
        await store.release_for_retry(conn, job_id=str(job["id"]), owner="w", delay_sec=3600)
        assert await store.claim_job(conn, owner="w", lease_sec=30) is None
        await conn.execute("UPDATE ingest_jobs SET next_attempt_at = now()")
        assert await store.claim_job(conn, owner="w", lease_sec=30) is not None


async def test_replace_chunks_is_idempotent_and_marks_the_file_ready(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x"))["id"])
        job = await store.claim_job(conn, owner="w", lease_sec=30)
        rows = [("h", "하나", _one_hot("하나")), ("h", "둘", _one_hot("둘"))]
        assert await store.replace_chunks(conn, file_id=file_id, kb_id=kb_id, chunks=rows) is True
        assert await store.replace_chunks(conn, file_id=file_id, kb_id=kb_id, chunks=rows) is True
        count = (await (await conn.execute("SELECT count(*) AS n FROM kb_chunks WHERE file_id=%s", (file_id,))).fetchone())["n"]
        assert await store.finish_job(conn, job_id=str(job["id"]), owner="w", file_id=file_id, error=None)
        row = await store.get_file(conn, file_id)
        jobs = (await (await conn.execute("SELECT count(*) AS n FROM ingest_jobs")).fetchone())["n"]
    assert count == 2 and row["status"] == "ready" and jobs == 0


async def test_finish_job_with_an_error_marks_the_file_failed(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x"))["id"])
        job = await store.claim_job(conn, owner="w", lease_sec=30)
        await store.finish_job(conn, job_id=str(job["id"]), owner="w", file_id=file_id, error="문서를 읽지 못했습니다")
        row = await store.get_file(conn, file_id)
    assert (row["status"], row["error"]) == ("failed", "문서를 읽지 못했습니다")


async def test_replace_chunks_reports_a_file_that_was_deleted_meanwhile(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x"))["id"])
        await store.delete_file(conn, file_id)
        assert await store.replace_chunks(conn, file_id=file_id, kb_id=kb_id, chunks=[("h", "t", _one_hot("t"))]) is False


async def test_search_orders_by_cosine_similarity_and_returns_text_only(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="doc.md", media_type="text/markdown", content=b"x"))["id"])
        await store.replace_chunks(conn, file_id=file_id, kb_id=kb_id,
                                   chunks=[("절", "정답 문장", _one_hot("정답 문장")), (None, "다른 문장", _one_hot("다른 문장"))])
        hits = await store.search(conn, kb_id=kb_id, embedding=_one_hot("정답 문장"), top_k=5)
    assert [h["text"] for h in hits] == ["정답 문장", "다른 문장"]
    assert hits[0] == {"text": "정답 문장", "score": pytest.approx(1.0), "heading": "절", "file": "doc.md"}
    assert hits[1]["score"] == pytest.approx(0.0)


async def test_get_kb_rejects_a_non_uuid_without_touching_the_database(pool):
    async with pool.connection() as conn:
        assert await store.get_kb(conn, "not-a-uuid") is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kb_store.py -q`  Expected: ImportError.

- [ ] **Step 3: 구현**

`engine/kb/store.py`:
```python
"""Knowledge base rows, the ingest lease and cosine search (knowledge-base design §3, §4.1).

Vectors are passed to Postgres as their text form (`'[0.1,0.2,...]'::vector`) and never read back, so
no pgvector client library is needed. Callers manage transactions; nothing here commits on its own.
"""
from __future__ import annotations

import uuid
from typing import Any, Protocol

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from engine.db.workflows import WORKSPACE

TEXT_TYPES = ("text/markdown", "text/plain")
TEXT_SUFFIXES = (".md", ".txt")


def _uuid(raw: str) -> str | None:
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return None


def vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def is_text_file(filename: str, media_type: str) -> bool:
    """Files the ingester reads as UTF-8 markdown itself, without MinerU."""
    return media_type.split(";")[0].strip() in TEXT_TYPES or filename.lower().endswith(TEXT_SUFFIXES)


# ------------------------------------------------------------------ knowledge bases

async def create_kb(conn: AsyncConnection, *, name: str, embed_model: str, dim: int) -> dict[str, Any]:
    return await (await conn.execute(
        "INSERT INTO knowledge_bases (id, workspace_id, name, embed_model, dim) VALUES (%s, %s, %s, %s, %s)"
        " RETURNING id, name, embed_model, dim, created_at",
        (str(uuid.uuid4()), WORKSPACE, name, embed_model, dim),
    )).fetchone()


async def get_kb(conn: AsyncConnection, kb_id: str) -> dict[str, Any] | None:
    kb_id = _uuid(kb_id)
    if kb_id is None:
        return None
    return await (await conn.execute("SELECT * FROM knowledge_bases WHERE id=%s", (kb_id,))).fetchone()


async def list_kbs(conn: AsyncConnection) -> list[dict[str, Any]]:
    return await (await conn.execute(
        "SELECT k.id, k.name, k.embed_model, k.created_at,"
        "   (SELECT count(*) FROM kb_files f WHERE f.kb_id = k.id) AS file_count"
        " FROM knowledge_bases k WHERE k.workspace_id=%s ORDER BY k.created_at DESC", (WORKSPACE,),
    )).fetchall()


async def delete_kb(conn: AsyncConnection, kb_id: str) -> bool:
    kb_id = _uuid(kb_id)
    if kb_id is None:
        return False
    row = await (await conn.execute("DELETE FROM knowledge_bases WHERE id=%s RETURNING id", (kb_id,))).fetchone()
    return row is not None


# ------------------------------------------------------------------ files and jobs

async def add_file(conn: AsyncConnection, *, kb_id: str, filename: str, media_type: str,
                   content: bytes) -> dict[str, Any]:
    """The file row and its job, in one statement each; the caller wraps them in a transaction."""
    row = await (await conn.execute(
        "INSERT INTO kb_files (id, kb_id, filename, media_type, size, content, status)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'pending') RETURNING id, filename, size, status, created_at",
        (str(uuid.uuid4()), kb_id, filename, media_type, len(content), content),
    )).fetchone()
    await conn.execute("INSERT INTO ingest_jobs (file_id) VALUES (%s)", (row["id"],))
    return row


async def get_file(conn: AsyncConnection, file_id: str) -> dict[str, Any] | None:
    file_id = _uuid(file_id)
    if file_id is None:
        return None
    return await (await conn.execute("SELECT * FROM kb_files WHERE id=%s", (file_id,))).fetchone()


async def list_files(conn: AsyncConnection, kb_id: str) -> list[dict[str, Any]]:
    return await (await conn.execute(
        "SELECT id, filename, media_type, size, status, error, created_at, updated_at"
        " FROM kb_files WHERE kb_id=%s ORDER BY created_at DESC", (kb_id,),
    )).fetchall()


async def delete_file(conn: AsyncConnection, file_id: str) -> bool:
    file_id = _uuid(file_id)
    if file_id is None:
        return False
    row = await (await conn.execute("DELETE FROM kb_files WHERE id=%s RETURNING id", (file_id,))).fetchone()
    return row is not None


async def claim_job(conn: AsyncConnection, *, owner: str, lease_sec: int) -> dict[str, Any] | None:
    """Take the oldest due job whose lease is free or expired, like runs.claim_next. The attempt counter
    moves here, so a crash mid-job (lease expiry) counts as an attempt too."""
    row = await (await conn.execute(
        "UPDATE ingest_jobs SET lease_owner=%(owner)s, attempt=attempt + 1,"
        "   lease_until=now() + make_interval(secs => %(lease)s)"
        " WHERE id = (SELECT id FROM ingest_jobs"
        "             WHERE (lease_until IS NULL OR lease_until < now()) AND next_attempt_at <= now()"
        "             ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)"
        " RETURNING id, file_id, attempt",
        {"owner": owner, "lease": lease_sec},
    )).fetchone()
    if row is not None:
        await conn.execute("UPDATE kb_files SET status='processing', error=NULL, updated_at=now() WHERE id=%s",
                           (row["file_id"],))
    return row


async def heartbeat_job(conn: AsyncConnection, *, job_id: str, owner: str, lease_sec: int) -> bool:
    row = await (await conn.execute(
        "UPDATE ingest_jobs SET lease_until=now() + make_interval(secs => %s)"
        " WHERE id=%s AND lease_owner=%s RETURNING id", (lease_sec, job_id, owner),
    )).fetchone()
    return row is not None


async def release_for_retry(conn: AsyncConnection, *, job_id: str, owner: str, delay_sec: float) -> None:
    await conn.execute(
        "UPDATE ingest_jobs SET lease_owner=NULL, lease_until=NULL,"
        "   next_attempt_at=now() + make_interval(secs => %s)"
        " WHERE id=%s AND lease_owner=%s", (delay_sec, job_id, owner),
    )
    await conn.execute(
        "UPDATE kb_files SET status='pending', updated_at=now()"
        " WHERE id=(SELECT file_id FROM ingest_jobs WHERE id=%s)", (job_id,),
    )


async def finish_job(conn: AsyncConnection, *, job_id: str, owner: str, file_id: str,
                     error: str | None) -> bool:
    """Close the job: the file becomes ready, or failed with `error`. False means this owner no longer
    held the lease (or the file is gone) and nothing was written."""
    owned = await (await conn.execute(
        "DELETE FROM ingest_jobs WHERE id=%s AND lease_owner=%s RETURNING id", (job_id, owner),
    )).fetchone()
    if owned is None:
        return False
    row = await (await conn.execute(
        "UPDATE kb_files SET status=%s, error=%s, updated_at=now() WHERE id=%s RETURNING id",
        ("failed" if error else "ready", error, file_id),
    )).fetchone()
    return row is not None


async def replace_chunks(conn: AsyncConnection, *, file_id: str, kb_id: str,
                         chunks: list[tuple[str | None, str, list[float]]]) -> bool:
    """Delete this file's chunks and insert `chunks` (heading, text, embedding), so a re-run never
    duplicates. False when the file no longer exists: the caller stops quietly."""
    async with conn.transaction():
        exists = await (await conn.execute("SELECT 1 FROM kb_files WHERE id=%s FOR UPDATE", (file_id,))).fetchone()
        if exists is None:
            return False
        await conn.execute("DELETE FROM kb_chunks WHERE file_id=%s", (file_id,))
        async with conn.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO kb_chunks (kb_id, file_id, ordinal, heading, text, embedding)"
                " VALUES (%s, %s, %s, %s, %s, %s::vector)",
                [(kb_id, file_id, i, heading, text, vector_literal(vec))
                 for i, (heading, text, vec) in enumerate(chunks)],
            )
    return True


# ------------------------------------------------------------------ search

async def search(conn: AsyncConnection, *, kb_id: str, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
    """Top-k chunks by cosine similarity. Text and metadata only: the vector stays in this table."""
    rows = await (await conn.execute(
        "SELECT c.text, c.heading, f.filename AS file, 1 - (c.embedding <=> %(q)s::vector) AS score"
        " FROM kb_chunks c JOIN kb_files f ON f.id = c.file_id"
        " WHERE c.kb_id=%(kb)s ORDER BY c.embedding <=> %(q)s::vector LIMIT %(k)s",
        {"q": vector_literal(embedding), "kb": kb_id, "k": top_k},
    )).fetchall()
    return [{"text": r["text"], "score": float(r["score"]), "heading": r["heading"], "file": r["file"]} for r in rows]


class KnowledgeBases(Protocol):
    """What the kb_search node needs: the model a knowledge base was embedded with, and a search."""

    async def get(self, kb_id: str) -> dict[str, Any] | None: ...

    async def search(self, kb_id: str, embedding: list[float], top_k: int) -> list[dict[str, Any]]: ...


class PostgresKnowledgeBases:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def get(self, kb_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            return await get_kb(conn, kb_id)

    async def search(self, kb_id: str, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            return await search(conn, kb_id=kb_id, embedding=embedding, top_k=top_k)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kb_store.py -q`  Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add services/engine/engine/kb/store.py services/engine/tests/test_kb_store.py
git commit -m "feat(engine): knowledge base store with ingest lease and cosine search" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: ingester 프로세스

**Files:**
- Create: `services/engine/engine/ingest/__init__.py`(빈 파일), `engine/ingest/worker.py`, `engine/ingest/main.py`
- Test: `services/engine/tests/test_ingest_worker.py`

- [ ] **Step 1: 실패하는 테스트**

`tests/test_ingest_worker.py`:
```python
"""The ingester: claim a job, parse → chunk → embed → store, and the ways that goes wrong (§4)."""
import asyncio

import pytest_asyncio

from engine.ingest.worker import Ingester
from engine.kb import IngestError, store
from engine.llm.scripted import ScriptedLLM
from tests.conftest import until
from tests.helpers import make_config

MD = "# 안내\n\n첫 문단.\n\n## 세부\n\n둘째 문단.\n".encode("utf-8")


class FakeParser:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls = 0
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()

    async def to_markdown(self, filename, media_type, content):
        self.calls += 1
        self.started.set()
        await self.proceed.wait()
        response = self.responses.pop(0) if self.responses else "# 파싱됨\n\n본문"
        if isinstance(response, Exception):
            raise response
        return response


@pytest_asyncio.fixture
async def ingester_factory(pool):
    started = []

    async def make(*, parser=None, llm=None, owner="ing-1", retry_delays=(0.1, 0.1), **overrides):
        config = make_config(claim_poll_sec=0.1, heartbeat_sec=0.2, lease_sec=2, **overrides)
        worker = Ingester(config, pool, llm=llm or ScriptedLLM([]), parser=parser, owner=owner,
                          retry_delays=retry_delays)
        await worker.start()
        started.append(worker)
        return worker

    yield make
    for worker in started:
        await worker.stop()


async def _upload(pool, content=MD, filename="a.md", media_type="text/markdown") -> tuple[str, str]:
    async with pool.connection() as conn, conn.transaction():
        kb = await store.create_kb(conn, name=f"kb-{filename}", embed_model="bge-m3", dim=1024)
        row = await store.add_file(conn, kb_id=str(kb["id"]), filename=filename, media_type=media_type, content=content)
    return str(kb["id"]), str(row["id"])


async def _file(pool, file_id):
    async with pool.connection() as conn:
        return await store.get_file(conn, file_id)


async def _status_is(pool, file_id, status):
    row = await _file(pool, file_id)
    return row if row is not None and row["status"] == status else None


async def _chunks(pool, file_id):
    async with pool.connection() as conn:
        return await (await conn.execute(
            "SELECT heading, text FROM kb_chunks WHERE file_id=%s ORDER BY ordinal", (file_id,))).fetchall()


async def test_a_markdown_file_is_chunked_embedded_and_marked_ready_without_the_parser(pool, ingester_factory):
    kb_id, file_id = await _upload(pool)
    parser = FakeParser()
    llm = ScriptedLLM([])
    await ingester_factory(parser=parser, llm=llm)

    await until(lambda: _status_is(pool, file_id, "ready"))

    assert parser.calls == 0
    assert [c["text"] for c in await _chunks(pool, file_id)] == ["첫 문단.", "둘째 문단."]
    assert llm.embed_calls[0][0] == "bge-m3" and llm.embed_calls[0][1] == ["안내\n\n첫 문단.", "세부\n\n둘째 문단."]


async def test_other_files_go_through_the_parser(pool, ingester_factory):
    _, file_id = await _upload(pool, b"%PDF", "a.pdf", "application/pdf")
    parser = FakeParser(["# 파싱됨\n\n본문"])
    await ingester_factory(parser=parser)

    await until(lambda: _status_is(pool, file_id, "ready"))
    assert parser.calls == 1
    assert [c["text"] for c in await _chunks(pool, file_id)] == ["본문"]


async def test_a_retryable_failure_is_retried_and_then_succeeds(pool, ingester_factory):
    _, file_id = await _upload(pool, b"%PDF", "a.pdf", "application/pdf")
    parser = FakeParser([IngestError("MinerU 오류 HTTP 503", retryable=True), "# ok\n\n본문"])
    await ingester_factory(parser=parser)

    await until(lambda: _status_is(pool, file_id, "ready"))
    assert parser.calls == 2


async def test_three_retryable_failures_mark_the_file_failed_with_the_reason(pool, ingester_factory):
    _, file_id = await _upload(pool, b"%PDF", "a.pdf", "application/pdf")
    parser = FakeParser([IngestError("MinerU 오류 HTTP 503", retryable=True)] * 3)
    await ingester_factory(parser=parser)

    row = await until(lambda: _status_is(pool, file_id, "failed"))
    assert parser.calls == 3 and row["error"] == "MinerU 오류 HTTP 503"
    async with pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) AS n FROM ingest_jobs")).fetchone())["n"] == 0


async def test_a_non_retryable_failure_fails_at_once(pool, ingester_factory):
    _, file_id = await _upload(pool, b"?", "a.xyz", "application/octet-stream")
    parser = FakeParser([IngestError("문서를 읽지 못했습니다 (MinerU 422)", retryable=False)])
    await ingester_factory(parser=parser)

    row = await until(lambda: _status_is(pool, file_id, "failed"))
    assert parser.calls == 1 and "422" in row["error"]


async def test_a_text_file_that_is_not_utf8_fails_at_once(pool, ingester_factory):
    _, file_id = await _upload(pool, b"\xff\xfe\x00", "a.txt", "text/plain")
    await ingester_factory(parser=FakeParser())

    row = await until(lambda: _status_is(pool, file_id, "failed"))
    assert "UTF-8" in row["error"]


async def test_a_file_with_no_text_fails_at_once(pool, ingester_factory):
    _, file_id = await _upload(pool, b"   \n", "empty.md")
    await ingester_factory(parser=FakeParser())

    row = await until(lambda: _status_is(pool, file_id, "failed"))
    assert row["error"] == "문서에서 텍스트를 찾지 못했습니다"


async def test_a_wrong_embedding_dimension_fails_at_once(pool, ingester_factory):
    _, file_id = await _upload(pool)

    class ShortLLM(ScriptedLLM):
        EMBED_DIM = 8

    await ingester_factory(llm=ShortLLM([]))
    row = await until(lambda: _status_is(pool, file_id, "failed"))
    assert "1024" in row["error"]


async def test_a_file_deleted_while_processing_is_dropped_quietly(pool, ingester_factory):
    _, file_id = await _upload(pool, b"%PDF", "a.pdf", "application/pdf")
    parser = FakeParser()
    parser.proceed.clear()
    await ingester_factory(parser=parser)
    await asyncio.wait_for(parser.started.wait(), 10)

    async with pool.connection() as conn:
        await store.delete_file(conn, file_id)
    parser.proceed.set()

    await asyncio.sleep(0.5)
    assert await _file(pool, file_id) is None
    async with pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) AS n FROM kb_chunks")).fetchone())["n"] == 0


async def test_a_job_whose_owner_died_is_picked_up_by_the_next_ingester(pool, ingester_factory):
    _, file_id = await _upload(pool)
    async with pool.connection() as conn:
        job = await store.claim_job(conn, owner="dead", lease_sec=30)
        await conn.execute("UPDATE ingest_jobs SET lease_until = now() - interval '1 second' WHERE id=%s", (job["id"],))

    await ingester_factory(owner="alive")
    await until(lambda: _status_is(pool, file_id, "ready"))
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_ingest_worker.py -q`  Expected: ImportError.

- [ ] **Step 3: 구현**

`engine/ingest/__init__.py`: 빈 파일.

`engine/ingest/worker.py`:
```python
"""The ingester: one process that turns uploaded files into chunk vectors (knowledge-base design §4).

Same shape as engine/worker/worker.py -- claim under a lease, heartbeat while working, hand back on
failure -- but for `ingest_jobs`, and with nothing to checkpoint: a job either finishes or runs again
from the start.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from engine.config import EngineConfig
from engine.errors import NodeError
from engine.kb import IngestError, store
from engine.kb.chunk import chunk_markdown
from engine.llm.base import LLMClient

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAYS_SEC: tuple[float, ...] = (30.0, 120.0)  # after attempt 1, after attempt 2; attempt 3 is the last
EMBED_BATCH = 32
STOP_TIMEOUT_SEC = 10.0


class Parser(Protocol):
    async def to_markdown(self, filename: str, media_type: str, content: bytes) -> str: ...


class Ingester:
    def __init__(self, config: EngineConfig, pool: AsyncConnectionPool, *, llm: LLMClient,
                 parser: Parser | None, owner: str | None = None,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS_SEC) -> None:
        self._config = config
        self._pool = pool
        self._llm = llm
        self._parser = parser
        self._retry_delays = retry_delays
        self.owner = owner or f"ingester-{uuid.uuid4()}"
        self._tasks: set[asyncio.Task] = set()
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._spawn(self._claim_loop())

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=STOP_TIMEOUT_SEC)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _claim_loop(self) -> None:
        in_flight: set[asyncio.Task] = set()
        while self._running:
            try:
                claimed = False
                while self._running and len(in_flight) < self._config.ingest_max_jobs:
                    async with self._pool.connection() as conn:
                        job = await store.claim_job(conn, owner=self.owner, lease_sec=self._config.lease_sec)
                    if job is None:
                        break
                    claimed = True
                    task = self._spawn(self._process(job))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
                if not claimed:
                    await asyncio.sleep(self._config.claim_poll_sec)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ingest claim loop failed; retrying after the poll interval")
                await asyncio.sleep(self._config.claim_poll_sec)

    async def _heartbeat(self, job_id: str, task: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(self._config.heartbeat_sec)
            try:
                async with self._pool.connection() as conn:
                    owned = await store.heartbeat_job(conn, job_id=job_id, owner=self.owner,
                                                      lease_sec=self._config.lease_sec)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("ingest heartbeat failed for job %s; retrying", job_id, exc_info=True)
                continue
            if not owned:  # the lease is gone: someone else owns the job, write nothing more
                task.cancel()
                return

    async def _process(self, job: dict[str, Any]) -> None:
        job_id, file_id, attempt = str(job["id"]), str(job["file_id"]), int(job["attempt"])
        beat = self._spawn(self._heartbeat(job_id, asyncio.current_task()))
        try:
            async with self._pool.connection() as conn:
                row = await store.get_file(conn, file_id)
            if row is None:
                return  # deleted between claim and read; the job went with it
            try:
                chunks = await self._ingest(row)
            except (IngestError, NodeError) as exc:
                if exc.retryable and attempt < MAX_ATTEMPTS:
                    delay = self._retry_delays[min(attempt, len(self._retry_delays)) - 1]
                    log.info("ingest of %s failed (attempt %s/%s), retrying in %ss: %s",
                             file_id, attempt, MAX_ATTEMPTS, delay, exc.message)
                    async with self._pool.connection() as conn:
                        await store.release_for_retry(conn, job_id=job_id, owner=self.owner, delay_sec=delay)
                    return
                async with self._pool.connection() as conn:
                    await store.finish_job(conn, job_id=job_id, owner=self.owner, file_id=file_id, error=exc.message)
                return
            async with self._pool.connection() as conn:
                if not await store.replace_chunks(conn, file_id=file_id, kb_id=str(row["kb_id"]), chunks=chunks):
                    return  # the file was deleted while we worked; its job is gone too
                await store.finish_job(conn, job_id=job_id, owner=self.owner, file_id=file_id, error=None)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Infrastructure, not the document: leave the lease to expire so the job is retried.
            log.exception("ingest of %s aborted", file_id)
        finally:
            beat.cancel()

    async def _ingest(self, row: dict[str, Any]) -> list[tuple[str | None, str, list[float]]]:
        filename, media_type, content = row["filename"], row["media_type"], bytes(row["content"])
        if store.is_text_file(filename, media_type):
            try:
                markdown = content.decode("utf-8")
            except UnicodeDecodeError:
                raise IngestError("텍스트 파일이 UTF-8이 아닙니다", retryable=False) from None
        else:
            if self._parser is None:
                raise IngestError("MinerU가 설정되지 않아 이 형식은 처리할 수 없습니다", retryable=False)
            markdown = await self._parser.to_markdown(filename, media_type, content)
        chunks = chunk_markdown(markdown)
        if not chunks:
            raise IngestError("문서에서 텍스트를 찾지 못했습니다", retryable=False)
        async with self._pool.connection() as conn:
            kb = await store.get_kb(conn, str(row["kb_id"]))
        if kb is None:
            raise IngestError("지식베이스를 찾을 수 없습니다", retryable=False)
        # The heading is part of what gets embedded -- "세부" alone says little, "안내 > 세부 ..." finds
        # the section -- but it is stored separately so the hit can show it.
        texts = [f"{c.heading}\n\n{c.text}" if c.heading else c.text for c in chunks]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            vectors += await self._llm.embed(model=kb["embed_model"], texts=texts[start:start + EMBED_BATCH])
        if any(len(v) != kb["dim"] for v in vectors):
            raise IngestError(f"임베딩 차원이 지식베이스({kb['dim']})와 다릅니다", retryable=False)
        return [(c.heading, c.text, v) for c, v in zip(chunks, vectors)]
```

`engine/ingest/main.py`:
```python
"""`python -m engine.ingest.main`: one ingester process."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from typing import Any

from engine.config import load_config
from engine.db.migrate import prepare_database
from engine.db.pool import make_pool
from engine.ingest.worker import Ingester
from engine.kb.mineru import MineruParser
from engine.llm.gateway import LLMGateway
from engine.llm.ollama import OllamaRaw

log = logging.getLogger(__name__)


async def run(stop: asyncio.Event | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config()
    if stop is None:
        stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(AttributeError, NotImplementedError):
            loop.add_signal_handler(getattr(signal, name), stop.set)

    await prepare_database(config.database_url)
    pool = make_pool(config.database_url, max_size=max(4, config.ingest_max_jobs + 3))
    raw: OllamaRaw | None = None
    parser: MineruParser | None = None
    worker: Ingester | None = None
    try:
        await pool.open(wait=True)
        raw = OllamaRaw(config.ollama_base_url)
        if config.mineru_base_url:
            parser = MineruParser(config.mineru_base_url, timeout=config.mineru_timeout_sec)
        else:
            log.warning("MINERU_BASE_URL is not set: only .md/.txt files can be ingested")
        worker = Ingester(config, pool, llm=LLMGateway(raw), parser=parser)
        await worker.start()
        log.info("ingester %s ready", worker.owner)
        await stop.wait()
    finally:
        shutdowns: list[tuple[str, Any]] = [
            ("ingester", worker.stop if worker is not None else None),
            ("mineru client", parser.aclose if parser is not None else None),
            ("ollama client", raw.aclose if raw is not None else None),
            ("db pool", pool.close),
        ]
        for name, shutdown in shutdowns:
            if shutdown is None:
                continue
            try:
                await shutdown()
            except Exception:
                log.warning("ingester shutdown: closing the %s failed", name, exc_info=True)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_ingest_worker.py -q`  Expected: PASS (약 10~20초).

- [ ] **Step 5: 커밋**

```bash
git add services/engine/engine/ingest services/engine/tests/test_ingest_worker.py
git commit -m "feat(engine): ingester process — parse, chunk, embed, store under a lease" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `kb_search` 노드와 포트 배선

**Files:**
- Create: `services/engine/engine/nodes/kb_search.py`
- Modify: `engine/nodes/base.py:46-60`, `engine/nodes/registry.py`, `engine/runtime/deps.py`, `engine/compiler/wrapper.py:_context`, `engine/worker/worker.py:__init__`, `engine/worker/main.py`, `tests/helpers.py:make_ctx`, `tests/conftest.py:worker_factory`
- Test: `services/engine/tests/test_nodes_kb_search.py`

- [ ] **Step 1: 실패하는 테스트**

`tests/test_nodes_kb_search.py`:
```python
"""The kb_search node (knowledge-base design §5.1): embed the question, search, return text only."""
import pytest

from engine.errors import ErrorCode, NodeError
from engine.kb import store
from engine.kb.store import PostgresKnowledgeBases
from engine.llm.scripted import ScriptedLLM
from engine.nodes.kb_search import KbSearchNode
from tests.helpers import make_ctx


def _one_hot(text: str) -> list[float]:
    vector = [0.0] * ScriptedLLM.EMBED_DIM
    vector[sum(map(ord, text)) % ScriptedLLM.EMBED_DIM] = 1.0
    return vector


async def _seed(pool, texts: list[str]) -> str:
    async with pool.connection() as conn, conn.transaction():
        kb = await store.create_kb(conn, name="kb", embed_model="bge-m3", dim=1024)
        row = await store.add_file(conn, kb_id=str(kb["id"]), filename="doc.md", media_type="text/markdown", content=b"x")
        await store.replace_chunks(conn, file_id=str(row["id"]), kb_id=str(kb["id"]),
                                   chunks=[("절", t, _one_hot(t)) for t in texts])
    return str(kb["id"])


def _config(kb_id: str, **overrides):
    raw = {"knowledgeBase": kb_id, "query": "{{ start.q }}", **overrides}
    return KbSearchNode().parse_config(raw)


async def test_returns_hits_in_score_order_and_a_joined_context(pool):
    kb_id = await _seed(pool, ["정답 문장", "다른 문장"])
    llm = ScriptedLLM([])
    ctx = make_ctx(llm=llm, kb=PostgresKnowledgeBases(pool))

    result = await KbSearchNode().execute(ctx, _config(kb_id, topK=2), {"query": "정답 문장"})

    assert [h["text"] for h in result.output["hits"]] == ["정답 문장", "다른 문장"]
    assert result.output["hits"][0] == {"text": "정답 문장", "score": pytest.approx(1.0), "heading": "절", "file": "doc.md"}
    assert result.output["context"] == "정답 문장\n\n---\n\n다른 문장"
    assert llm.embed_calls == [("bge-m3", ["정답 문장"])]


async def test_min_score_drops_weak_hits(pool):
    kb_id = await _seed(pool, ["정답 문장", "다른 문장"])
    ctx = make_ctx(llm=ScriptedLLM([]), kb=PostgresKnowledgeBases(pool))

    result = await KbSearchNode().execute(ctx, _config(kb_id, minScore=0.5), {"query": "정답 문장"})

    assert [h["text"] for h in result.output["hits"]] == ["정답 문장"]


async def test_an_empty_knowledge_base_is_an_empty_result_not_an_error(pool):
    kb_id = await _seed(pool, [])
    ctx = make_ctx(llm=ScriptedLLM([]), kb=PostgresKnowledgeBases(pool))

    result = await KbSearchNode().execute(ctx, _config(kb_id), {"query": "무엇"})

    assert result.output == {"hits": [], "context": ""}


async def test_an_unknown_knowledge_base_fails_without_retry(pool):
    ctx = make_ctx(llm=ScriptedLLM([]), kb=PostgresKnowledgeBases(pool))

    with pytest.raises(NodeError) as caught:
        await KbSearchNode().execute(ctx, _config("00000000-0000-0000-0000-00000000dead"), {"query": "무엇"})
    assert caught.value.code == ErrorCode.NODE_FAILED and not caught.value.retryable
    assert "지식베이스를 찾을 수 없습니다" in caught.value.message


async def test_an_empty_query_is_a_template_error():
    with pytest.raises(NodeError) as caught:
        await KbSearchNode().execute(make_ctx(), _config("x"), {"query": "   "})
    assert caught.value.code == ErrorCode.TEMPLATE_ERROR


def test_the_config_schema_marks_the_knowledge_base_field_for_the_editor():
    schema = KbSearchNode.Config.model_json_schema()
    assert schema["properties"]["knowledgeBase"]["x-knowledge-base"] is True
    assert schema["properties"]["query"]["x-template"] is True


def test_the_output_schema_describes_hits_and_context():
    schema = KbSearchNode().output_schema(_config("x"), {})
    assert set(schema["properties"]) == {"hits", "context"}
    assert schema["properties"]["hits"]["items"]["required"] == ["text", "score", "heading", "file"]
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_nodes_kb_search.py -q`  Expected: ImportError.

- [ ] **Step 3: 포트 추가**

`engine/nodes/base.py` — `SecretResolver` 아래에 Protocol 두 개 추가:
```python
class KnowledgeBases(Protocol):
    async def get(self, kb_id: str) -> dict[str, Any] | None: ...

    async def search(self, kb_id: str, embedding: list[float], top_k: int) -> list[dict[str, Any]]: ...


class Reranker(Protocol):
    async def rerank(self, query: str, texts: list[str]) -> list[tuple[int, float]]:
        """(index into texts, score) pairs, best first."""
        ...
```
`NodeContext`의 `timeout_sec: float | None = None` 아래에 추가:
```python
    kb: KnowledgeBases | None = None
    rerank: Reranker | None = None
```
`engine/runtime/deps.py` — `TYPE_CHECKING` import에 `KnowledgeBases, Reranker` 추가하고 `RunDeps` 끝에:
```python
    kb: "KnowledgeBases | None" = None
    rerank: "Reranker | None" = None
```
`engine/compiler/wrapper.py` `_context()`의 `NodeContext(` 호출에 `timeout_sec=timeout,` 뒤 추가:
```python
        kb=deps.kb,
        rerank=deps.rerank,
```
`engine/worker/worker.py` — `__init__` 시그니처 `secrets: Any = None` 뒤에 `, kb: Any = None, rerank: Any = None` 추가, 본문에 `self._kb = kb`, `self._rerank = rerank` 추가, `_run()`의 `RunDeps(` 호출에 `kb=self._kb, rerank=self._rerank` 추가.

`engine/worker/main.py` — import에 `from engine.kb.store import PostgresKnowledgeBases` 추가(리랭커 배선은 Task 9). `secrets = ...` 줄 다음에:
```python
        kb = PostgresKnowledgeBases(pool)
```
`Worker(` 호출에 `kb=kb` 추가. (`rerank=`는 Task 9에서 붙인다.)

`tests/helpers.py` `make_ctx` 시그니처에 `kb=None, rerank=None` 추가, `NodeContext(` 호출에 `kb=kb, rerank=rerank` 추가.
`tests/conftest.py` `worker_factory.make` 시그니처에 `kb=None, rerank=None` 추가, `Worker(` 호출에 `kb=kb, rerank=rerank` 추가.

- [ ] **Step 4: 노드**

`engine/nodes/kb_search.py`:
```python
"""The kb_search node (knowledge-base design §5.1).

Embeds the question and searches inside the node, so the vector never enters run state: the output is
the matching text, a score, and where it came from.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from engine.dsl.models import Policy, RetrySpec
from engine.errors import EngineFault, ErrorCode, NodeError
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField, Usage

KNOWLEDGE_BASE = {"x-knowledge-base": True}  # the editor renders a knowledge base picker
SEPARATOR = "\n\n---\n\n"
HIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "score": {"type": "number"},
        "heading": {},  # string or null; `{}` like http_request's body, since the validator reads single types
        "file": {"type": "string"},
    },
    "required": ["text", "score", "heading", "file"],
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"hits": {"type": "array", "items": HIT_SCHEMA}, "context": {"type": "string"}},
    "required": ["hits", "context"],
}


def join_context(hits: list[dict[str, Any]]) -> str:
    """One string a prompt can take whole: `{{ kb_search_1.context }}`."""
    return SEPARATOR.join(hit["text"] for hit in hits)


class KbSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledgeBase: str = Field(min_length=1, max_length=64, json_schema_extra=KNOWLEDGE_BASE)
    query: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    topK: int = Field(5, ge=1, le=20, strict=True)
    # 0 means no filter. A plain float rather than `float | None`: pydantic renders Optional as an
    # `anyOf`, and RJSF draws its own branch selector for those (see collapseOptionalSchemas).
    minScore: float = Field(0.0, ge=0, le=1)


class KbSearchNode(NodeSpec):
    type = "kb_search"
    label = "지식 검색"
    category = "AI"
    Config = KbSearchConfig
    default_policy = Policy(timeoutSec=60, retry=RetrySpec(maxAttempts=3))

    def template_fields(self, config: KbSearchConfig) -> list[TemplateField]:
        return [TemplateField("query", config.query, "string")]

    def output_schema(self, config: KbSearchConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return OUTPUT_SCHEMA

    async def execute(self, ctx: NodeContext, config: KbSearchConfig, rendered: dict[str, Any]) -> NodeResult:
        query = rendered["query"]
        if not query.strip():
            raise NodeError(ErrorCode.TEMPLATE_ERROR, "검색할 질문이 비어 있습니다", retryable=False)
        if ctx.kb is None:
            raise EngineFault("kb_search needs a knowledge base store")
        kb = await ctx.kb.get(config.knowledgeBase)
        if kb is None:
            raise NodeError(ErrorCode.NODE_FAILED, "지식베이스를 찾을 수 없습니다", retryable=False)
        [embedding] = await ctx.llm.embed(model=kb["embed_model"], texts=[query])
        hits = await ctx.kb.search(config.knowledgeBase, embedding, config.topK)
        hits = [hit for hit in hits if hit["score"] >= config.minScore]
        return NodeResult({"hits": hits, "context": join_context(hits)}, Usage())
```
`engine/nodes/registry.py` — `from engine.nodes.kb_search import KbSearchNode` 추가, 목록 끝에 `KbSearchNode(),` 추가.

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_nodes_kb_search.py tests/test_nodes_basic.py tests/test_compiler_wrapper.py tests/test_api_basics.py -q`  Expected: PASS. `test_api_basics`에 노드 타입 목록을 고정한 assert가 있으면 `kb_search`를 추가한다.

- [ ] **Step 6: 커밋**

```bash
git add services/engine/engine services/engine/tests
git commit -m "feat(engine): kb_search node with the knowledge base port wired through the worker" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: TEI 리랭커와 `rerank` 노드

**Files:**
- Create: `services/engine/engine/kb/rerank.py`, `engine/nodes/rerank.py`
- Modify: `engine/nodes/registry.py`, `engine/worker/main.py`
- Test: `services/engine/tests/test_kb_rerank.py`, `tests/test_nodes_rerank.py`

- [ ] **Step 1: 실패하는 테스트**

`tests/test_kb_rerank.py`:
```python
import json

import httpx
import pytest

from engine.errors import ErrorCode, NodeError
from engine.kb.rerank import TeiReranker


def _reranker(handler) -> TeiReranker:
    return TeiReranker("http://tei:80/", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_posts_query_and_texts_and_returns_index_score_pairs():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, json=[{"index": 1, "score": 0.9}, {"index": 0, "score": 0.2}])

    pairs = await _reranker(handler).rerank("질문", ["a", "b"])

    assert seen["url"] == "http://tei:80/rerank"
    assert json.loads(seen["body"]) == {"query": "질문", "texts": ["a", "b"]}
    assert pairs == [(1, 0.9), (0, 0.2)]


async def test_a_malformed_response_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"oops": 1})

    with pytest.raises(NodeError) as caught:
        await _reranker(handler).rerank("q", ["a"])
    assert caught.value.code == ErrorCode.LLM_UNAVAILABLE and caught.value.retryable


@pytest.mark.parametrize("status,retryable", [(503, True), (422, False)])
async def test_status_codes_decide_retryability(status, retryable):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="x")

    with pytest.raises(NodeError) as caught:
        await _reranker(handler).rerank("q", ["a"])
    assert caught.value.retryable is retryable
```
`tests/test_nodes_rerank.py`:
```python
import pytest

from engine.errors import ErrorCode, NodeError
from engine.nodes.rerank import RerankNode
from tests.helpers import make_ctx

HITS = [
    {"text": "하나", "score": 0.7, "heading": None, "file": "a.md"},
    {"text": "둘", "score": 0.6, "heading": "h", "file": "a.md"},
    {"text": "셋", "score": 0.5, "heading": None, "file": "b.md"},
]


class FakeReranker:
    def __init__(self, pairs) -> None:
        self.pairs = pairs
        self.calls = []

    async def rerank(self, query, texts):
        self.calls.append((query, list(texts)))
        return self.pairs


def _config(**overrides):
    return RerankNode().parse_config({"query": "{{ start.q }}", "hits": "{{ kb_search_1.hits }}", **overrides})


async def test_reorders_by_rerank_score_and_keeps_top_n():
    reranker = FakeReranker([(2, 0.95), (0, 0.4), (1, 0.1)])
    result = await RerankNode().execute(make_ctx(rerank=reranker), _config(topN=2), {"query": "질문", "hits": HITS})

    assert reranker.calls == [("질문", ["하나", "둘", "셋"])]
    assert [(h["text"], h["score"]) for h in result.output["hits"]] == [("셋", 0.95), ("하나", 0.4)]
    assert result.output["hits"][0]["file"] == "b.md"
    assert result.output["context"] == "셋\n\n---\n\n하나"


async def test_min_score_filters_after_reranking():
    reranker = FakeReranker([(0, 0.9), (1, 0.2)])
    result = await RerankNode().execute(make_ctx(rerank=reranker), _config(minScore=0.5), {"query": "q", "hits": HITS[:2]})
    assert [h["text"] for h in result.output["hits"]] == ["하나"]


async def test_empty_hits_skip_the_reranker():
    reranker = FakeReranker([])
    result = await RerankNode().execute(make_ctx(rerank=reranker), _config(), {"query": "q", "hits": []})
    assert result.output == {"hits": [], "context": ""} and reranker.calls == []


@pytest.mark.parametrize("hits", ["문자열", [{"nope": 1}], [{"text": 3}], [{"text": "x"}] * 101])
async def test_malformed_hits_are_a_template_error(hits):
    with pytest.raises(NodeError) as caught:
        await RerankNode().execute(make_ctx(rerank=FakeReranker([])), _config(), {"query": "q", "hits": hits})
    assert caught.value.code == ErrorCode.TEMPLATE_ERROR and not caught.value.retryable


async def test_a_missing_reranker_is_a_configuration_error():
    with pytest.raises(NodeError) as caught:
        await RerankNode().execute(make_ctx(rerank=None), _config(), {"query": "q", "hits": HITS})
    assert not caught.value.retryable and "리랭커가 설정되지 않았습니다" in caught.value.message


def test_the_hits_field_is_an_array_template():
    fields = RerankNode().template_fields(_config())
    assert [(f.path, f.target) for f in fields] == [("query", "string"), ("hits", "array")]
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_kb_rerank.py tests/test_nodes_rerank.py -q`  Expected: ImportError.

- [ ] **Step 3: 구현**

`engine/kb/rerank.py`:
```python
"""text-embeddings-inference `/rerank` client (knowledge-base design §2, §5.2). The model is whatever
the TEI server was started with; the node does not choose one."""
from __future__ import annotations

import httpx

from engine.errors import ErrorCode, NodeError

CONNECT_TIMEOUT = 10.0


class TeiReranker:
    def __init__(self, base_url: str, *, timeout: float = 60.0, client: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def rerank(self, query: str, texts: list[str]) -> list[tuple[int, float]]:
        try:
            response = await self._client.post(f"{self._base}/rerank", json={"query": query, "texts": texts})
        except httpx.RequestError as exc:
            raise NodeError(ErrorCode.LLM_UNAVAILABLE, f"리랭커 연결 실패: {exc}", retryable=True) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise NodeError(ErrorCode.LLM_UNAVAILABLE, f"리랭커 오류 HTTP {response.status_code}", retryable=True)
        if response.status_code >= 400:
            raise NodeError(ErrorCode.NODE_FAILED, f"리랭커 요청 오류 HTTP {response.status_code}: {response.text[:200]}",
                            retryable=False)
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, list) or not all(
            isinstance(item, dict) and isinstance(item.get("index"), int) and not isinstance(item.get("index"), bool)
            and isinstance(item.get("score"), (int, float)) and 0 <= item["index"] < len(texts)
            for item in data
        ):
            raise NodeError(ErrorCode.LLM_UNAVAILABLE, "리랭커 응답 형식이 올바르지 않습니다", retryable=True)
        return [(item["index"], float(item["score"])) for item in data]
```
`engine/nodes/rerank.py`:
```python
"""The rerank node (knowledge-base design §5.2): reorder search hits with a cross-encoder."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from engine.dsl.models import Policy, RetrySpec
from engine.errors import ErrorCode, NodeError
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField, Usage
from engine.nodes.kb_search import OUTPUT_SCHEMA, join_context

MAX_HITS = 100


class RerankConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    hits: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    topN: int = Field(3, ge=1, le=20, strict=True)
    minScore: float = Field(0.0, ge=0, le=1)  # 0 means no filter; see KbSearchConfig for why not Optional


class RerankNode(NodeSpec):
    type = "rerank"
    label = "리랭킹"
    category = "AI"
    Config = RerankConfig
    default_policy = Policy(timeoutSec=60, retry=RetrySpec(maxAttempts=3))

    def template_fields(self, config: RerankConfig) -> list[TemplateField]:
        return [TemplateField("query", config.query, "string"), TemplateField("hits", config.hits, "array")]

    def output_schema(self, config: RerankConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return OUTPUT_SCHEMA  # same shape as kb_search, so a prompt can take either

    async def execute(self, ctx: NodeContext, config: RerankConfig, rendered: dict[str, Any]) -> NodeResult:
        hits = _check_hits(rendered["hits"])
        if not hits:
            return NodeResult({"hits": [], "context": ""}, Usage())
        if ctx.rerank is None:
            raise NodeError(ErrorCode.NODE_FAILED, "리랭커가 설정되지 않았습니다 (RERANK_BASE_URL)", retryable=False)
        pairs = await ctx.rerank.rerank(rendered["query"], [hit["text"] for hit in hits])
        reranked = [{**hits[index], "score": score} for index, score in pairs if score >= config.minScore]
        reranked = reranked[: config.topN]
        return NodeResult({"hits": reranked, "context": join_context(reranked)}, Usage())


def _check_hits(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_HITS or not all(
        isinstance(hit, dict) and isinstance(hit.get("text"), str) for hit in value
    ):
        raise NodeError(ErrorCode.TEMPLATE_ERROR,
                        f"hits는 text를 가진 항목의 목록이어야 합니다 (최대 {MAX_HITS}개). 보통 {{{{ 지식검색노드.hits }}}}를 넣습니다",
                        retryable=False)
    return value
```
`engine/nodes/registry.py` — `from engine.nodes.rerank import RerankNode` 추가, 목록 끝에 `RerankNode(),`.
`engine/worker/main.py` — `from engine.kb.rerank import TeiReranker` 추가; `kb = PostgresKnowledgeBases(pool)` 아래에:
```python
        rerank = TeiReranker(config.rerank_base_url) if config.rerank_base_url else None
        if rerank is None:
            log.warning("RERANK_BASE_URL is not set: rerank nodes will fail")
```
`Worker(` 호출에 `rerank=rerank` 추가; shutdown 목록에 `("rerank client", rerank.aclose if rerank is not None else None),` 추가(변수 선언 `rerank: TeiReranker | None = None`을 try 위에 둔다).

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_kb_rerank.py tests/test_nodes_rerank.py tests/test_worker_main.py tests/test_api_basics.py -q`  Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add services/engine/engine services/engine/tests
git commit -m "feat(engine): rerank node backed by a TEI reranker" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: 지식베이스 API

**Files:**
- Create: `services/engine/engine/api/routers/knowledge_bases.py`
- Modify: `engine/api/app.py`
- Test: `services/engine/tests/test_api_knowledge_bases.py`

- [ ] **Step 1: 실패하는 테스트**

`tests/test_api_knowledge_bases.py`:
```python
"""The knowledge base API (knowledge-base design §6)."""
import dataclasses

from httpx import ASGITransport, AsyncClient

from engine.api.app import create_app


async def _kb(api, name="문서") -> str:
    created = await api.post("/knowledge-bases", json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_create_list_and_delete(api):
    kb_id = await _kb(api)
    listed = (await api.get("/knowledge-bases")).json()["knowledgeBases"]
    assert listed[0] == {"id": kb_id, "name": "문서", "embedModel": "bge-m3", "fileCount": 0,
                         "createdAt": listed[0]["createdAt"]}
    assert (await api.delete(f"/knowledge-bases/{kb_id}")).status_code == 204
    assert (await api.delete(f"/knowledge-bases/{kb_id}")).status_code == 404
    assert (await api.get("/knowledge-bases")).json()["knowledgeBases"] == []


async def test_a_duplicate_name_is_a_409(api):
    await _kb(api, "같은")
    response = await api.post("/knowledge-bases", json={"name": "같은"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "NAME_TAKEN"


async def test_a_bad_name_is_a_422(api):
    assert (await api.post("/knowledge-bases", json={"name": ""})).status_code == 422
    assert (await api.post("/knowledge-bases", json={"name": "x" * 101})).status_code == 422


async def test_upload_queues_the_file_and_lists_it_without_content(api):
    kb_id = await _kb(api)
    uploaded = await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "보고서.md"},
                             content="# 제목\n\n본문".encode("utf-8"), headers={"content-type": "text/markdown"})
    assert uploaded.status_code == 202, uploaded.text
    body = uploaded.json()
    assert body["filename"] == "보고서.md" and body["status"] == "pending"

    files = (await api.get(f"/knowledge-bases/{kb_id}/files")).json()["files"]
    assert files[0]["id"] == body["id"] and files[0]["size"] == len("# 제목\n\n본문".encode("utf-8"))
    assert "content" not in files[0] and "본문" not in (await api.get(f"/knowledge-bases/{kb_id}/files")).text
    assert (await api.get("/knowledge-bases")).json()["knowledgeBases"][0]["fileCount"] == 1


async def test_upload_needs_a_name_and_an_existing_knowledge_base(api):
    kb_id = await _kb(api)
    assert (await api.put(f"/knowledge-bases/{kb_id}/files", content=b"x")).status_code == 422
    assert (await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "a/b.md"}, content=b"x")).status_code == 422
    assert (await api.put("/knowledge-bases/00000000-0000-0000-0000-00000000dead/files", params={"name": "a.md"},
                          content=b"x")).status_code == 404
    assert (await api.put("/knowledge-bases/not-a-uuid/files", params={"name": "a.md"}, content=b"x")).status_code == 404


async def test_an_upload_over_the_file_limit_is_a_413(pool, redis, api):
    kb_id = await _kb(api)
    small = dataclasses.replace(api.config, kb_max_file_bytes=10)
    async with AsyncClient(transport=ASGITransport(app=create_app(small, pool, redis)), base_url="http://api",
                           headers={"Authorization": "Bearer test-token"}) as client:
        response = await client.put(f"/knowledge-bases/{kb_id}/files", params={"name": "big.md"}, content=b"x" * 11)
    assert response.status_code == 413 and response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert (await api.get(f"/knowledge-bases/{kb_id}/files")).json()["files"] == []


async def test_deleting_a_file_removes_it_from_the_list(api):
    kb_id = await _kb(api)
    file_id = (await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "a.md"}, content=b"# a")).json()["id"]
    assert (await api.delete(f"/knowledge-bases/{kb_id}/files/{file_id}")).status_code == 204
    assert (await api.delete(f"/knowledge-bases/{kb_id}/files/{file_id}")).status_code == 404
    assert (await api.get(f"/knowledge-bases/{kb_id}/files")).json()["files"] == []
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_api_knowledge_bases.py -q`  Expected: 404 응답으로 FAIL.

- [ ] **Step 3: 구현**

`engine/api/routers/knowledge_bases.py`:
```python
"""Knowledge bases and their files (knowledge-base design §6).

An upload is the raw file body with its Content-Type, not multipart: one fewer dependency, and the
proxy streams it through unchanged. It is the one route whose body limit is KB_MAX_FILE_BYTES rather
than MAX_BODY_BYTES.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from psycopg.errors import UniqueViolation

from engine.api.body import field, read_json, require_object
from engine.api.errors import ApiError
from engine.kb import store

router = APIRouter()

MAX_NAME_CHARS = 100
MAX_FILENAME_CHARS = 255
EMBED_DIM = 1024  # what kb_chunks.embedding holds (migration 0004)


def _kb_id(raw: str) -> str:
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise ApiError(404, "NOT_FOUND", "지식베이스를 찾을 수 없습니다") from None


def _kb_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(row["id"]), "name": row["name"], "embedModel": row["embed_model"],
            "fileCount": int(row.get("file_count", 0)), "createdAt": row["created_at"].isoformat()}


def _file_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(row["id"]), "filename": row["filename"], "size": int(row["size"]), "status": row["status"],
            "error": row.get("error"), "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else row["created_at"].isoformat()}


@router.get("/knowledge-bases")
async def list_knowledge_bases(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        rows = await store.list_kbs(conn)
    return {"knowledgeBases": [_kb_view(row) for row in rows]}


@router.post("/knowledge-bases", status_code=201)
async def create_knowledge_base(request: Request) -> dict[str, Any]:
    body = require_object(await read_json(request))
    name = field(body, "name", str).strip()
    if not 1 <= len(name) <= MAX_NAME_CHARS:
        raise ApiError(422, "REQUEST_ERROR", f"이름은 1~{MAX_NAME_CHARS}자여야 합니다")
    config = request.app.state.config
    try:
        async with request.app.state.pool.connection() as conn:
            row = await store.create_kb(conn, name=name, embed_model=config.kb_embed_model, dim=EMBED_DIM)
    except UniqueViolation:
        raise ApiError(409, "NAME_TAKEN", "같은 이름의 지식베이스가 이미 있습니다") from None
    return _kb_view(row)


@router.delete("/knowledge-bases/{kb_id}", status_code=204)
async def delete_knowledge_base(kb_id: str, request: Request) -> Response:
    async with request.app.state.pool.connection() as conn:
        if not await store.delete_kb(conn, _kb_id(kb_id)):
            raise ApiError(404, "NOT_FOUND", "지식베이스를 찾을 수 없습니다")
    return Response(status_code=204)


@router.get("/knowledge-bases/{kb_id}/files")
async def list_files(kb_id: str, request: Request) -> dict[str, Any]:
    kb_id = _kb_id(kb_id)
    async with request.app.state.pool.connection() as conn:
        if await store.get_kb(conn, kb_id) is None:
            raise ApiError(404, "NOT_FOUND", "지식베이스를 찾을 수 없습니다")
        rows = await store.list_files(conn, kb_id)
    return {"files": [_file_view(row) for row in rows]}


@router.put("/knowledge-bases/{kb_id}/files", status_code=202)
async def upload_file(kb_id: str, request: Request) -> dict[str, Any]:
    kb_id = _kb_id(kb_id)
    name = (request.query_params.get("name") or "").strip()
    if not 1 <= len(name) <= MAX_FILENAME_CHARS or "/" in name or "\\" in name or "\x00" in name:
        raise ApiError(422, "REQUEST_ERROR", "파일 이름이 필요합니다 (경로 구분자 없이 255자 이하)")
    media_type = request.headers.get("content-type") or "application/octet-stream"
    content = await _read_bytes(request, request.app.state.config.kb_max_file_bytes)
    if not content:
        raise ApiError(422, "REQUEST_ERROR", "빈 파일은 올릴 수 없습니다")
    async with request.app.state.pool.connection() as conn, conn.transaction():
        if await store.get_kb(conn, kb_id) is None:
            raise ApiError(404, "NOT_FOUND", "지식베이스를 찾을 수 없습니다")
        row = await store.add_file(conn, kb_id=kb_id, filename=name, media_type=media_type, content=content)
    return _file_view({**row, "error": None, "updated_at": None})


@router.delete("/knowledge-bases/{kb_id}/files/{file_id}", status_code=204)
async def delete_file(kb_id: str, file_id: str, request: Request) -> Response:
    kb_id = _kb_id(kb_id)
    async with request.app.state.pool.connection() as conn:
        row = await store.get_file(conn, file_id)
        if row is None or str(row["kb_id"]) != kb_id or not await store.delete_file(conn, file_id):
            raise ApiError(404, "NOT_FOUND", "파일을 찾을 수 없습니다")
    return Response(status_code=204)


async def _read_bytes(request: Request, limit: int) -> bytes:
    """Like body.read_json's bounded read, for bytes: stop the instant the running total passes `limit`."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def _too_large(limit: int) -> ApiError:
    return ApiError(413, "PAYLOAD_TOO_LARGE", f"파일이 너무 큽니다 (최대 {limit // 1_000_000}MB)")
```
`engine/api/app.py` — import를 `from engine.api.routers import knowledge_bases, node_types, runs, secrets, workflows`로 바꾸고 `app.include_router(secrets.router)` 아래에 `app.include_router(knowledge_bases.router)` 추가.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_api_knowledge_bases.py tests/test_api_basics.py -q`  Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add services/engine/engine/api services/engine/tests/test_api_knowledge_bases.py
git commit -m "feat(engine): knowledge base API — create, upload, list, delete" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: 배포 설정

**Files:**
- Modify: `deploy/docker-compose.yml`, `deploy/docker-compose.dev.yml`, `deploy/.env.example`, `services/engine/README.md`

- [ ] **Step 1: compose**

`deploy/docker-compose.yml`:
- `x-engine.environment`에 추가:
  ```yaml
      MINERU_BASE_URL: ${MINERU_BASE_URL:-}
      RERANK_BASE_URL: ${RERANK_BASE_URL:-}
      KB_EMBED_MODEL: ${KB_EMBED_MODEL:-bge-m3}
  ```
- `postgres.image`를 `pgvector/pgvector:pg17`로 바꾼다.
- `worker` 서비스 뒤에 추가:
  ```yaml
    ingester:
      <<: *engine
      command: ["python", "-m", "engine.ingest.main"]
      # Document ingestion is minutes per file and must never sit in a run worker's slot. Scale
      # separately: each replica claims jobs under its own lease.
      deploy:
        replicas: ${INGESTER_REPLICAS:-1}
  ```
`deploy/docker-compose.dev.yml`의 `postgres.image`도 `pgvector/pgvector:pg17`로 바꾼다.

- [ ] **Step 2: `.env.example`**

`OLLAMA_BASE_URL=...` 블록 아래에 추가:
```
# Knowledge base. Both servers run on the GPU host next to Ollama; neither is bundled here.
# MinerU API server (document → markdown). Unset: only .md/.txt files can be ingested.
MINERU_BASE_URL=
# text-embeddings-inference running a reranker such as BAAI/bge-reranker-v2-m3. Unset: rerank nodes fail.
RERANK_BASE_URL=
# Ollama embedding model (must be 1024-dimensional in this release).
KB_EMBED_MODEL=bge-m3
INGESTER_REPLICAS=1
```

- [ ] **Step 3: README**

`services/engine/README.md`의 환경변수 표(62행 부근 `OLLAMA_BASE_URL` 행 뒤)에 행 추가:
```
| `MINERU_BASE_URL`      | (unset)                                | MinerU API server for document parsing; unset limits ingestion to .md/.txt |
| `MINERU_TIMEOUT_SEC`   | `600`                                  | One document's parse timeout                                      |
| `RERANK_BASE_URL`      | (unset)                                | text-embeddings-inference `/rerank` endpoint for the rerank node  |
| `KB_EMBED_MODEL`       | `bge-m3`                               | Ollama embedding model for knowledge bases (1024 dims)            |
| `KB_MAX_FILE_BYTES`    | `50000000`                             | Upload size limit per file                                        |
| `INGEST_MAX_JOBS`      | `1`                                    | Files one ingester processes at once                              |
```
같은 파일의 실행 방법 절에 `python -m engine.ingest.main` 한 줄을 worker 실행 줄 옆에 추가한다.

- [ ] **Step 4: 문법 확인**

Run (저장소 루트에서): `docker compose -f deploy/docker-compose.yml config --quiet` — `.env`가 없어 변수 오류가 나면 `POSTGRES_PASSWORD=x ENGINE_API_TOKEN=0123456789abcdef LANGGRAPH_AES_KEY=$(printf '0%.0s' $(seq 32)) ENGINE_SECRET_KEY=$(printf '1%.0s' $(seq 32)) OLLAMA_BASE_URL=http://o docker compose -f deploy/docker-compose.yml config --quiet`. Expected: 출력 없음(exit 0).

- [ ] **Step 5: 커밋**

```bash
git add deploy services/engine/README.md
git commit -m "chore(deploy): ingester service, pgvector image, knowledge base settings" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: 웹 — 프록시 허용과 브라우저 클라이언트

**Files:**
- Modify: `apps/web/lib/engine/paths.ts:20`, `apps/web/lib/engine/paths.test.ts`
- Create: `apps/web/lib/engine/knowledgeBases.ts`, `apps/web/lib/engine/knowledgeBases.test.ts`

- [ ] **Step 1: 실패하는 테스트**

`lib/engine/paths.test.ts`의 `it.each([...])("passes %j through"` 목록에 추가:
```ts
    [["knowledge-bases"], "/knowledge-bases"],
    [["knowledge-bases", "abc", "files"], "/knowledge-bases/abc/files"],
```
`lib/engine/knowledgeBases.test.ts`:
```ts
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { createKnowledgeBase, deleteFile, listFiles, listKnowledgeBases, uploadFile } from "./knowledgeBases"

function reply(status: number, body?: unknown) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

let calls: { url: string; init?: RequestInit }[]

function answer(...responses: Response[]) {
  const queue = [...responses]
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init })
      return Promise.resolve(queue.shift() ?? reply(500, null))
    }),
  )
}

beforeEach(() => {
  calls = []
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const KB = { id: "kb_1", name: "문서", embedModel: "bge-m3", fileCount: 2, createdAt: "2026-09-22T00:00:00.000Z" }
const FILE = { id: "f_1", filename: "a.md", size: 12, status: "pending", error: null, createdAt: "2026-09-22T00:00:00.000Z", updatedAt: "2026-09-22T00:00:00.000Z" }

describe("listKnowledgeBases", () => {
  it("reads the list through the proxy", async () => {
    answer(reply(200, { knowledgeBases: [KB] }))
    expect(await listKnowledgeBases()).toEqual({ outcome: "ok", knowledgeBases: [KB] })
    expect(calls[0]?.url).toBe("/api/engine/knowledge-bases")
  })

  it("passes the engine's message through on failure", async () => {
    answer(reply(503, { error: { code: "INTERNAL", message: "엔진 점검 중" } }))
    expect(await listKnowledgeBases()).toEqual({ outcome: "failed", message: "엔진 점검 중" })
  })
})

describe("createKnowledgeBase", () => {
  it("posts the name and returns the row", async () => {
    answer(reply(201, KB))
    expect(await createKnowledgeBase("문서")).toEqual({ outcome: "ok", knowledgeBase: KB })
    expect(calls[0]?.init?.method).toBe("POST")
    expect(JSON.parse(calls[0]?.init?.body as string)).toEqual({ name: "문서" })
  })
})

describe("uploadFile", () => {
  it("PUTs the raw file with its name in the query and its type in the header", async () => {
    answer(reply(202, FILE))
    const file = new File(["# a"], "보고서.md", { type: "text/markdown" })

    expect(await uploadFile("kb_1", file)).toEqual({ outcome: "ok", file: FILE })
    expect(calls[0]?.url).toBe("/api/engine/knowledge-bases/kb_1/files?name=%EB%B3%B4%EA%B3%A0%EC%84%9C.md")
    expect(calls[0]?.init?.method).toBe("PUT")
    expect((calls[0]?.init?.headers as Record<string, string>)["content-type"]).toBe("text/markdown")
    expect(calls[0]?.init?.body).toBe(file)
  })

  it("falls back to octet-stream when the browser knows no type", async () => {
    answer(reply(202, FILE))
    await uploadFile("kb_1", new File(["x"], "a.hwp"))
    expect((calls[0]?.init?.headers as Record<string, string>)["content-type"]).toBe("application/octet-stream")
  })

  it("reports a 413 in the engine's words", async () => {
    answer(reply(413, { error: { code: "PAYLOAD_TOO_LARGE", message: "파일이 너무 큽니다 (최대 50MB)" } }))
    expect(await uploadFile("kb_1", new File(["x"], "a.md"))).toEqual({ outcome: "failed", message: "파일이 너무 큽니다 (최대 50MB)" })
  })
})

describe("listFiles and deleteFile", () => {
  it("reads and deletes under the knowledge base", async () => {
    answer(reply(200, { files: [FILE] }), reply(204))
    expect(await listFiles("kb_1")).toEqual({ outcome: "ok", files: [FILE] })
    expect(await deleteFile("kb_1", "f_1")).toEqual({ outcome: "ok" })
    expect(calls[1]?.url).toBe("/api/engine/knowledge-bases/kb_1/files/f_1")
    expect(calls[1]?.init?.method).toBe("DELETE")
  })
})
```

- [ ] **Step 2: 실패 확인**

Run (`apps/web`): `npx vitest run lib/engine/paths.test.ts lib/engine/knowledgeBases.test.ts`  Expected: FAIL.

- [ ] **Step 3: 구현**

`lib/engine/paths.ts`: `const PREFIXES = new Set(["workflows", "runs", "node-types", "secrets", "healthz", "knowledge-bases"])`

`lib/engine/knowledgeBases.ts`:
```ts
/** Knowledge bases from the browser, through the BFF proxy (knowledge-base design §6, §7).
 *
 * An upload is the File object itself as the request body: fetch streams it, the proxy streams it on,
 * and nothing here buffers 50 MB into memory. The name rides in the query because the body has no
 * other place for it.
 */

import { messageOf } from "./envelope"

export interface KnowledgeBaseSummary {
  id: string
  name: string
  embedModel: string
  fileCount: number
  createdAt: string
}

export type FileStatus = "pending" | "processing" | "ready" | "failed"

export interface KbFile {
  id: string
  filename: string
  size: number
  status: FileStatus
  error: string | null
  createdAt: string
  updatedAt: string
}

type Failed = { outcome: "failed"; message: string }

export async function listKnowledgeBases(init: { signal?: AbortSignal } = {}): Promise<{ outcome: "ok"; knowledgeBases: KnowledgeBaseSummary[] } | Failed> {
  const response = await fetch("/api/engine/knowledge-bases", { signal: init.signal })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) return { outcome: "failed", message: messageOf(body, `지식베이스 목록을 불러오지 못했습니다 (${response.status})`) }
  const raw = (body as { knowledgeBases?: unknown } | null)?.knowledgeBases
  if (!Array.isArray(raw)) return { outcome: "failed", message: "지식베이스 응답을 이해하지 못했습니다" }
  return { outcome: "ok", knowledgeBases: raw as KnowledgeBaseSummary[] }
}

export async function createKnowledgeBase(name: string): Promise<{ outcome: "ok"; knowledgeBase: KnowledgeBaseSummary } | Failed> {
  const response = await fetch("/api/engine/knowledge-bases", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
  })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) return { outcome: "failed", message: messageOf(body, `지식베이스를 만들지 못했습니다 (${response.status})`) }
  return { outcome: "ok", knowledgeBase: body as KnowledgeBaseSummary }
}

export async function deleteKnowledgeBase(id: string): Promise<{ outcome: "ok" } | Failed> {
  const response = await fetch(`/api/engine/knowledge-bases/${encodeURIComponent(id)}`, { method: "DELETE" })
  if (response.status === 204) return { outcome: "ok" }
  const body: unknown = await response.json().catch(() => null)
  return { outcome: "failed", message: messageOf(body, `지식베이스를 삭제하지 못했습니다 (${response.status})`) }
}

export async function listFiles(kbId: string, init: { signal?: AbortSignal } = {}): Promise<{ outcome: "ok"; files: KbFile[] } | Failed> {
  const response = await fetch(`/api/engine/knowledge-bases/${encodeURIComponent(kbId)}/files`, { signal: init.signal })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) return { outcome: "failed", message: messageOf(body, `파일 목록을 불러오지 못했습니다 (${response.status})`) }
  const raw = (body as { files?: unknown } | null)?.files
  if (!Array.isArray(raw)) return { outcome: "failed", message: "파일 목록 응답을 이해하지 못했습니다" }
  return { outcome: "ok", files: raw as KbFile[] }
}

export async function uploadFile(kbId: string, file: File): Promise<{ outcome: "ok"; file: KbFile } | Failed> {
  const response = await fetch(
    `/api/engine/knowledge-bases/${encodeURIComponent(kbId)}/files?name=${encodeURIComponent(file.name)}`,
    { method: "PUT", headers: { "content-type": file.type || "application/octet-stream" }, body: file },
  )
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) return { outcome: "failed", message: messageOf(body, `파일을 올리지 못했습니다 (${response.status})`) }
  return { outcome: "ok", file: body as KbFile }
}

export async function deleteFile(kbId: string, fileId: string): Promise<{ outcome: "ok" } | Failed> {
  const response = await fetch(
    `/api/engine/knowledge-bases/${encodeURIComponent(kbId)}/files/${encodeURIComponent(fileId)}`,
    { method: "DELETE" },
  )
  if (response.status === 204) return { outcome: "ok" }
  const body: unknown = await response.json().catch(() => null)
  return { outcome: "failed", message: messageOf(body, `파일을 삭제하지 못했습니다 (${response.status})`) }
}
```

- [ ] **Step 4: 통과 확인**

Run: `npx vitest run lib/engine` 그리고 `npx tsc --noEmit`(기존 LayoutProps 오류만), `npx eslint`.  Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add apps/web/lib/engine
git commit -m "feat(web): knowledge base client and proxy prefix" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: 웹 — `/knowledge-bases` 화면

**Files:**
- Create: `apps/web/components/knowledge/KnowledgeBasesScreen.tsx`, `apps/web/components/knowledge/KnowledgeBasesScreen.test.tsx`, `apps/web/app/knowledge-bases/page.tsx`
- Modify: `apps/web/components/shell/Shell.tsx:29-31`

- [ ] **Step 1: 실패하는 테스트**

`components/knowledge/KnowledgeBasesScreen.test.tsx`:
```tsx
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { KbFile, KnowledgeBaseSummary } from "@/lib/engine/knowledgeBases"

import { KnowledgeBasesScreen } from "./KnowledgeBasesScreen"

const KB: KnowledgeBaseSummary = { id: "kb_1", name: "제품 문서", embedModel: "bge-m3", fileCount: 1, createdAt: "2026-09-22T00:00:00.000Z" }
const READY: KbFile = { id: "f_1", filename: "guide.pdf", size: 2048, status: "ready", error: null, createdAt: "2026-09-22T00:00:00.000Z", updatedAt: "2026-09-22T00:00:00.000Z" }
const PENDING: KbFile = { ...READY, id: "f_2", filename: "new.md", status: "pending" }
const FAILED: KbFile = { ...READY, id: "f_3", filename: "bad.xyz", status: "failed", error: "문서를 읽지 못했습니다 (MinerU 422)" }

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })
}

let calls: { url: string; init?: RequestInit }[]
let fetchMock: ReturnType<typeof vi.fn>

function answer(route: (url: string, init?: RequestInit) => Response) {
  fetchMock = vi.fn((url: string, init?: RequestInit) => {
    calls.push({ url, init })
    return Promise.resolve(route(url, init))
  })
  vi.stubGlobal("fetch", fetchMock)
}

beforeEach(() => {
  calls = []
  answer((url) => (url.endsWith("/files") ? json(200, { files: [READY] }) : json(200, { knowledgeBases: [KB] })))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe("the list", () => {
  it("shows every knowledge base with its file count", () => {
    render(<KnowledgeBasesScreen initial={[KB]} />)
    expect(screen.getByRole("button", { name: /제품 문서/ })).toHaveTextContent("파일 1개")
  })

  it("creates one by name and reloads the list", async () => {
    answer((url, init) =>
      init?.method === "POST" ? json(201, { ...KB, id: "kb_2", name: "새 문서" }) : json(200, { knowledgeBases: [KB, { ...KB, id: "kb_2", name: "새 문서" }] }),
    )
    render(<KnowledgeBasesScreen initial={[KB]} />)
    await userEvent.type(screen.getByLabelText("새 지식베이스 이름"), "새 문서")
    await userEvent.click(screen.getByRole("button", { name: "만들기" }))
    await waitFor(() => expect(screen.getByRole("button", { name: /새 문서/ })).toBeInTheDocument())
  })
})

describe("files of the open knowledge base", () => {
  it("lists them with status, and a failed file shows why", async () => {
    answer((url) => (url.endsWith("/files") ? json(200, { files: [READY, FAILED] }) : json(200, { knowledgeBases: [KB] })))
    render(<KnowledgeBasesScreen initial={[KB]} />)
    await userEvent.click(screen.getByRole("button", { name: /제품 문서/ }))

    expect(await screen.findByText("guide.pdf")).toBeInTheDocument()
    expect(screen.getByText("완료")).toBeInTheDocument()
    expect(screen.getByText("문서를 읽지 못했습니다 (MinerU 422)")).toBeInTheDocument()
  })

  it("uploads each chosen file and reloads the list", async () => {
    answer((url, init) => {
      if (init?.method === "PUT") return json(202, PENDING)
      if (url.endsWith("/files")) return json(200, { files: [READY, PENDING] })
      return json(200, { knowledgeBases: [KB] })
    })
    render(<KnowledgeBasesScreen initial={[KB]} />)
    await userEvent.click(screen.getByRole("button", { name: /제품 문서/ }))
    await screen.findByText("guide.pdf")

    await userEvent.upload(screen.getByLabelText("파일 올리기"), new File(["# n"], "new.md", { type: "text/markdown" }))

    await waitFor(() => expect(screen.getByText("new.md")).toBeInTheDocument())
    expect(calls.some((c) => c.init?.method === "PUT" && c.url.includes("name=new.md"))).toBe(true)
  })

  it("polls every 5 seconds only while a file is pending or processing", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let phase = 0
    answer((url) => {
      if (!url.endsWith("/files")) return json(200, { knowledgeBases: [KB] })
      phase += 1
      return json(200, { files: [phase < 3 ? PENDING : READY] })
    })
    render(<KnowledgeBasesScreen initial={[KB]} />)
    await userEvent.click(screen.getByRole("button", { name: /제품 문서/ }))
    await screen.findByText("대기 중")

    await vi.advanceTimersByTimeAsync(5_100)
    await vi.advanceTimersByTimeAsync(5_100)
    await screen.findByText("완료")
    const after = calls.filter((c) => c.url.endsWith("/files")).length
    await vi.advanceTimersByTimeAsync(11_000)
    expect(calls.filter((c) => c.url.endsWith("/files")).length).toBe(after)
  })
})
```

- [ ] **Step 2: 실패 확인**

Run: `npx vitest run components/knowledge`  Expected: FAIL (모듈 없음).

- [ ] **Step 3: 구현**

`components/knowledge/KnowledgeBasesScreen.tsx`:
```tsx
"use client"

import { useEffect, useState } from "react"

import {
  createKnowledgeBase,
  deleteFile,
  deleteKnowledgeBase,
  listFiles,
  listKnowledgeBases,
  uploadFile,
  type KbFile,
  type KnowledgeBaseSummary,
} from "@/lib/engine/knowledgeBases"

/** Knowledge bases and their files (knowledge-base design §7).
 *
 * Ingestion happens somewhere else, minutes later, so the file table is a status board: it re-reads
 * itself every few seconds only while something is still pending, and stops the moment nothing is.
 */

const POLL_MS = 5_000
const STATUS: Record<KbFile["status"], string> = { pending: "대기 중", processing: "처리 중", ready: "완료", failed: "실패" }

export function KnowledgeBasesScreen({ initial }: { initial: KnowledgeBaseSummary[] }) {
  const [bases, setBases] = useState(initial)
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function refresh() {
    const result = await listKnowledgeBases()
    if (result.outcome === "ok") setBases(result.knowledgeBases)
    else setError(result.message)
  }

  const current = bases.find((kb) => kb.id === open) ?? null

  return (
    <div className="flex flex-col gap-8">
      <CreateForm onCreated={() => void refresh()} onError={setError} />
      {error === null ? null : (
        <p role="alert" className="border px-3 py-2 text-xs" style={{ borderRadius: "var(--radius)", borderColor: "var(--st-failed)", color: "var(--st-failed)" }}>
          {error}
        </p>
      )}
      <section>
        <h2 className="instrument-label mb-2">지식베이스</h2>
        {bases.length === 0 ? (
          <p className="text-sm text-fg-muted">아직 지식베이스가 없습니다.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {bases.map((kb) => (
              <li key={kb.id} className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={() => setOpen(kb.id === open ? null : kb.id)}
                  aria-pressed={kb.id === open}
                  className="readout flex-1 border border-ink-600 bg-ink-800 px-3 py-2 text-left text-sm aria-pressed:border-ink-400"
                  style={{ borderRadius: "var(--radius)" }}
                >
                  {kb.name} <span className="text-xs text-fg-faint">파일 {kb.fileCount}개</span>
                </button>
                <button
                  type="button"
                  className="text-xs text-fg-muted underline"
                  onClick={() => {
                    if (!window.confirm(`'${kb.name}' 지식베이스와 모든 파일을 삭제할까요? 이를 쓰는 워크플로는 실행에 실패합니다.`)) return
                    void deleteKnowledgeBase(kb.id).then((result) => {
                      if (result.outcome === "ok") {
                        if (open === kb.id) setOpen(null)
                        void refresh()
                      } else setError(result.message)
                    })
                  }}
                >
                  삭제
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      {current === null ? null : <Files kb={current} onChanged={() => void refresh()} onError={setError} />}
    </div>
  )
}

function CreateForm({ onCreated, onError }: { onCreated: () => void; onError: (message: string) => void }) {
  const [name, setName] = useState("")
  const [busy, setBusy] = useState(false)
  const ready = name.trim() !== "" && name.trim().length <= 100

  async function create() {
    if (!ready) return
    setBusy(true)
    const result = await createKnowledgeBase(name.trim())
    setBusy(false)
    if (result.outcome === "ok") {
      setName("")
      onCreated()
    } else onError(result.message)
  }

  return (
    <section>
      <h2 className="instrument-label mb-2">지식베이스 만들기</h2>
      <div className="flex items-center gap-2">
        <label htmlFor="kb-name" className="sr-only">새 지식베이스 이름</label>
        <input
          id="kb-name"
          className="flex-1 border border-ink-600 bg-ink-700 px-2 py-1.5 text-sm outline-none focus:border-ink-500"
          style={{ borderRadius: "var(--radius)" }}
          value={name}
          placeholder="예: 제품 매뉴얼"
          onChange={(event) => setName(event.target.value)}
        />
        <button
          type="button"
          onClick={() => void create()}
          disabled={!ready || busy}
          className="border px-3 py-1.5 text-xs disabled:opacity-35"
          style={{ borderRadius: "var(--radius)", borderColor: "var(--accent)", color: "var(--accent)" }}
        >
          {busy ? "만드는 중" : "만들기"}
        </button>
      </div>
    </section>
  )
}

function Files({ kb, onChanged, onError }: { kb: KnowledgeBaseSummary; onChanged: () => void; onError: (message: string) => void }) {
  const [files, setFiles] = useState<KbFile[] | null>(null)
  const [uploading, setUploading] = useState(0)

  async function load() {
    const result = await listFiles(kb.id)
    if (result.outcome === "ok") setFiles(result.files)
    else onError(result.message)
  }

  useEffect(() => {
    setFiles(null)
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reload when the open knowledge base changes
  }, [kb.id])

  // Only while something is in flight: an idle table polling forever is load on the engine for nothing.
  const busy = files?.some((file) => file.status === "pending" || file.status === "processing") ?? false
  useEffect(() => {
    if (!busy) return
    const timer = setInterval(() => void load(), POLL_MS)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `load` reads kb.id, which is the other dep
  }, [busy, kb.id])

  async function upload(chosen: FileList | null) {
    if (chosen === null || chosen.length === 0) return
    setUploading(chosen.length)
    for (const file of Array.from(chosen)) {
      const result = await uploadFile(kb.id, file)
      if (result.outcome === "failed") onError(`${file.name}: ${result.message}`)
      setUploading((n) => n - 1)
    }
    await load()
    onChanged()
  }

  return (
    <section>
      <h2 className="instrument-label mb-2">{kb.name}의 파일</h2>
      <div className="mb-3 flex items-center gap-3">
        <label htmlFor="kb-upload" className="border px-3 py-1.5 text-xs" style={{ borderRadius: "var(--radius)", borderColor: "var(--accent)", color: "var(--accent)" }}>
          파일 올리기
        </label>
        <input id="kb-upload" type="file" multiple className="sr-only" onChange={(event) => void upload(event.target.files)} />
        {uploading > 0 ? <span className="text-xs text-fg-muted">올리는 중 ({uploading})</span> : null}
        <span className="text-xs text-fg-faint">PDF·오피스 문서는 MinerU가, .md·.txt는 바로 처리됩니다. 파일당 최대 50MB.</span>
      </div>
      {files === null ? (
        <p className="text-sm text-fg-muted">불러오는 중…</p>
      ) : files.length === 0 ? (
        <p className="text-sm text-fg-muted">아직 파일이 없습니다.</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="rule-engraved text-left">
              <th scope="col" className="instrument-label pb-2 font-normal">이름</th>
              <th scope="col" className="instrument-label pb-2 font-normal">크기</th>
              <th scope="col" className="instrument-label pb-2 font-normal">상태</th>
              <th scope="col" className="sr-only">작업</th>
            </tr>
          </thead>
          <tbody>
            {files.map((file) => (
              <tr key={file.id} className="border-b border-ink-700">
                <td className="readout py-2 pr-4">{file.filename}</td>
                <td className="readout py-2 pr-4 text-xs text-fg-faint">{formatSize(file.size)}</td>
                <td className="py-2 pr-4 text-xs">
                  <span style={{ color: file.status === "failed" ? "var(--st-failed)" : file.status === "ready" ? "var(--st-succeeded)" : "var(--st-waiting)" }}>
                    {STATUS[file.status]}
                  </span>
                  {file.error === null ? null : <span className="ml-2 text-fg-muted">{file.error}</span>}
                </td>
                <td className="py-2 text-right">
                  <button
                    type="button"
                    className="text-xs text-fg-muted underline"
                    onClick={() => {
                      if (!window.confirm(`'${file.filename}'을 지식베이스에서 삭제할까요?`)) return
                      void deleteFile(kb.id, file.id).then((result) => {
                        if (result.outcome === "ok") {
                          void load()
                          onChanged()
                        } else onError(result.message)
                      })
                    }}
                  >
                    삭제
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
```
`app/knowledge-bases/page.tsx`:
```tsx
import { KnowledgeBasesScreen } from "@/components/knowledge/KnowledgeBasesScreen"
import { Shell } from "@/components/shell/Shell"
import { engineFetch } from "@/lib/engine/client"
import type { KnowledgeBaseSummary } from "@/lib/engine/knowledgeBases"

export const dynamic = "force-dynamic"

export default async function Page() {
  let bases: KnowledgeBaseSummary[] = []
  let error: string | null = null
  try {
    bases = (await engineFetch<{ knowledgeBases: KnowledgeBaseSummary[] }>("/knowledge-bases")).knowledgeBases
  } catch {
    error = "지식베이스 목록을 불러오지 못했습니다. 엔진에 연결할 수 없습니다."
  }

  return (
    <Shell title="지식베이스">
      {error === null ? null : (
        <p role="alert" className="mb-4 border px-3 py-2 text-xs" style={{ borderRadius: "var(--radius)", borderColor: "var(--st-failed)", color: "var(--st-failed)" }}>
          {error}
        </p>
      )}
      <KnowledgeBasesScreen initial={bases} />
    </Shell>
  )
}
```
`components/shell/Shell.tsx` — 시크릿 링크 아래에 추가:
```tsx
            <Link href="/knowledge-bases" className="text-fg-muted hover:text-fg">
              지식베이스
            </Link>
```

- [ ] **Step 4: 통과 확인**

Run: `npx vitest run components/knowledge && npx tsc --noEmit; npx eslint`  Expected: 테스트 PASS, tsc는 기존 오류 1건만, eslint exit 0. eslint가 `react-hooks/exhaustive-deps` disable 주석을 "unused"로 보고하면 그 주석을 지운다.

- [ ] **Step 5: 커밋**

```bash
git add apps/web/components/knowledge apps/web/app/knowledge-bases apps/web/components/shell/Shell.tsx
git commit -m "feat(web): knowledge base screen — create, upload, watch ingestion" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: 웹 — 노드 패널의 지식베이스 드롭다운

**Files:**
- Modify: `apps/web/lib/panel/uiSchema.ts`, `apps/web/lib/panel/uiSchema.test.ts`, `apps/web/components/panel/widgets.tsx`
- Create: `apps/web/components/panel/KnowledgeBaseWidget.tsx`, `apps/web/components/panel/KnowledgeBaseWidget.test.tsx`

- [ ] **Step 1: 실패하는 테스트**

`lib/panel/uiSchema.test.ts`의 `describe("buildUiSchema"` 안에 추가:
```ts
  it("routes an x-knowledge-base property to the knowledge base picker, with Korean titles", () => {
    const ui = buildUiSchema(
      nodeType("kb_search", {
        knowledgeBase: { type: "string", "x-knowledge-base": true },
        query: { type: "string", "x-template": true },
        topK: { type: "integer" },
      }),
    )
    expect(field(ui, "knowledgeBase")?.["ui:widget"]).toBe("knowledgeBase")
    expect(field(ui, "knowledgeBase")?.["ui:title"]).toBe("지식베이스")
    expect(field(ui, "topK")?.["ui:title"]).toBe("검색 개수")
    expect(ui["ui:order"]).toEqual(["knowledgeBase", "query", "topK", "minScore", "*"])
  })
```
`components/panel/KnowledgeBaseWidget.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import type { WidgetProps } from "@rjsf/utils"

import { KnowledgeBaseWidget } from "./KnowledgeBaseWidget"

const BASES = [
  { id: "kb_1", name: "제품 문서", embedModel: "bge-m3", fileCount: 1, createdAt: "" },
  { id: "kb_2", name: "사내 규정", embedModel: "bge-m3", fileCount: 3, createdAt: "" },
]

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({ knowledgeBases: BASES }), { status: 200 }))))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function props(overrides: Partial<WidgetProps> = {}): WidgetProps {
  return { id: "root_knowledgeBase", value: undefined, onChange: vi.fn(), onBlur: vi.fn(), onFocus: vi.fn(), ...overrides } as unknown as WidgetProps
}

it("lists the knowledge bases by name and writes the chosen id", async () => {
  const onChange = vi.fn()
  render(<KnowledgeBaseWidget {...props({ onChange })} />)

  const select = await screen.findByRole("combobox")
  await userEvent.selectOptions(select, "kb_2")

  expect(screen.getByRole("option", { name: "사내 규정" })).toBeInTheDocument()
  expect(onChange).toHaveBeenCalledWith("kb_2")
})

it("keeps a saved id that is no longer listed, and says so", async () => {
  render(<KnowledgeBaseWidget {...props({ value: "kb_gone" })} />)
  const select = await screen.findByRole("combobox")
  expect((select as HTMLSelectElement).value).toBe("kb_gone")
  expect(screen.getByRole("option", { name: /삭제된 지식베이스/ })).toBeInTheDocument()
})
```

- [ ] **Step 2: 실패 확인**

Run: `npx vitest run lib/panel components/panel/KnowledgeBaseWidget.test.tsx`  Expected: FAIL.

- [ ] **Step 3: 구현**

`lib/panel/uiSchema.ts`:
- `TITLES`에 추가: `knowledgeBase: "지식베이스", query: "질문", topK: "검색 개수", minScore: "최소 점수", hits: "검색 결과", topN: "남길 개수",`
- `ORDER`에 추가: `kb_search: ["knowledgeBase", "query", "topK", "minScore"], rerank: ["query", "hits", "topN", "minScore"],`
- `isTemplate` 아래에:
  ```ts
  function isKnowledgeBase(property: unknown): boolean {
    return typeof property === "object" && property !== null && (property as Record<string, unknown>)["x-knowledge-base"] === true
  }
  ```
- `buildUiSchema`의 분기에 `else if (isKnowledgeBase(property)) field["ui:widget"] = "knowledgeBase"`를 `isTemplate` 분기 앞에 추가.

`components/panel/KnowledgeBaseWidget.tsx`:
```tsx
"use client"

import type { WidgetProps } from "@rjsf/utils"
import { useEffect, useState } from "react"

import { listKnowledgeBases, type KnowledgeBaseSummary } from "@/lib/engine/knowledgeBases"

/** The picker behind `x-knowledge-base` (knowledge-base design §7).
 *
 * It fetches the list itself rather than having the canvas thread it through: the list is small,
 * changes rarely, and only this widget wants it. A saved id that is no longer listed stays selected
 * and is labelled as gone -- silently switching the node to another knowledge base would be worse.
 */
export function KnowledgeBaseWidget({ id, value, disabled, readonly, onChange, onBlur }: WidgetProps) {
  const [bases, setBases] = useState<KnowledgeBaseSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    void listKnowledgeBases({ signal: controller.signal }).then((result) => {
      if (result.outcome === "ok") setBases(result.knowledgeBases)
      else setError(result.message)
    })
    return () => controller.abort()
  }, [])

  const current = typeof value === "string" ? value : ""
  const known = bases?.some((kb) => kb.id === current) ?? true

  return (
    <div>
      <select
        id={id}
        value={current}
        disabled={disabled === true || readonly === true || bases === null}
        onChange={(event) => onChange(event.target.value === "" ? undefined : event.target.value)}
        onBlur={() => onBlur(id, value)}
        className="w-full border border-ink-600 bg-ink-700 px-2 py-1.5 text-sm outline-none focus:border-ink-500"
        style={{ borderRadius: "var(--radius)" }}
      >
        <option value="">{bases === null ? "불러오는 중…" : "지식베이스를 고르세요"}</option>
        {current !== "" && !known ? <option value={current}>삭제된 지식베이스 ({current})</option> : null}
        {(bases ?? []).map((kb) => (
          <option key={kb.id} value={kb.id}>
            {kb.name}
          </option>
        ))}
      </select>
      {error === null ? null : <p className="mt-1 text-xs text-st-failed">{error}</p>}
      {bases !== null && bases.length === 0 ? (
        <p className="mt-1 text-xs text-fg-faint">지식베이스가 없습니다. 지식베이스 화면에서 먼저 만들어 주세요.</p>
      ) : null}
    </div>
  )
}
```
`components/panel/widgets.tsx` — `import { KnowledgeBaseWidget } from "./KnowledgeBaseWidget"` 추가, `WIDGETS`에 `knowledgeBase: KnowledgeBaseWidget,` 추가.

- [ ] **Step 4: 통과 확인**

Run: `npx vitest run && npx tsc --noEmit; npx eslint`  Expected: 전부 PASS(기존 828 + 새 테스트), tsc 기존 오류 1건만, eslint 0.

- [ ] **Step 5: 커밋**

```bash
git add apps/web/lib/panel apps/web/components/panel
git commit -m "feat(web): knowledge base picker for kb_search, titles for the RAG nodes" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 15: 스택 e2e

**Files:**
- Create: `apps/web/e2e/stack/knowledge-base.spec.ts`

이 스펙은 전체 compose 스택(`deploy/docker-compose.yml`, `.env` 채움, `docker compose up --build`)이 떠 있을 때만 돈다. MinerU·TEI 없이 `.md`만 쓴다.

- [ ] **Step 1: 스펙**

```ts
import { expect, test } from "@playwright/test"

import { createWorkflow, deleteWorkflow, editorPath, endNode, expectRunStatus, startNode } from "./support"

/** Upload a markdown file, wait for ingestion, and search it from a workflow (knowledge-base design §8).
 *
 * Markdown skips MinerU, so this runs against the compose stack alone: postgres (pgvector), api, worker,
 * ingester, web, and the Ollama host the stack already points at for the embedding.
 */

let kbId: string
let workflowId: string

test.beforeEach(async ({ request }) => {
  const created = await request.post("/api/engine/knowledge-bases", { data: { name: `e2e-kb-${Date.now()}` } })
  expect(created.status(), await created.text()).toBe(201)
  kbId = ((await created.json()) as { id: string }).id

  const uploaded = await request.put(`/api/engine/knowledge-bases/${kbId}/files?name=policy.md`, {
    headers: { "content-type": "text/markdown" },
    data: "# 환불 정책\n\n구매 후 14일 이내에는 전액 환불됩니다.\n\n# 배송\n\n주문 후 3일 안에 발송합니다.\n",
  })
  expect(uploaded.status(), await uploaded.text()).toBe(202)

  await expect
    .poll(async () => {
      const listed = await request.get(`/api/engine/knowledge-bases/${kbId}/files`)
      const { files } = (await listed.json()) as { files: { status: string; error: string | null }[] }
      if (files[0]?.status === "failed") throw new Error(`ingestion failed: ${files[0].error}`)
      return files[0]?.status
    }, { timeout: 60_000, intervals: [1_000] })
    .toBe("ready")

  const workflow = await createWorkflow(request, `e2e-kb-${Date.now()}`, {
    version: "1",
    nodes: [
      startNode({ question: { type: "string" } }),
      {
        id: "kb_search_1",
        type: "kb_search",
        position: { x: 340, y: 80 },
        config: { knowledgeBase: kbId, query: "{{ start.question }}", topK: 1 },
      },
      endNode({ answer: "{{ kb_search_1.context }}" }),
    ],
    edges: [
      { id: "edge_1", source: "start", target: "kb_search_1" },
      { id: "edge_2", source: "kb_search_1", target: "end" },
    ],
  })
  workflowId = workflow.id
})

test.afterEach(async ({ request }) => {
  await deleteWorkflow(request, workflowId)
  await request.delete(`/api/engine/knowledge-bases/${kbId}`).catch(() => undefined)
})

test("a question finds the matching section of an uploaded file", async ({ page, request }) => {
  await page.goto(editorPath(workflowId))
  await expect(page.locator(".react-flow__node")).toHaveCount(3, { timeout: 20_000 })

  const run = page.getByRole("button", { name: "실행" })
  await expect(run).toBeEnabled({ timeout: 20_000 })
  await run.click()
  await page.getByLabel("question").fill("환불은 언제까지 되나요?")
  await page.getByRole("button", { name: "실행", exact: true }).last().click()
  await expectRunStatus(page, /성공/, 60_000)

  const listed = await request.get(`/api/engine/workflows/${workflowId}/runs`)
  const { runs } = (await listed.json()) as { runs: { id: string }[] }
  const detail = await request.get(`/api/engine/runs/${runs[0].id}`)
  const { outputs } = (await detail.json()) as { outputs: { answer: string } }
  expect(outputs.answer).toContain("14일")
  expect(outputs.answer).not.toContain("배송")
})
```

- [ ] **Step 2: 스택에서 실행**

`deploy/.env`에 `OLLAMA_BASE_URL`이 bge-m3를 가진 Ollama를 가리키는지 확인한 뒤(`curl $OLLAMA_BASE_URL/api/tags | grep bge-m3`), 저장소 루트에서:
```bash
docker compose -f deploy/docker-compose.yml up --build -d
```
`apps/web`에서:
```bash
PLAYWRIGHT_CHROMIUM_PATH="C:/Program Files/Google/Chrome/Application/chrome.exe" npx playwright test --project=stack knowledge-base
```
Expected: 1 passed. Ollama에 닿을 수 없는 환경이면 결과를 "실행 불가"로 기록하고 이유(연결 오류 메시지)를 적는다.

- [ ] **Step 3: 프록시의 큰 본문 확인**

스택이 떠 있을 때(`apps/web`에서, 30 MB 텍스트 파일):
```bash
python -c "open('big.txt','w').write('가나다라 ' * 6_000_000)" && curl -s -o /dev/null -w "%{http_code}\n" -X PUT -H "content-type: text/plain" --data-binary @big.txt "http://localhost:3000/api/engine/knowledge-bases/$KB_ID/files?name=big.txt"; rm big.txt
```
(`$KB_ID`는 `curl -s http://localhost:3000/api/engine/knowledge-bases | python -c "import sys,json; print(json.load(sys.stdin)['knowledgeBases'][0]['id'])"`로 얻는다.) Expected: `202`. `413`이나 `500`이면 `apps/web/app/api/engine/[...path]/route.ts`가 아니라 Next의 기본 본문 제한이 원인인지 `docker compose logs web`로 확인하고, 계획 밖의 수정이 필요하면 멈추고 보고한다. 확인 후 그 파일은 지식베이스 화면에서 삭제한다.

- [ ] **Step 4: 커밋**

```bash
git add apps/web/e2e/stack/knowledge-base.spec.ts
git commit -m "test(web): stack e2e — upload, ingest, search from a workflow" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 16: 마무리

- [ ] **Step 1: 전체 검증**

`services/engine`: `uv run pytest -q` → 기존 기준선 + 새 테스트 전부 PASS(기존 간헐 실패 1건 제외).
`apps/web`: `npx vitest run && npx eslint && npx tsc --noEmit` → PASS / 0 / 기존 오류 1건.

- [ ] **Step 2: 스펙 대조**

스펙 §3~§8의 각 항목이 Task 1~15 중 어디서 구현됐는지 한 줄씩 확인한다. 빠진 것이 있으면 태스크를 추가해 처리한다.

- [ ] **Step 3: superpowers:finishing-a-development-branch 로 통합 (PR 생성 여부는 사용자에게 묻는다)**
