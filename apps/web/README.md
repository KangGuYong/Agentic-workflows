# 워크플로 빌더 — 웹 에디터

Plan 3의 프런트엔드. 엔진 API(`services/engine`) 앞에 서서, 워크플로를 그리고 돌리고 지켜보는 화면이다.

계획: `docs/superpowers/plans/2026-09-18-web-editor.md`
설계: `docs/superpowers/specs/2026-09-18-web-editor-design.md`

## 실행

```bash
pnpm install
pnpm dev        # http://localhost:3000
```

엔진에 붙으려면 두 변수가 필요하다. **둘 다 서버 전용이며 `NEXT_PUBLIC_` 접두사를 붙이지 않는다.**

| 변수 | 예시 | 설명 |
|---|---|---|
| `ENGINE_API_URL` | `http://localhost:8000` | 엔진 API 주소. compose 안에서는 `http://api:8000` |
| `ENGINE_API_TOKEN` | — | 엔진의 `ENGINE_API_TOKEN`과 같은 값 |

브라우저는 엔진을 직접 부르지 않는다. `/api/engine/*`의 BFF 프록시가 토큰을 붙여 대신 부른다. 엔진이 모든 경로에 Bearer 토큰을 요구하는데 `EventSource`는 헤더를 붙일 수 없어서, SSE를 브라우저에서 직접 구독하는 것이 원리적으로 불가능하기 때문이다. 자세한 근거는 설계 §3.2.

## 검사

```bash
pnpm test       # vitest — lib(node) + components(jsdom). Docker 불필요
pnpm typecheck
pnpm lint
pnpm build
pnpm e2e        # Playwright. 실행 중인 compose 스택이 필요하다
```

## 구조

| 경로 | 역할 |
|---|---|
| `app/api/engine/[...path]/` | BFF 프록시 — 토큰을 쓰는 유일한 곳 |
| `lib/engine/` | 엔진 API 타입과 클라이언트 (`server-only`) |
| `lib/dsl/` | DSL 문서 모델, 편집 커맨드, undo/redo — 순수 TS |
| `lib/template/` | `{{ }}` 파서와 자동완성 후보 — 순수 TS |
| `lib/design/` | 색·상태 토큰의 TS 쪽 정본 |
| `store/` | Zustand 슬라이스 |
| `components/` | 캔버스, 패널, 대화상자 |

## 디자인

미학 방향은 **계기판**이다. 근거와 경계는 `app/globals.css` 머리말과 계획서 「시각 디자인」 절에 있다. 색은 전부 `app/globals.css`의 CSS 변수에서 나오며, 컴포넌트에 hex를 쓰면 `lib/design/status.test.ts`가 잡는다.
