# 지식베이스(RAG) 설계 — 적재, `kb_search`, `rerank`

날짜: 2026-09-22 · 범위: `services/engine`, `apps/web`, `deploy`
선행 결정: `docs/superpowers/plans/2026-09-11-roadmap.md` "Deferred: MinerU / RAG platform"

## 1. 목표

문서를 지식베이스에 올려 두면 여러 워크플로가 그 내용을 검색해 LLM 답변에 쓸 수 있게 한다.

- 사용자는 웹 화면에서 지식베이스를 만들고 파일을 올린다. 파일은 백그라운드에서 파싱·청킹·임베딩되어 저장된다.
- 워크플로에는 노드 두 개가 생긴다: **지식 검색**(`kb_search`)과 **리랭킹**(`rerank`).
- 대표 흐름: `시작(question)` → `지식 검색(topK 20)` → `리랭킹(topN 3)` → `LLM({{rerank_1.context}})` → `끝`.

### 왜 청킹·임베딩은 노드가 아닌가

로드맵의 결정을 따른다. 청킹은 데이터 개수만큼 갈라지는 fan-out인데 엔진은 DSL에 선언된 정적 분기만 지원하고,
임베딩 벡터(1024차원 ≈ 12 KB JSON)를 노드 출력으로 넘기면 청크 100개쯤에서 노드 출력 상한 1 MB
(`engine/compiler/wrapper.py` `MAX_OUTPUT_BYTES`)를 넘으며 체크포인트가 매 단계 이를 복사한다. 그래서 적재는
엔진 실행 밖의 별도 서비스가 맡고, 워크플로 노드는 벡터를 실행 상태에 싣지 않는 검색·리랭킹만 둔다.

## 2. 외부 구성요소 (운영자가 GPU 호스트에 띄움, compose에 포함하지 않음)

| 구성요소 | 용도 | 엔진 설정 |
|---|---|---|
| Ollama (기존) | 임베딩 `POST /api/embed`, 기본 모델 `bge-m3`(1024차원) | `OLLAMA_BASE_URL` (기존) |
| MinerU API 서버 | 문서 → 마크다운 파싱 | `MINERU_BASE_URL`, `MINERU_TIMEOUT_SEC`(기본 600) |
| TEI (text-embeddings-inference) | `bge-reranker-v2-m3` 리랭킹 `POST /rerank` | `RERANK_BASE_URL` |

세 주소 모두 운영자 설정값이므로 테넌트용 HTTP 허용 목록(`http_allowlist`) 정책의 대상이 아니다. MinerU의
정확한 엔드포인트와 요청 형식은 배포된 버전에 맞춘다 — 구현 계획의 첫 MinerU 태스크에서 배포본의 API 문서로 확정한다.

## 3. 데이터 모델 (Postgres + pgvector)

compose의 Postgres 이미지를 `postgres:17-alpine`에서 `pgvector/pgvector:pg17`로 바꾸고, 마이그레이션 `0004`가
`CREATE EXTENSION IF NOT EXISTS vector`와 아래 테이블을 만든다. 테스트의 testcontainers Postgres 이미지도 같게 바꾼다.

| 테이블 | 컬럼 | 비고 |
|---|---|---|
| `knowledge_bases` | `id`, `name`(unique), `embed_model`, `dim`, `created_at` | 모델·차원은 생성 시 고정. 바꾸려면 새 지식베이스 |
| `kb_files` | `id`, `kb_id`(FK cascade), `filename`, `media_type`, `size`, `content bytea`, `status`, `error`, `created_at`, `updated_at` | `status` ∈ `pending`/`processing`/`ready`/`failed` |
| `ingest_jobs` | `id`, `file_id`(FK cascade, unique), `attempt`, `lease_owner`, `lease_until`, `next_attempt_at`, `created_at` | 파일당 작업 하나. 완료·최종 실패 시 삭제 |
| `kb_chunks` | `id`, `kb_id`, `file_id`(FK cascade), `ordinal`, `heading`, `text`, `embedding vector(1024)` | HNSW 인덱스 `vector_cosine_ops` |

- v1은 차원을 1024로 고정한다. `knowledge_bases.dim`은 임베딩 응답 검증에 쓰고, 다른 차원이 필요해지면 그때 넓힌다.
- 파일 크기 상한 `KB_MAX_FILE_BYTES` 기본 50,000,000.
- 원본은 `kb_files.content`에 보관한다(오브젝트 스토리지 없음). 파일 삭제는 청크·작업까지 cascade로 지운다.

## 4. 적재 파이프라인 — `ingester` 서비스

compose에 서비스 `ingester`를 추가한다. 이미지는 `engine:local` 그대로, 명령만 `python -m engine.ingest.main`.
환경변수는 DB 접속과 `OLLAMA_BASE_URL`, `MINERU_BASE_URL`, `MINERU_TIMEOUT_SEC`, `INGEST_MAX_JOBS`, `KB_EMBED_MODEL`.
`RERANK_BASE_URL`은 worker(노드 실행)에만 필요하다. `.env.example`에 새 변수를 모두 추가한다.

### 4.1 작업 잡기

