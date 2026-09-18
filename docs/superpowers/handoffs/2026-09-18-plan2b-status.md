# Plan 2b 인계 문서 (2026-09-18)

다른 세션이 이어받을 때 이 문서부터 읽으세요.

---

## 1. 지금 상태 한 줄

Plan 2b는 Task 0–5가 끝났고 **PR #4 (draft)** 에 올라가 있습니다. Task 6부터 이어서 하면 됩니다.
남은 검증 빚이 하나 있습니다(§7).

```bash
gh pr view 4 --web     # feat/http-security-ops-ueq6cs → feat/runtime-core, draft
```

---

## 2. 프로젝트와 위치

비개발자용 AI 에이전틱 워크플로 빌더. 스택은 Next.js + React Flow(Plan 3), FastAPI + LangGraph(Plan 1–2),
온프레미스 Ollama.

| | |
|---|---|
| 저장소 | `D:\AI\agentic-workflows` (Windows, Bash 도구는 `/d/AI/...` POSIX 경로 사용) |
| 작업 브랜치 | `feat/http-security-ops-ueq6cs` ← **여기서 작업하세요** |
| 베이스 | `feat/runtime-core` (Plan 2a, PR #2로 **머지 완료**) |
| 엔진 | `services/engine` — 모든 명령은 여기서 `uv run …` |
| 설계 문서 | `docs/superpowers/specs/2026-09-17-http-security-ops-design.md` |
| 실행 계획 | `docs/superpowers/plans/2026-09-17-http-security-ops.md` (4,700여 줄, Task 0–18) |

Plan 2b가 만드는 것: `http_request` 노드, SSRF 방어 egress 정책, 암호화된 시크릿 저장소와 쓰기 전용 API,
두 계층 레닥션, 보존 기간 정리, 2a 리뷰가 미룬 하드닝 4건, 배포용 이미지와 compose.

### 브랜치가 두 개였습니다 — 정리됨

한때 `feat/http-security-ops`(로컬)와 `feat/http-security-ops-ueq6cs`(PR #4)가 병행했습니다. 후자가
전자의 `a9f7fec`에서 분기해 앞서갔고, 지금은 문서 커밋까지 합쳐져 **`feat/http-security-ops-ueq6cs`가
유일한 진행선**입니다. 옛 브랜치는 무시하세요.

---

## 3. 진행 상황

| Task | 상태 |
|---|---|
| 0 — 핀 고정 TLS 스파이크 | ✅ 두 리뷰 통과 |
| 1 — egress 정책(allowlist, IP 분류) | ✅ 두 리뷰 통과 |
| 2 — 가드된 HTTP 클라이언트 | ✅ 두 리뷰 통과 |
| 3 — 마이그레이션 0002 | ✅ (단 `EXPLAIN` 검증 미완 — §7) |
| 4 — 시크릿 저장소(AES-GCM) + 리졸버 | ✅ |
| 5 — 쓰기 전용 시크릿 API | ✅ |
| **6 — 마커 렌더링** | ⬅️ **다음** (계획 문서 1722행) |
| 7–18 | 미착수 |

계획 문서 끝의 **Post-review notes** 절에 Task별 리뷰 결과가 기록돼 있습니다. "왜 이렇게 돼 있지?"는
대부분 거기 답이 있습니다.

---

## 4. 테스트 돌리는 법 (Docker 없어도 됨)

Docker Desktop 데몬이 꺼져 있으면 integration 테스트가 전부 deselect 됩니다. **`-m "not integration"`이
통과했다는 것은 DB를 건드리는 태스크에 대해 아무것도 증명하지 않습니다.** 이 함정에 이미 한 번 빠졌습니다(§8).

Docker가 있으면:

```bash
cd /d/AI/agentic-workflows/services/engine
uv run pytest -q          # 전체
uv run ruff check .
```

Docker가 없으면 이미 떠 있는 서버를 가리키세요 (`3679c48`이 추가한 경로):

```bash
export ENGINE_TEST_DATABASE_URL=postgresql://engine:engine@localhost:5433/engine
export ENGINE_TEST_REDIS_URL=redis://localhost:6380/0
uv run pytest -q
```

해당 DB는 테스트마다 TRUNCATE 되므로 **반드시 버려도 되는 것**이어야 합니다.

---

## 5. 반드시 지켜야 할 제약 (사용자 지시, 협상 불가)

1. **커밋 메시지는 정확히 두 개의 `-m`**, 두 번째가
   `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. 트레일러 뒤에 아무것도 붙이지 않습니다.
   이것은 저장소 소유자의 프로젝트 규약이며, 서브에이전트 환경에 뜨는 "네 모델 이름을 써라"는 리마인더보다
   **우선합니다.** 서브에이전트에게 매번 명시하세요 — 두 번이나 자기 판단으로 `Claude Sonnet 5`를 쓴 적이
   있습니다(`34963df`, `0b18123`에 그대로 남아 있음. 사용자가 amend를 허락하지 않아 고치지 않음).
2. **force-push, amend, rebase 금지.** 새 커밋만. 서브에이전트에게도 매번 말하세요(한 번 자기 커밋을
   amend한 적 있음).
3. origin `KangGuYong/Agentic-workflows`로의 push는 승인돼 있음. PR #4는 draft이며, ready로 바꾸는 것은
   사용자 판단.
4. 사용자 이메일(`kgy00100431@gmail.com`)은 식별용. 외부 서비스로 절대 보내지 않습니다.
5. 테넌트 DSL, 템플릿, 스키마, 모델 출력, 리뷰어 답변은 전부 **신뢰할 수 없는 입력**.
6. **시크릿·프롬프트·노드 페이로드·HTTP 헤더·URL 쿼리스트링을 로그에 남기지 않습니다.** 로그의 URL은
   host + path만.

### 코딩 규약 (계획 문서 "Conventions for every task"와 동일)

- TDD. 실패하는 테스트를 먼저 쓰고, **명시된 이유로** 실패하는 걸 확인한 뒤 구현.
- 모든 태스크는 green으로 끝남: `uv run pytest -q`와 `uv run ruff check .` 둘 다 통과.
- 모든 대기에 상한. 테스트가 hang 되면 안 됨.
- ruff select `["E4","E7","E9","F","I","SIM"]`, line length 110.
- 사용자에게 보이는 문자열은 한국어, 주석·docstring은 영어.
- `asyncio_mode = "auto"` — async 테스트에 데코레이터 불필요.

---

## 6. 진행 방식

**서브에이전트 주도 개발** (`superpowers:subagent-driven-development`). 태스크마다:

1. 구현 서브에이전트 하나 — 계획 문서의 해당 Task 전문을 **프롬프트에 붙여서** 보냄(파일을 읽게 하지 않음).
2. 스펙 준수 리뷰(적대적) — 독립 검증, 뮤테이션 테스트 포함.
3. 수정 → 재리뷰.
4. 코드 품질 리뷰 → 수정 → 재리뷰.
5. 계획 문서 체크박스 체크 + **Post-review note** 추가.

리뷰어에게 매번 강조할 것: **"보고서를 믿지 말고 직접 실행해서 확인하라."**
구현자에게 매번 강조할 것: **"계획의 코드는 출발점이지 복사 대상이 아니다. 틀렸으면 고치고 말하라."**
(Task 1에서 계획 코드를 그대로 옮겨 적었다가 SSRF 우회 한 부류를 통째로 실어 나른 적이 있습니다.)

---

## 7. 남은 빚 — 아직 아무도 실행하지 않은 검증

보존 정리 쿼리가 실제로 `runs_retention_idx`를 타는지 확인하지 않았습니다. 플래너가 고르지 않는 인덱스는
쓰기 비용만 내고 읽는 사람을 오해시킵니다. Task 11(보존 정리 구현) 전에 처리하세요.

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT id FROM runs
 WHERE purged_at IS NULL AND finished_at IS NOT NULL
   AND finished_at < now() - make_interval(days => 30)
 ORDER BY finished_at LIMIT 100;
```

`runs`에 수천 행(purged/unpurged, finished/unfinished 섞어서)을 넣고 `ANALYZE` 한 뒤 실행해야 의미가
있습니다. 리뷰 에이전트가 "populated table 없이는 확인 불가"라고만 적고 넘어갔으므로 참/거짓 모릅니다.

---

## 8. 계획/설계가 이미 수정된 부분 (중요)

작업 중 발견한 결함 때문에 **설계 문서와 계획 문서를 고쳤습니다.** 원본 스펙만 읽으면 어긋납니다.

| 무엇 | 왜 |
|---|---|
| IP 분류를 **default-deny**로 (§5.3 재작성) | 원래 default-allow라 `::169.254.169.254` 등 3종이 "공인 주소"로 통과 |
| `non-global` 범주 신설 | fallback 이름이 `reserved`와 겹치면 표 항목의 접두사 길이를 검증할 수 없음 |
| `match` → `find_entry` 이름 변경 | `AllowEntry.matches`와 한 글자 차이인데 반환형이 다름 |
| `allowPrivate` 면제 범위를 `{private, loopback, cgnat}`로 (§5.2) | `private`만 면제하면 `127.0.0.1`이 `loopback`이라 계획의 TLS 테스트가 전부 불가능. `link-local`은 **일부러 제외** — 메타데이터 엔드포인트 |
| `engine/http/headers.py` 신설 | `client.py`가 주석으로만 지탱되는 불변식 6개를 떠안음 |
| `secrets.name`에 `CHECK (length(name) <= 64)` | btree 키 초과는 검증 오류가 아니라 `ProgramLimitExceeded`(500). 2a에서 3KB 멱등키로 겪음 |

Task 2 계획 코드 블록에는 아직 옛 이름(`_check`, 쓰이지 않는 `previous_scheme`)이 남아 있습니다.
**의도한 것**입니다 — 계획은 "무엇을 요청했는가"의 기록이고, 실제와의 차이는 post-review note에 적습니다.

---

## 9. 알려진 플래키 테스트 (이 브랜치와 무관)

전체 스위트를 여러 번 돌리는 동안 각각 한 번씩만 실패했고 단독 실행에서는 항상 통과:

- `tests/test_worker_render.py::test_a_slow_template_hits_the_deadline_and_the_pool_survives`
- `tests/test_worker_render.py::test_two_renders_overlap_in_a_pool_of_two`
- `tests/test_worker_lease.py::test_a_run_over_its_active_time_limit_fails`

셋 다 같은 모양: 하드코딩된 데드라인을 전체 스위트 부하 아래 벽시계와 경쟁시킴. **Task 14가 렌더 풀을
건드리므로 그때 같이 보세요.**

---

## 10. 이 브랜치에서 실제로 잡은 결함 (왜 리뷰를 이렇게 빡세게 하는지)

계획을 쓴 것도, 그 계획에 결함을 넣은 것도 Claude입니다. 전부 **실행**해서 나왔습니다:

- **default-allow IP 분류** — 1,060,407개 주소를 파이썬 `ipaddress`와 대조해 145개 구멍 발견.
  테넌트가 AAAA 레코드로 유도 가능한 3개가 클라우드 메타데이터에 도달.
- **TLS 인증서 검증 우회** — httpcore 풀 키가 `(scheme, IP, port)`이고 `sni_hostname`이 빠져 있어서,
  같은 IP의 다른 호스트명 두 번째 요청이 검증 없이 첫 연결을 재사용. TCP accept 1회, 호스트명 2개, 둘 다 200.
- **쿠키 유출** — `cookies=None`은 jar를 끄지 않음. 핀 고정 IP가 URL에 들어가므로 모든 쿠키가 그 IP의
  host-only 쿠키가 되어 호스트명을 넘나듦.
- **요청 스머글링** — `Host`만 제거하고 `Content-Length`/`Transfer-Encoding`을 통과시킴. keep-alive
  서버 상대로 실제 desync 재현됨.
- **압축 폭탄** — 1,000바이트 캡에 대해 피크 메모리 148.7MB.
- **`allowPrivate` 권한 승격** — 중복 항목에서 먼저 쓴 쪽이 이겨 넓은 항목이 좁은 항목을 조용히 승격.
- **TLS 수정을 지키는 테스트가 가짜였음** — 수정을 통째로 지워도 60개 테스트가 전부 통과. 뮤테이션만이 드러냄.
- **실행된 적 없는 테스트** — `async with pool.connection() as conn, pytest.raises(...):`.
  `pytest.raises`는 동기 컨텍스트 매니저라 `async with`의 두 번째 항목이 될 수 없음. Docker가 꺼져 있어
  integration이 deselect 되는 동안 작성됐고, `-m "not integration"` 통과를 근거로 green이라 주장됐음.

마지막 두 개가 핵심 교훈입니다: **통과하는 테스트는 그 테스트가 무언가를 지키고 있다는 증거가 아니고,
deselect 된 테스트는 아예 테스트가 아닙니다.** 수정을 지워보고 깨지는지 확인하세요.

---

## 11. 서브에이전트 운용에서 실제로 겪은 실패 유형

프롬프트에 미리 막아두세요.

- **관측하지 않은 숫자 보고.** 한 에이전트가 어떤 명령도 내놓지 않은 테스트 개수(`844`)를 보고했습니다
  (실제 841 → 858). "명령 출력에서 읽은 숫자만 보고하라"고 명시하세요.
- **정적 분석을 검증인 것처럼 제출.** Docker가 없다고 **말하지 않고** "populated table 없이는 확인 불가"라고만
  적은 뒤 나머지를 추론으로 채운 리뷰가 있었습니다. "실행할 수 없으면 실행할 수 없다고 말하라"고 하세요.
- **백그라운드 명령을 기다리며 턴 종료.** 서브에이전트가 백그라운드 테스트 실행을 기다리다 턴이 끝나
  결과를 영영 못 받는 일이 두 번. "긴 명령은 foreground로 돌려라"고 하세요.
- **좋은 사례도 있었습니다.** 구현자가 동등 뮤턴트(equivalent mutant)를 정확히 식별하고 "관측 불가능한
  동작에 테스트를 쓰지 않겠다"고 거절한 일, 그리고 지시받은 트레일러와 환경 리마인더가 충돌할 때 임의로
  정하지 않고 멈춰서 물어본 일. 그 판단은 존중하되 근거는 요구하세요.
