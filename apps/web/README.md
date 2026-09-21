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
pnpm test:bundle  # 엔진 토큰이 클라이언트 번들에 닿지 않는지 확인한다
pnpm e2e        # Playwright. 아래 참고
```

`pnpm e2e`는 프로젝트가 **둘**이다. 필요한 것이 다르기 때문이다.

| 프로젝트 | 무엇을 | 무엇이 필요한가 |
|---|---|---|
| `component` (`e2e/fixture/`) | 컴포넌트 하나를 Vite 페이지에 띄운다. jsdom이 못 하는 것 — IME 조합, dialog 모달성 — 을 본다 | 브라우저만. Playwright가 Vite를 띄운다 |
| `stack` (`e2e/stack/`) | 컨테이너로 뜬 진짜 스택에 대고 편집기를 몬다 | `docker compose up -d`로 **다섯 서비스**가 떠 있어야 한다 |

스택은 Playwright가 띄우지 않는다. 다섯 컨테이너를 테스트 러너가 올리는 것은 배포를 시작하는 일이지
배포를 시험하는 일이 아니다. 한쪽만 돌리려면 `pnpm e2e --project=component`.

스택이 `localhost:3000`이 아닌 곳에 있으면 `E2E_BASE_URL`로 알려 준다. 브라우저가 이미 설치된 환경
(CI 이미지, 샌드박스)에서는 `PLAYWRIGHT_CHROMIUM_PATH`로 그 실행 파일을 가리키면 Playwright가 두 번째
사본을 내려받지 않는다.

## 템플릿 규칙은 엔진의 것이다

노드 패널의 `템플릿 작성 규칙`(`components/panel/TemplateHelp.tsx`)은 Jinja 일반이 아니라 **이 엔진이
허용하는 것**을 적어 둔 것이다. 정본은 `engine/templates/parser.py`와 `engine/templates/env.py`이고,
여기 있는 것은 **사본이라 엔진이 규칙을 바꾸면 낡는다**. 엔진 쪽 요약은 `services/engine/README.md`의
「Templates」 절에 있다.

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

## 컨테이너

`Dockerfile`은 멀티스테이지로 `.next/standalone`만 남기고, uid 10001 비루트로 돈다. `deploy/docker-compose.yml`의
`web` 서비스가 이것을 쓴다. 헬스체크는 `/api/health`이고 **엔진을 건드리지 않는다** — 건드리면 엔진이
죽었을 때 Docker가 멀쩡한 이 프로세스를 재시작한다.

서체는 `next/font/local`로 IBM Plex Sans KR·Mono를 자체 호스팅한다. `next/font/google`은 **빌드 시점에**
구글에서 내려받으므로 온프레미스 빌드에서 깨진다.

## 디자인

미학 방향은 **계기판**이다. 근거와 경계는 `app/globals.css` 머리말과 계획서 「시각 디자인」 절에 있다. 색은 전부 `app/globals.css`의 CSS 변수에서 나오며, 컴포넌트에 hex를 쓰면 `lib/design/status.test.ts`가 잡는다.
