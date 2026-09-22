# 워크플로 예시

바로 불러다 돌려 볼 수 있는 워크플로 여섯 개. 뒤로 갈수록 노드가 하나씩 늘어난다.

| 파일 | 다루는 것 | 바깥 의존 |
|---|---|---|
| `01-hello.json` | 시작 입력, 템플릿, 끝 출력 | 없음 |
| `02-threshold.json` | `condition` — 데이터로 갈라지기 | 없음 |
| `03-parallel.json` | 한 출력에서 병렬로 갈라져 `merge`로 모으기 | 없음 |
| `04-approval.json` | `human_approval` — 사람이 답할 때까지 멈추기 | 없음 |
| `05-triage.json` | `classifier`로 분류하고 `llm`으로 초안 쓰기 | Ollama |
| `06-http-notify.json` | `http_request` — 시크릿, JSON 본문, 멱등 키 | 허용목록 + 시크릿 |

## 불러오기

**`가져오기`** 버튼이 두 군데 있고, **하는 일이 다르다.**

**워크플로 목록**(`http://localhost:3000`)의 `가져오기`는 **새 워크플로를 만든다.** 파일 이름이 워크플로
이름이 되고 편집기가 바로 열린다. 노드 위치까지 파일에 들어 있어서 따로 정렬하지 않아도 된다. 이 폴더의
예시를 처음 열어 볼 때 쓰는 쪽이다.

**편집기 툴바**의 `가져오기`는 **보고 있는 워크플로에 파일의 노드를 얹는다.** 팔레트에서 노드를 끌어다
놓는 것과 같은 편집이고, `되돌리기` 한 번으로 파일 전체가 다시 빠진다. 조각으로 만들어 둔 워크플로를
가져다 쓰는 쪽이다.

얹을 때 두 가지가 자동으로 일어나고, 둘 다 툴바 아래에 적힌다.

- **파일의 시작·끝 노드는 오지 않는다.** 엔진이 그 둘의 id를 `start`·`end`로 고정해서 문서마다 하나씩만
  있을 수 있다. 그 노드에 붙어 있던 연결도 함께 빠진다.
- **id가 겹치면 이름을 바꾼다.** `llm_1`이 이미 있으면 들어오는 쪽이 `llm_2`가 되고, **그 노드를 가리키던
  템플릿 참조와 연결이 같이 고쳐진다.** id는 `{{ llm_1.text }}`가 가리키는 그것이라, 이름만 바꾸고 참조를
  두면 문서가 조용히 깨진다.

얹은 노드는 아직 아무 데도 연결돼 있지 않아서 검증 오류가 뜬다 — 손잡이를 이어 주면 사라진다.

내보내기는 두 군데 있다. 목록의 각 행에는 `내보내기`가 있어 **저장된** 초안을 내려받고, 편집기 툴바의
`내보내기`는 **지금 화면에 있는** 문서를 내려받는다. 자동 저장이 아직 안 끝났을 때 둘이 다를 수 있는데,
"이걸 내보내라"가 보통 뜻하는 쪽은 화면에 있는 것이다.

내보낸 파일은 이 폴더의 예시들과 같은 모양이다 — DSL 그 자체이고, 편집기 메타데이터도 워크플로 id도
들어 있지 않다. 그래서 내보낸 파일을 그대로 다시 가져올 수 있고, 손으로 고쳐도 되고, diff도 읽힌다.

<details>
<summary>API로 넣기 (스크립트에서 쓸 때)</summary>

```bash
export TOKEN=...            # deploy/.env의 ENGINE_API_TOKEN
export API=http://localhost:8000
FILE=examples/01-hello.json

ID=$(curl -s -X POST "$API/workflows" -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"name":"hello 예시"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -s -X PUT "$API/workflows/$ID" -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d "$(python3 -c "import json,sys;print(json.dumps({'draftDsl':json.load(open('$FILE')),'revision':1}))")"
```

그다음 `http://localhost:3000/workflows/$ID`를 열면 된다.

</details>

## 읽기 전에 알아 두면 좋은 규칙

예시를 쓰면서 검증기에 걸렸던 것들이다. 전부 엔진이 잡아 주지만, 미리 알면 덜 헤맨다.

**갈라진 뒤의 참조에는 `default`가 필요하다.** `condition`이나 `human_approval`처럼 갈라지는 노드
뒤에서는 한쪽 경로만 실행된다. 그래서 끝 노드가 양쪽을 다 참조하면 `REF_NOT_GUARANTEED` 오류가 난다.

```json
"notice": "{{ manual.text | default('', true) }}{{ auto.text | default('', true) }}"
```