기존 worker의 runs lease 방식을 따른다: `ingest_jobs`에서 `lease_until`이 지났거나 비었고 `next_attempt_at`이 된
행 하나를 `FOR UPDATE SKIP LOCKED`로 잡아 `lease_owner`/`lease_until`을 쓴다. 처리 중에는 heartbeat로 lease를
연장한다. 잡으면 `kb_files.status = processing`. 동시 처리 수는 `INGEST_MAX_JOBS`(기본 1).

### 4.2 처리 단계

1. **파싱**: `media_type`이 `text/markdown`·`text/plain`이거나 확장자가 `.md`/`.txt`면 UTF-8로 그대로 쓴다.
   그 밖에는 MinerU에 보내 마크다운을 받는다.
2. **청킹**(순수 함수, 외부 라이브러리 없음): 마크다운 제목(`#`~`######`) 경계로 먼저 나누고, 1,000자를 넘는
   구간은 150자 겹침으로 다시 자른다. 각 청크의 `heading`은 가장 가까운 상위 제목. 공백뿐인 청크는 버린다.
3. **임베딩**: Ollama `/api/embed`에 32개씩 보낸다. 응답 벡터 차원이 `knowledge_bases.dim`과 다르면 실패.
4. **저장**: 한 트랜잭션에서 그 파일의 기존 청크를 지우고 새 청크를 넣은 뒤 `status = ready`, 작업 삭제.

### 4.3 오류

- **재시도**: MinerU·Ollama 연결 실패, 타임아웃, 5xx. 최대 3회, `next_attempt_at`을 30초·2분·10분 뒤로.
- **즉시 실패**: 4xx, 파싱 결과가 비었음, 청크 0개, 차원 불일치, UTF-8이 아닌 텍스트 파일.
- 실패하면 `status = failed`, `error`에 한국어 사유(예: `문서를 읽지 못했습니다 (MinerU 422)`)를 남기고 작업을 삭제한다.
- 처리 중 파일이 삭제되면(cascade로 작업도 사라짐) 저장 단계에서 행이 없음을 보고 조용히 끝낸다.
- ingester가 죽으면 lease가 만료되고 다음 ingester가 같은 작업을 다시 잡는다(`attempt`는 lease를 잡을 때 증가).

## 5. 엔진 코드 변경

| 위치 | 변경 |
|---|---|
| `engine/llm/base.py`, `ollama.py`, `scripted.py` | `LLMClient`에 `embed(model: str, texts: list[str]) -> list[list[float]]` 추가 |
| `engine/kb/` (신규) | `chunk.py`(청커), `mineru.py`(클라이언트), `rerank.py`(TEI 클라이언트), `store.py`(지식베이스·파일·청크 DB 접근과 벡터 검색) |
| `engine/ingest/` (신규) | `main.py`(진입점), `worker.py`(lease 루프와 처리 단계) |
| `engine/nodes/base.py` | `NodeContext`에 `kb: KnowledgeSearch \| None`, `rerank: Reranker \| None` 포트 추가(`http`·`secrets`와 같은 방식) |
| `engine/nodes/kb_search.py`, `rerank.py` (신규) | 노드 두 개, `registry.py`에 등록 |
| `engine/api/routers/knowledge_bases.py` (신규) | 6절 API |
| `engine/config.py` | `MINERU_BASE_URL`, `MINERU_TIMEOUT_SEC`, `RERANK_BASE_URL`, `KB_MAX_FILE_BYTES`, `INGEST_MAX_JOBS`, `KB_EMBED_MODEL`(기본 `bge-m3`) |
| `engine/db/migrations/versions/0004_knowledge_base.py` | 3절 스키마 |

### 5.1 `kb_search` 노드

- 라벨 `지식 검색`, 분류 `AI`, 부작용 없음, 기본 정책은 LLM 노드와 같은 재시도.
- 설정:
  - `knowledgeBase: str` — 지식베이스 id. JSON Schema에 `x-knowledge-base: true` 표시.
  - `query: str` — 템플릿(`string`), 비어 있으면 안 됨.
  - `topK: int` — 1~20, 기본 5.
  - `minScore: float | None` — 0~1.
- 실행: 지식베이스를 읽어 모델을 얻고 → `ctx.llm.embed(model, [query])` → 코사인 유사도(`1 - (embedding <=> q)`) 내림차순 top-k,
  `minScore` 미만 제외. 벡터는 노드 밖으로 나가지 않는다.
- 출력 스키마:
  ```json
  {"hits": [{"text": "…", "score": 0.83, "heading": "…", "file": "report.pdf"}], "context": "…"}
  ```
  `context`는 hits의 `text`를 `\n\n---\n\n`으로 이어 붙인 문자열(없으면 `""`).
- 오류: 지식베이스 없음 → 재시도 불가 `지식베이스를 찾을 수 없습니다`. 청크 0개 → 오류 아님, 빈 결과.
  검증기는 DB를 보지 않으므로 존재 확인은 실행 시점에만 한다.

### 5.2 `rerank` 노드

