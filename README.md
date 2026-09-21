# Agentic Workflow Builder

온프레미스에서 도는 워크플로 빌더. 브라우저에서 노드를 그려 워크플로를 만들고, 그 자리에서 돌리고,
어디까지 갔는지 지켜본다. LLM은 사내 GPU 호스트의 Ollama를 쓴다 — 어떤 데이터도 바깥으로 나가지 않는다.

설계: `docs/superpowers/specs/2026-09-11-agentic-workflow-builder-mvp-design.md`
계획과 진행: `docs/superpowers/plans/2026-09-11-roadmap.md`

## 세 덩어리

| 위치 | 무엇인가 |
|---|---|
| `services/engine` | 엔진. DSL·검증·컴파일·실행(LangGraph), FastAPI API와 워커. Postgres에 상태를, Redis에 이벤트와 제어를 둔다. |
| `apps/web` | 편집기. Next.js + React Flow 캔버스, 스키마 기반 폼, 검증 표시, 실행과 추적 화면. 엔진 토큰을 쥔 BFF 프록시도 여기 있다. |
| `deploy` | compose 스택. 다섯 서비스: postgres, redis, api, worker, web. **Ollama는 포함하지 않는다** — 이미 GPU 호스트에서 돌고 있고 `OLLAMA_BASE_URL`이 그곳을 가리킨다. |

브라우저는 엔진을 직접 부르지 않는다. 엔진은 모든 경로에 Bearer 토큰을 요구하는데 `EventSource`는 헤더를
붙일 수 없어서, SSE를 브라우저에서 직접 구독하는 것이 원리적으로 불가능하다. 그래서 `/api/engine/*`
프록시가 토큰을 붙여 대신 부르고, **토큰은 web 컨테이너 밖으로 나가지 않는다.**

## 띄우기

```bash
cd deploy
cp .env.example .env          # 그리고 채운다. .env는 git-ignore된다

# 두 개의 키 (각각 32자 무작위 ASCII, 16·24자도 된다)
python -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(32)))"
# API 토큰 (16자 이상)
python -c "import secrets; print(secrets.token_urlsafe(32))"

docker compose up -d --build
docker compose ps             # postgres·redis·api·web healthy, worker running
```

편집기는 `http://localhost:3000`(`.env`의 `WEB_PORT`)에 뜬다.

`.env`에서 잘못 두면 아픈 것들은 `services/engine/README.md`의 표에 있다. 요약하면: 두 키는 **돌리면
예전 데이터를 못 읽는다**(재암호화 경로가 이 릴리스에는 없다), `HTTP_ALLOWLIST`는 **비어 있으면 모든
`http_request`를 막는다**(일부러 그렇다).

## 처음 한 바퀴

1. **시크릿 하나 넣기** — 편집기의 `시크릿` 화면에서 이름(`A-Z0-9_`, 대문자로 시작)과 값을 저장한다.
   값은 저장한 뒤 **API로도 다시 읽을 수 없다**. `http_request` 노드의 URL·헤더·본문에서
   `{{ secret.NAME }}`으로 참조한다. 이 한 바퀴에 시크릿이 꼭 필요하지는 않으니 건너뛰어도 된다.
2. **워크플로 만들기** — 목록에서 `새 워크플로`. 시작과 끝 노드만 있는 캔버스가 열린다.
   이미 있는 워크플로 파일이 있다면 `가져오기`로 `.json`을 고르면 된다 — `examples/`에 바로 써 볼
   수 있는 여섯 개가 있다.
3. **노드 놓고 잇기** — 왼쪽 팔레트에서 `템플릿`을 캔버스로 끌어다 놓고, 노드 오른쪽 손잡이에서
   다음 노드 왼쪽 손잡이로 끈다. 편집은 자동 저장된다(오른쪽 위에 `저장됨`).
4. **시작 입력 정하기** — 시작 노드를 눌러 설정 탭에서 `+ 필드 추가`로 입력 필드를 만든다.
   템플릿에서 `{{ `를 치면 그 필드가 자동완성에 뜬다.
5. **돌리기** — `실행`. 검증에 오류가 있으면 버튼이 눌리지 않고, 무엇이 몇 건인지 말해 준다.
   실행 중에는 노드에 상태가 켜지고, `사람 승인` 노드를 지나면 대화상자가 떠서 답을 기다린다.
   새로고침해도 그 질문은 그대로 있다 — 실행은 탭이 아니라 엔진에 있다.
6. **내보내기** — 편집기 툴바나 목록의 `내보내기`로 워크플로를 `.json`으로 내려받는다. 나온 파일은
   `examples/`의 문서들과 같은 모양이라 그대로 다시 가져올 수 있다.
7. **결과 보기** — 노드를 눌러 `실행 기록` 탭. 시도별 입력과 출력이 있고, 시크릿이 닿은 값은
   `[REDACTED]`로 나온다.

## 검사

```bash
# 엔진 (Postgres와 Redis 컨테이너가 필요하다)
cd services/engine && uv run pytest -q && uv run ruff check .

# 편집기
cd apps/web && pnpm test && pnpm typecheck && pnpm lint && pnpm build
pnpm test:bundle              # 엔진 토큰이 클라이언트 번들에 없는지 확인한다

# 브라우저 (compose 스택이 떠 있어야 한다)
pnpm e2e
```

## 아직 없는 것

멀티테넌시, 사용자 계정, AI 코파일럿, RAG 지식베이스는 이 릴리스의 범위 밖이다.