둘째 인자 `true`가 핵심이다 — 이게 없으면 `default`는 **없는 값**만 바꾸고 `null`은 그대로 둔다.

**`merge`는 분기 합류용이 아니다.** 한 노드의 **한 출력**에서 둘 이상으로 갈라진 병렬 분기를 모으는
노드다(`03-parallel.json`). `condition`의 `true`/`false`처럼 서로 배타적인 갈래를 모으려고 쓰면
`MERGE_WITHOUT_FAN_OUT`이 난다. 그런 경우는 각 갈래를 그냥 끝 노드로 이으면 된다
(`02-threshold.json`, `04-approval.json`).

**갈라지는 노드는 모든 출력을 이어야 한다.** `condition`은 `true`·`false`, `human_approval`은
`approve`·`reject`, `classifier`는 카테고리마다 하나씩 **그리고 `default`** 까지. 하나라도 비면
`HANDLE_NOT_CONNECTED` 오류다. 분류가 실패했을 때 갈 곳이 `default`다(`05-triage.json`).

**JSON 템플릿에서는 `{{ }}` 하나가 JSON 값 하나다.** 따옴표로 감싸지도, `| tojson`을 붙이지도 않는다.

```json
"template": "{\"channel\": {{ start.channel }}, \"text\": {{ start.text }}}",
"format": "json"
```

출력 필드 이름도 형식에 따라 다르다 — `format: "text"`면 `.text`, `"json"`이면 `.data`다.

**노드마다 출력 필드가 정해져 있다.** `template` → `.text`/`.data`, `condition` → `.result`,
`classifier` → `.category`, `human_approval` → `.decision`·`.comment`, `http_request` →
`.status`·`.headers`·`.body`, `merge` → `.branches.<노드id>`. 없는 필드를 쓰면 `REF_UNKNOWN_FIELD`가 난다.

## 05, 06을 돌리려면

**`05-triage.json`** 은 `classifier`와 `llm`이 모델을 부른다. `deploy/.env`의 `OLLAMA_BASE_URL`이
실제로 도는 Ollama를 가리켜야 하고, 그 서버에 `qwen2.5:14b`가 받아져 있어야 한다. 다른 모델을 쓰면
두 노드의 `model` 값을 바꾸면 된다.

**`06-http-notify.json`** 은 두 가지가 더 필요하다.

1. **허용목록.** `HTTP_ALLOWLIST`가 비어 있으면 **모든** `http_request`가 막힌다. 일부러 그렇다.
   ```
   HTTP_ALLOWLIST=https://api.example.com
   ```
2. **시크릿.** `{{ secret.NOTIFY_TOKEN }}`이 가리키는 값을 먼저 저장한다.
   ```bash
   curl -X PUT "$API/secrets/NOTIFY_TOKEN" -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' -d '{"value":"..."}'
   ```
   저장한 뒤에는 API로도 다시 읽을 수 없다. 실행 기록에는 `[REDACTED]`로 남는다.

`api.example.com`은 진짜 주소가 아니다. 쓰던 웹훅 주소로 바꿔서 쓰면 된다.

`POST`는 재시도하지 않는다 — 재시도가 부작용을 두 번 일으킬 수 있어서다. 대신
`sendIdempotencyKey: true`로 멱등 키를 보내 받는 쪽이 중복을 걸러낼 수 있게 한다.

**전송이 실패해도 실행은 계속된다.** `notify` 노드의 정책이 `onError: "default"`라서, 호출이 실패하면
`defaultOutput`(`status: 0`)이 그 자리에 들어가고 `sent` 조건이 그걸 보고 수동 확인 경로로 보낸다.
알림 하나 때문에 워크플로 전체가 죽는 것보다 이쪽이 낫다는 판단이다. 실행 기록에는 **두 줄**이 남는다 —
실패한 시도 하나와 기본값으로 끝난 시도 하나. 무엇이 실패했는지가 지워지지 않는다.

## 확인한 것

01~04와 06은 컨테이너로 띄운 실제 스택에서 **끝까지 실행해** 출력을 확인했고, 갈라지는 것(02, 04)은
양쪽 경로를 모두 돌렸다. 06은 호출이 막히는 쪽 — 즉 `onError: "default"`가 기본값을 넣고 수동 확인
경로로 가는 쪽 — 을 확인했다. 성공 경로는 진짜 웹훅 주소가 있어야 한다.

05는 이 환경에 Ollama가 없어 **`/validate` 통과까지만** 확인했다 — 구조·참조·템플릿은 검증됐고,
모델 호출은 확인하지 못했다.
