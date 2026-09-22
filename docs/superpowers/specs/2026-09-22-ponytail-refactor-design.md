# Ponytail 리팩터링 설계

날짜: 2026-09-22 · 범위: `apps/web`, `services/engine`

## 목표

ponytail 원칙(삭제 우선, 호출처 하나짜리 추상화 제거, 최소 diff)으로 확인된 중복과 죽은 의존성을 걷어낸다.
**동작 변경은 0.** 기존 테스트가 그대로 통과해야 하며, 새 기능·새 추상화는 넣지 않는다.

## 근거

`/ponytail-audit`으로 저장소 전체를 감사했고, 각 항목을 코드와 grep으로 직접 확인했다. 두 서브시스템 모두
이미 군살이 적다. engine의 큰 모듈(`jsondata`, `api/routers/runs`, `worker/worker`, `http/client`)은
보안·정확성을 해치지 않고 뺄 로직이 없었다.

## 변경 (PR 1개, 커밋 3개)

### 커밋 1 — web 엔진 클라이언트의 에러 envelope 읽기 통합

현재 `apps/web/lib/engine/{resume,run,save,secrets,workflows}.ts` 5개 파일이 같은 `messageOf`를 각자
복사해 두었고, 4곳이 `Envelope`/`ErrorEnvelope` 인터페이스를 따로 선언한다. `run.ts`의 `detailsOf`와
`save.ts`의 `conflictOf` 앞부분도 같은 details 추출을 반복한다.

- 새 파일 `apps/web/lib/engine/envelope.ts`: `messageOf(body, fallback): string`,
  `detailsOf(body): Record<string, unknown> | null`. 구현은 `run.ts`의 것을 그대로 옮긴다.
- 5개 파일에서 로컬 `Envelope`/`ErrorEnvelope`, `messageOf`를 지우고 import한다.
- `run.ts`의 로컬 `detailsOf`를 지우고 import한다. `save.ts`의 `conflictOf`는 `detailsOf`를 쓰게 한다.
- `response.json().catch(() => null)`은 그대로 둔다. 이미 한 줄이라 헬퍼로 바꿔도 줄지 않는다.

### 커밋 2 — `apps/web/components/canvas/Canvas.tsx` 정리

- **선택 병합 중복:** `onNodesChange`와 `onEdgesChange`의 Set 토글 루프를 파일 안의 순수 함수
  `toggled(ids: readonly string[], changes): string[]`로 합친다. `select` 변경이 없으면 두 핸들러 모두
  `state.select`를 호출하지 않는다(현재 동작 그대로).
- **호출처 하나짜리 래퍼:** `useSaveStore`, `useValidationStore`, `useRunStore`를 `Editor` 안의
  `useState(() => create…(...))`로 인라인한다. `workflowId === undefined`일 때의 분기(fixture가 의존)와
  설명 주석은 유지한다.
- **URL 파라미터:** `clearRunFromUrl`과 `useRunInUrl`을 `setRunParam(runId: string | null)` 하나로 합친다.
  값이 이미 같으면 `replaceState`를 부르지 않고, `pushState`가 아닌 `replaceState`만 쓴다. 호출은 기존 두
  effect가 그대로 한다.
- **중복 계산:** `inputSchema(state.dsl)`는 한 번만 계산하고, null 검사 뒤에 붙어 실행될 일이 없는 `?? {}`를 지운다.

### 커밋 3 — engine의 미사용 `jsonschema` 의존성 제거

`services/engine/pyproject.toml`의 `jsonschema>=4.26,<5`를 `engine/`과 `tests/` 어디서도 import하지 않는다.
이 줄을 지우고 `uv lock`으로 lock 파일을 갱신한다. 다른 패키지를 통한 간접 의존으로 남는 것은 괜찮다.

## 범위 밖 (의도적으로 제외)

- 큰 컴포넌트 파일 분리(Canvas, SchemaEditor, NodePanel): 코드가 옮겨질 뿐 줄지 않는다.
- 테스트 삭제: 리팩터링의 안전망이다.
- `engine/db/crypto.py`: 위임만 하는 래퍼가 아니다(JSON 직렬화, 컬럼 AAD, 복호화 실패 시 `None`).
- Canvas의 에러 `<p>` 3개 스타일 통합: role과 위치가 달라 합쳐도 줄지 않는다.
- 타입 가드 모음, `IssueUtils` 같은 새 추상화.

## 검증

| 커밋 | 명령 (`apps/web` 또는 `services/engine`에서) |
|------|------|
| 1 | `pnpm test lib/engine` · `pnpm typecheck` |
| 2 | `pnpm test` · `pnpm typecheck` · `pnpm lint` · `pnpm e2e --project=component` |
| 3 | `uv lock` · `uv run pytest`(DB가 필요한 테스트는 로컬 환경이 허락하는 범위) |

## 성공 기준

- 동작 변경 0. 위 검증이 모두 통과한다.
- 순감소 약 60줄, 런타임 의존성 1개 감소.