- 라벨 `리랭킹`, 분류 `AI`, 부작용 없음, 재시도 정책은 `kb_search`와 같음.
- 설정:
  - `query: str` — 템플릿(`string`).
  - `hits: str` — 템플릿(대상 `array`), 보통 `{{kb_search_1.hits}}`.
  - `topN: int` — 1~20, 기본 3.
  - `minScore: float | None` — 0~1.
- 실행: hits가 `text` 문자열을 가진 객체의 배열(최대 100개)인지 확인 → 비었으면 TEI를 부르지 않고 빈 결과 →
  `POST {RERANK_BASE_URL}/rerank` `{"query": q, "texts": [...]}` → 돌려받은 `[{index, score}]`로 원래 hit를 재배열,
  `score`를 리랭크 점수로 바꾸고 `minScore` 미만 제외, 상위 `topN`.
- 출력: `kb_search`와 같은 `{hits, context}` 모양.
- 오류: `RERANK_BASE_URL` 미설정 → 재시도 불가 `리랭커가 설정되지 않았습니다`. hits 형식 오류 → 재시도 불가 템플릿 오류.
  연결 실패·5xx → 재시도 가능.

## 6. API (엔진, Bearer 토큰, 브라우저는 BFF 프록시 경유)

| 메서드 · 경로 | 동작 |
|---|---|
| `GET /knowledge-bases` | 목록(id, name, embedModel, 파일 수, createdAt) |
| `POST /knowledge-bases` `{name}` | 생성. 모델은 `KB_EMBED_MODEL`, dim 1024. 이름 중복 409 |
| `DELETE /knowledge-bases/{id}` | 삭제(파일·청크 cascade) |
| `GET /knowledge-bases/{id}/files` | 파일 목록(id, filename, size, status, error, updatedAt) — `content` 제외 |
| `PUT /knowledge-bases/{id}/files?name=<filename>` | 업로드. 본문은 파일 바이트 그대로, `Content-Type` 동봉. 이 경로만 `max_body_bytes` 대신 `KB_MAX_FILE_BYTES` 적용(초과 413). `kb_files` + `ingest_jobs` 생성, 202 |
| `DELETE /knowledge-bases/{id}/files/{fileId}` | 삭제(청크·작업 cascade) |

multipart를 쓰지 않으므로 `python-multipart` 의존성이 필요 없다. BFF 프록시(`apps/web/app/api/engine/[...path]/route.ts`)가
50 MB 본문을 그대로 전달하는지는 구현 계획에서 확인하고, 막히면 그 경로만 스트리밍 전달로 고친다.

## 7. 웹

- **`/knowledge-bases` 화면**(기존 `/secrets` 화면 패턴): 지식베이스 목록·만들기·삭제. 지식베이스를 열면 파일 표
  (이름, 크기, 상태, 실패 사유)와 업로드(`<input type="file" multiple>`)·삭제. `pending`/`processing` 파일이 있는 동안만
  5초마다 목록을 다시 읽는다. 셸 내비게이션에 링크 추가.
- **노드 패널**: 스키마 속성에 `x-knowledge-base: true`가 있으면 `/knowledge-bases` 목록으로 채운 드롭다운을 그린다
  (`lib/panel/uiSchema.ts`의 `x-template` 처리와 같은 자리).
- 브라우저 쪽 엔진 클라이언트는 `lib/engine/knowledgeBases.ts` 하나로, 에러 메시지는 `lib/engine/envelope.ts`를 쓴다.

## 8. 테스트

| 대상 | 방법 |
|---|---|
| 청커 | 순수 함수 단위 테스트: 제목 경계, 긴 구간 분할·겹침, 빈 청크 제거, `heading` |
| MinerU·TEI·Ollama `embed` 클라이언트 | httpx MockTransport: 성공, 4xx 즉시 실패, 5xx·타임아웃 재시도 분류 |
| 적재 작업 | testcontainers Postgres(pgvector 이미지) + scripted embed: `.md` 업로드 → `ready`·청크 수, 재시도 스케줄, 최종 실패 사유, lease 만료 후 재획득, 처리 중 삭제 |
| `kb_search` | 실제 pgvector에 알려진 벡터를 넣고 top-k 순서·`minScore`·빈 지식베이스·없는 지식베이스 |
| `rerank` | MockTransport: 재배열·`topN`·`minScore`·빈 hits·잘못된 hits·미설정 |
| API | 생성·중복 409·업로드 202·413·목록에 `content` 없음·삭제 cascade |
| 웹 | vitest: 지식베이스 화면(목록, 업로드, 상태 폴링 시작·중지), 드롭다운 위젯 |
| 스택 e2e | `.md` 업로드 → `ready` → `시작 → 지식 검색 → 끝` 워크플로 실행 결과에 해당 청크. MinerU·TEI 없이 돈다 |

## 9. 범위 밖

- 파일 재색인 버튼(실패한 파일은 지우고 다시 올린다), 진행률 퍼센트
- 지식베이스별 권한, 멀티테넌시
- 하이브리드 검색(키워드 + 벡터), 이미지·표 별도 임베딩
- 1024 이외 차원의 임베딩 모델
- MinerU·TEI 서버 자체의 배포 자동화
