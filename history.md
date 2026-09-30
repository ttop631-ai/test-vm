# history.md — AI 협업 및 트러블슈팅 기록

> 작성 원칙: 실제로 발생한 건만 기록한다. Before/After는 커밋 해시로 연결해 저장소 히스토리에서 그대로 확인할 수 있게 한다.

---

## 1. 사용 AI 도구

| 도구 | 모델 | 사용 범위 |
|---|---|---|
| Claude (claude.ai 프로젝트) | Claude Opus 5.5 | 요구사항 분석, 설계 문서(`docs/SPEC.md`, `README.md`, `CLAUDE.md`) 초안, 설계 리뷰 |
| Claude Code (CLI) | Claude Opus 5.5 | 단계별(S1~S6a) 코드 생성, 단위 테스트, 컨테이너 기동·장애 주입 검증, 브라우저(Playwright) 검증, 문서 정리 |

엔지니어 역할: 설계 결정과 명세 확정, 생성 코드 리뷰·보정 방향 결정, 장애 주입 검증 결과 확인, 배포.

역할 구분: 아래 사례의 **발견**은 대부분 AI가 완료 기준을 검증하다 보고한 것이고, **해결 방안 선택과 명세 변경 승인**은 엔지니어가 했다. 사례마다 누가 무엇을 했는지 적는다.

---

## 2. 초기 프롬프트 전략

### 2.1 명세 우선 (Spec-first)

코드를 요청하기 전에 `docs/SPEC.md`(무엇을 만들지)와 `CLAUDE.md`(어떻게 만들지, 무엇을 금지할지)를 먼저 확정했다.

이유: 요구사항 원문만 주고 코드를 요청하면 AI는 해피패스 중심, 순차 호출, 타임아웃 미지정, 명령 문자열을 그대로 실행하는 구조로 수렴하는 경향이 있다. 온프레미스 원격 운영에서 중요한 판단(타임아웃 예산, 실패와 미확인의 구분, 오탐 억제 기준)은 AI가 추론하게 두지 않고 명세에 숫자와 규칙으로 고정했다.

구현 중 명세를 바꿔야 할 때는 **SPEC을 먼저 고치는 `docs:` 커밋 → 코드 `fix:` 커밋** 순서를 지켰다 (예: `0657254` → `c95dd55`, `edee759` → `809b909`).

### 2.2 단계 분할과 완료 기준

S1(agent) → S2(poller·상태 판정) → S3(job·이력) → S4(대시보드) → S5(인증·배포) → S6a(상태 전이 이벤트)로 나누고, 각 단계에 **장애 주입으로 확인 가능한 완료 기준**을 붙였다 (`CLAUDE.md §7`). AI의 "구현 완료" 보고가 아니라 이 기준으로 통과 여부를 판단했다.

S6a는 S5 이후 추가한 단계다. "상태·이벤트 이력 DB를 S5와 함께 할지 나중에 할지" AI에게 검토를 먼저 요청했고, 필수 제출물(AWS 데모) 우선 + 설계 결정 필요(무엇을 저장할지, 판정 위치, 쓰기 경합, 화면) 이유로 분리했다. 범위는 엔지니어가 "이벤트만 저장, 판정은 poller에서, 타임라인은 새 탭"으로 좁혔다.

### 2.3 금지 패턴 명시

도메인 경험상 사고로 이어지는 패턴을 `CLAUDE.md §4, §5`에 명시했다. 예: 참조 없는 `create_task`, `TaskGroup` 예외 전파, `ConnectTimeout` 오분류, `shell=True`, 멀티 워커.

### 2.4 자기 점검 보고 요구

코드 생성 후 MUST 항목별 준수 여부를 표로 보고하게 했다. 보고와 실제 코드가 다른 경우도 기록 대상으로 삼았다.

실제로 AI 원본 `158dd47`은 MUST 15(key=value 로그)를 의도했지만 uvicorn 로거를 재설정하면서 `--no-access-log`가 무력화되어 `uvicorn.access` 형식 로그가 섞여 나왔다. 보고 전 검증 단계에서 발견해 `d7f874c`에서 고쳤다 (§3 사례 3).

### 2.5 커밋 규칙

AI 원본을 수정 없이 `ai(Sx):`로 먼저 커밋하고, 엔지니어 보정은 `fix(Sx):`로 분리했다. 아래 사례의 Before/After는 이 두 커밋이다.

### 2.6 실제 사용 프롬프트 (발췌)

S1 요청 (원문):

```text
CLAUDE.md와 docs/SPEC.md를 먼저 읽고, S1 단계만 구현해줘.

범위:
- agent/ 전체 (Dockerfile, requirements.txt, app/main.py, simulator.py, actions.py, chaos.py)
- docker-compose.yml: node-a/b/c만, SPEC §2·§12 기준 (nodenet internal, 호스트 포트 미노출)
- compose.dev.yml: 검증용 override, 127.0.0.1:9001~9003 → 각 노드 9000 바인딩

기준: SPEC §3(agent), §7(액션 카탈로그), §12(컨테이너 설정)
추가 제약: blackhole 대기는 최대 60초 후 종료. 클라이언트가 먼저 끊어도 서버 쪽 코루틴이 남아 누적되기 때문.

완료 기준:
1. 3개 노드 /livez, /health 응답. PROFILE별 초기 상태가 SPEC §3.2 표와 일치
2. 같은 command_id로 POST /commands 2회 → 실행 1회, 두 번째는 저장된 결과 반환
3. 토큰 불일치 → 401, 미등록 action → 400 (실행 안 됨)
4. blackhole 중 /commands는 실행되고 응답만 보류. reset 후 GET /commands/{id}로 결과 조회 가능

작성 후 제출할 것:
- 완료 기준 1~4를 확인하는 curl 명령 목록 (compose.dev.yml 포트 기준)
- CLAUDE.md §4 MUST 중 agent에 해당하는 항목별 준수 여부 표
```

단계 사이 결정 전달 (AI가 올린 확인 요청에 대한 답):

```text
①은 a, ②는 추가, ③은 상한 89로 진행
```

(① S2 검증용 console 최소 기동 범위, ② pytest를 requirements-dev.txt로 추가, ③ disk_pressure 상한 방식)

S6a 요청 (원문):

```text
우선 이벤트만 저장 그리고 판정은 폴로워 수집할 때 하는 걸로 변경하고
화면에 이벤트 타임라인은 새 탭으로 두는 게 좋을 것 같아 S6A로 진행해보자
```

---

## 3. 엔지니어 개입 및 트러블슈팅 사례

### 사례 1. stale 판정이 연속 실패 임계보다 먼저 걸림

| 항목 | 내용 |
|---|---|
| 단계 | S2 |
| 발견 경위 | 장애 주입 테스트. AI가 완료 기준(node-c blackhole → 3회 실패 후 UNREACHABLE)을 검증하면서 상태 전이를 1초 단위로 기록하다 발견·보고 |
| 증상 | node-c가 **실패 2회 시점**에 UNREACHABLE로 바뀜. 사유가 `3회 연속 TIMEOUT`이 아니라 `마지막 성공 15s 전 > 15s` |
| AI 원본 커밋 | `97cad26` |
| 명세 변경 | `0657254` (SPEC §4.2 규칙 2, §5 `STALE_FACTOR` 근거) |
| 보정 커밋 | `c95dd55` |

관찰된 전이 (보정 전):

```
+0s 실패 0 → +5s 실패 1 → +10s 실패 2 → +15s UNREACHABLE ("마지막 성공 15s 전 > 15s") → +18s "3회 연속 TIMEOUT"
```

**Before (AI 생성)**

```python
stale_limit = settings.stale_factor * settings.poll_interval_sec
if state.last_success_at is not None:
    age = (now - state.last_success_at).total_seconds()
    if age > stale_limit:
        reasons.append(f"마지막 성공 {int(age)}s 전 > {_fmt(stale_limit)}s")
```

**After (보정)**

```python
# last_success_at이 아니라 last_attempt_at 기준이다. 노드 장애는 실패 카운트로만 판정한다.
stale_limit = settings.stale_factor * settings.poll_interval_sec
if state.last_attempt_at is not None:
    age = (now - state.last_attempt_at).total_seconds()
    if age > stale_limit:
        reasons.append(f"마지막 수집 시도 {int(age)}s 전 > {_fmt(stale_limit)}s")
```

**판단 근거**

- 원인은 코드가 아니라 **명세의 두 규칙이 서로 간섭**하는 것이었다. AI 원본은 SPEC 문구(`now − last_success_at > STALE_FACTOR × POLL_INTERVAL_SEC`)를 그대로 구현했다. 실패는 read timeout(3s)이 지나야 기록되므로 3번째 실패는 마지막 성공 후 약 18s에 들어오는데, stale 규칙은 15s에 먼저 걸린다.
- stale 규칙의 목적은 "poller 자체 이상 감지"인데 실제로는 노드 장애에 먼저 반응해 `FAIL_THRESHOLD`(오탐 억제 기준)를 무력화하고 있었다.
- AI가 세 가지 안을 제시했고 엔지니어가 (a)를 선택했다.
  - (a) stale 기준을 `last_attempt_at`으로 변경 — **채택**. 목적에 정확히 맞고, poller가 살아 있으면 노드 장애는 실패 카운트로만 판정된다.
  - (b) `STALE_FACTOR`를 4로 상향 — 기각. 숫자를 조정해 간섭을 피할 뿐이고, 타임아웃 예산이 바뀌면 다시 겹친다.
  - (c) 현행 유지 + 명세에 명시 — 기각. 완료 기준("3회 실패 후")과 어긋난다.

**검증**

```text
단위 테스트: 23 passed (회귀 테스트 test_node_failure_below_threshold_is_not_stale 추가)
blackhole 재검증 (30초 관찰):
  node-a/b last_success_at 간격 [5, 5, 5, 5, 5, 5]
  node-c: CRITICAL(실패 0) → CRITICAL(1) → CRITICAL(2) → UNREACHABLE(3, "3회 연속 TIMEOUT")
```

---

### 사례 2. keep-alive 경합으로 인한 간헐 수집 실패 (이벤트 기록으로 드러남)

| 항목 | 내용 |
|---|---|
| 단계 | S1(agent 기동 옵션)·S2(console HTTP 클라이언트)에서 생겼고, S6a 검증 중 발견 |
| 발견 경위 | 장애 주입 테스트. S6a에서 상태 전이 이벤트를 기록하기 시작하자 **blackhole을 걸지 않은 node-a/b**에 `정상 → 경고(수집 실패 1회)` 이벤트가 생겼고, AI가 로그를 추적해 보고 |
| 증상 | node-a·node-b가 같은 순간 6ms 만에 `RemoteProtocolError: Server disconnected without sending a response`로 실패. 약 95회 수집 중 2건 |
| AI 원본 커밋 | `158dd47` (agent uvicorn 기본 keep-alive), `97cad26` (console httpx 기본 keepalive_expiry) |
| 명세 변경 | `edee759` (SPEC §3.1 `AGENT_KEEPALIVE_SEC`, §5 `HTTP_KEEPALIVE_EXPIRY`) |
| 보정 커밋 | `809b909` |

**Before (AI 생성)** — 둘 다 기본값이라 코드에 보이지 않았다.

```dockerfile
# agent/Dockerfile: uvicorn --timeout-keep-alive 기본값 5s
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${AGENT_PORT} --no-access-log"]
```

```python
# console/app/agent_client.py: httpx keepalive_expiry 기본값 5s
limits = httpx.Limits(
    max_connections=settings.poll_concurrency + settings.job_concurrency,
    max_keepalive_connections=settings.poll_concurrency + settings.job_concurrency,
)
```

**After (보정)**

```dockerfile
# keep-alive는 console의 HTTP_KEEPALIVE_EXPIRY(15s)보다 길어야 한다 (SPEC §5, 경합 방지).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${AGENT_PORT} --timeout-keep-alive ${AGENT_KEEPALIVE_SEC} --no-access-log"]
```

```python
limits = httpx.Limits(
    max_connections=settings.poll_concurrency + settings.job_concurrency,
    max_keepalive_connections=settings.poll_concurrency + settings.job_concurrency,
    # agent가 idle 연결을 닫는 시점과 재사용이 겹치지 않도록 서버 keep-alive보다 짧게 둔다.
    keepalive_expiry=settings.http_keepalive_expiry,
)
```

**판단 근거**

- 서버 keep-alive(5s), 클라이언트 idle 만료(5s), 수집 주기(5s)가 모두 같아서, 서버가 idle 연결을 닫는 순간 클라이언트가 그 연결을 재사용해 요청을 보내는 경합이 생겼다.
- 영향은 헬스체크보다 **명령 쪽이 크다**. `RemoteProtocolError`는 SPEC §6에서 "전달됐을 수 있음"(TIMEOUT → UNKNOWN)으로 분류되므로, 실제로는 전송조차 안 된 명령이 결과 미확인으로 남고 운영자가 reconcile을 해야 한다.
- 순서를 고정했다: **수집 주기(5s) < console 연결 만료(15s) < agent keep-alive(30s)**. 연결 재사용은 유지하면서(주기보다 김) 서버가 먼저 닫지 않게 한다.
- 검토했으나 채택하지 않은 대안:
  - 헬스체크 재시도 — MUST 8(재시도 금지) 위반이고 원인을 가린다.
  - keep-alive 비활성화 — 매 요청 연결 수립. 동작은 하지만 원인 대신 증상을 없앤다.
- 교훈: 이 경합은 S2부터 있었지만 "수집 실패 1회 → 다음 주기 회복"이라 화면에서는 거의 보이지 않았다. **상태 전이를 기록하자 처음으로 드러났다.**

**검증**

```text
5분 소크 테스트 (중간에 node-c blackhole 30초):
  보정 전: poll_ok 약 95회 중 RemoteProtocolError 2건, node-a/b 가짜 경고 이벤트 4건
  보정 후: poll_ok 186회, RemoteProtocolError 0건, 이벤트는 node-c의 blackhole 전이 2건뿐
```

---

### 사례 3. 동일 이미지 태그 병렬 빌드 실패 + 로그 형식 혼재

| 항목 | 내용 |
|---|---|
| 단계 | S1 |
| 발견 경위 | 첫 `docker compose up --build` 실패(빌드), 완료 기준 검증 중 로그 확인(로그 형식). 둘 다 AI가 발견 |
| 증상 | ① `failed to solve: image "docker.io/library/nodewatch-agent:local": already exists` ② `logger=uvicorn.access 172.18.0.1:58526 - "GET /commands/... HTTP/1.1" 200` 같은 key=value가 아닌 줄이 섞임 |
| AI 원본 커밋 | `158dd47` |
| 보정 커밋 | `d7f874c` |

**Before (AI 생성)**

```yaml
x-agent: &agent
  build: ./agent
  image: nodewatch-agent:local   # node-a/b/c가 같은 태그로 동시에 export
```

```python
for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    lg = logging.getLogger(name)
    lg.handlers = []
    lg.propagate = True   # --no-access-log로 꺼 둔 access 로그가 root 핸들러로 되살아남
```

**After (보정)**

```yaml
x-agent: &agent
  build: ./agent        # 서비스별 이미지 (레이어 캐시는 공유)
```

```python
for name in ("uvicorn", "uvicorn.error"):
    lg = logging.getLogger(name)
    lg.handlers = []
    lg.propagate = True
# access log는 auth_and_log 미들웨어가 key=value로 남긴다.
logging.getLogger("uvicorn.access").disabled = True
```

**판단 근거**

- 빌드: YAML anchor로 세 서비스가 `build`와 `image`를 함께 공유하면 compose가 같은 태그를 병렬로 export한다. 태그를 빼고 서비스별 이미지로 두었다. 레이어 캐시를 공유하므로 빌드 시간 차이는 없다.
- 로그: MUST 15(한 줄 key=value, `node_id` 포함)를 지키려고 uvicorn 로거를 root로 모으다가 access 로거까지 되살렸다. 요청 로그는 이미 미들웨어가 `event=request node_id=... status=... duration_ms=...`로 남기므로 access 로거는 끈다.

**검증**

```text
docker compose ps → node-a/b/c healthy
docker compose logs | grep -c uvicorn.access → 0
```

---

### 그 밖의 보정 (요약)

| 단계 | 커밋 | 내용 | 발견 |
|---|---|---|---|
| S1 | `6e7fec7` → `5759117` | `disk_pressure` 프로필이 명세대로 누적 증가하면 약 80초 뒤 disk 90%(CRITICAL)에 도달해 시연 시나리오("병원 B = 경고")와 충돌 → 명세에 89 상한 추가 | AI가 구현 중 계산으로 발견해 TODO로 보고, 엔지니어가 상한 89 선택 |
| S4 | `17aae59` → `41a6dd2` | 응답 시간 패널이 1500ms 임계선에 스케일이 묶여 수 ms 값이 바닥에 붙음 → 데이터 기준 자동 스케일. 패널 2×2 배치, 정수 눈금 | AI가 Playwright 스크린샷 검토 중 발견 |
| S4 | `17aae59` → `d10f291` | 캡처 중 `resize`(innerWidth=1)에서 `<rect width=-5>` 생성 → 극단적으로 좁을 때 렌더 생략 | AI가 S5 브라우저 재검증의 JS 콘솔 오류를 계측해 원인 확인 |
| S5 | `41314a7` → `5a59dc0` | 로그인 페이지 도입 후, 로그아웃한 브라우저가 캐시된 `index.html`·`app.js`를 서버 확인 없이 띄우고 API 401 이후에야 로그인으로 이동 → 인증 응답에 `Cache-Control: no-store`. 로그인 후 원래 탭으로 복귀 | AI가 Playwright로 로그아웃 후 딥링크를 검증하다 발견 (응답 목록에 `/`가 서버 302 없이 200으로 찍힘) |

---

## 4. 개입 포인트 체크리스트

구현 중 아래 항목을 리뷰·테스트로 확인하고, 실제로 발생한 건만 §3 사례로 옮긴다. 발생하지 않은 항목은 "확인함 / 해당 없음"으로 표시한다.

| # | 영역 | AI가 흔히 내놓는 형태 | 운영 관점 문제 | 보정 방향 | 확인 방법 | 결과 |
|---|---|---|---|---|---|---|
| 1 | 헬스체크 | `for` 루프 순차 `await`, 타임아웃 미지정 | 한 노드 지연이 전체 사이클 지연 | 노드별 병렬 + connect/read 분리 | blackhole 주입 후 A/B 갱신 간격 | 확인함 — 처음부터 노드별 태스크 + `httpx.Timeout(connect, read)`로 생성. A/B 간격 5s 유지 |
| 2 | 병렬 예외 | `TaskGroup` 또는 `gather` 기본값 | 한 노드 예외가 다른 노드 수집 취소·전체 실패 | 노드 함수가 예외를 흡수하고 결과 객체 반환 | `error_rate=1.0` 주입 | 확인함 — `get_health`·`post_command`가 결과 객체 반환, job은 `gather(return_exceptions=True)`. `error_rate=1.0`은 agent 단독 호출(500, 미실행)로 확인했고, console 수집 경로는 blackhole·노드 중지로 확인 |
| 3 | 예외 분류 | `except httpx.TimeoutException` 하나로 처리 | 미전달(ConnectTimeout)을 결과 미확인으로 오분류 → 재시도 판단 오류 | Connect 계열을 먼저 분기 | agent 중지 후 명령 결과 error_type | 확인함 — node-c 중지 시 `FAILED / CONNECT_ERROR (ConnectTimeout after 2.0s)` |
| 4 | 장애 판정 | 1회 실패 = DOWN | 상태 깜빡임, 경보 피로 | 연속 N회 + 중간 단계 경고 | `error_rate=0.3` 주입 | 확인함 — 연속 3회 기준. 다만 stale 규칙이 먼저 걸리는 명세 간섭 발견 → **사례 1**. `error_rate=0.3` 주입은 미실시 |
| 5 | 타임아웃 의미 | Timeout → FAILED + 자동 재시도 | 데몬 재시작 중복 실행 | UNKNOWN + `command_id` 멱등 + reconcile | blackhole 중 명령 후 agent 실행 횟수 | 확인함 — blackhole 중 명령은 UNKNOWN, reset 후 reconcile로 SUCCESS. 같은 `command_id` 2회 전송 시 `command_start` 로그 1건 |
| 6 | 백그라운드 작업 | 참조 없는 `create_task`, 예외 미처리 | job이 조용히 멈추고 로그도 없음 | 태스크 레지스트리 + done callback 로깅 | 워커 내부 강제 예외 | 확인함(코드) — agent·poller·jobs 모두 모듈/인스턴스 `set` + done callback. 이벤트 기록 실패 시 판정 계속은 단위 테스트로 확인. 워커 강제 예외 주입은 미실시 |
| 7 | 명령 실행 | 명령 문자열 수신 → `subprocess(shell=True)` | 원격 명령 주입 | 액션 화이트리스트 + enum 파라미터 | 미등록 action 요청 → 400 | 확인함 — console·agent 모두 400이고 실행 안 됨(재조회 404). bool을 int로 넣는 우회(`true` → `1`)도 400 |
| 8 | 이벤트 루프 블로킹 | async 함수 안 `time.sleep`, 동기 `sqlite3` | 명령 실행 중 API 전체 정지 | `asyncio.sleep`, aiosqlite | 명령 실행 중 `/api/nodes` 응답 시간 | 확인함 — `time.sleep`·동기 `sqlite3` 없음(aiosqlite). blackhole 중 명령(15s 대기) 동안에도 A/B 수집 5s 간격 유지. 응답 시간 수치 측정은 미실시 |
| 9 | 인증 범위 | 라우터 dependency로 Basic 인증 | StaticFiles mount가 무인증으로 열림 | 미들웨어로 전환 | 인증 없이 `/` 요청 → 401 | 확인함 — 처음부터 ASGI 미들웨어로 생성. `/`, 정적 파일, `/api/*` 전부 401, `/healthz`만 200 |
| 10 | 프론트 주소 | `http://localhost:8000` 하드코딩 | AWS에서 API 호출 실패 | 상대경로 | 공인 IP로 접속 | 확인함(코드) — `/api/...` 상대경로만 사용. 공인 IP 접속은 배포 후 확인 (TBD) |
| 11 | 워커 수 | `uvicorn --workers 4` | poller 중복 실행, 워커별 상태 불일치 | 단일 워커 명시 | 로그의 수집 횟수 | 확인함 — 단일 워커. 노드당 5s에 `poll_ok` 1건 |
| 12 | 컨테이너 헬스체크 | `curl -f http://localhost/...` | slim 이미지에 curl 없음 → 계속 unhealthy | Python urllib 한 줄 | `docker compose ps` | 확인함 — urllib 한 줄, 4개 컨테이너 healthy |
| 13 | 출력 저장 | 반환 로그 전체 저장 | DB 비대, 화면 멈춤 | 64KB 절단 + 플래그 | 대용량 출력 액션 | 확인함 — `truncate_output`이 UTF-8 경계를 지키며 자르고 플래그 기록(함수 단위 확인). 64KB 초과 출력 액션이 없어 end-to-end는 미실시 |
| 14 | 시간 | naive `datetime.now()` | 컨테이너 TZ에 따라 표시 불일치 | UTC 저장, 브라우저 변환 | API 응답 시각 형식 | 확인함 — API·DB·로그 모두 `...Z`, 브라우저에서 로컬 변환 |
| 15 | 폴링 주기 | 사이클 종료 후 `sleep(interval)` | 주기가 조금씩 밀림 (drift) | 사이클 시작 시각 기준 대기 | 10분간 수집 간격 | 확인함 — `next_start += interval` 방식. 5분 소크 테스트에서 186회 정상 수집. 10분 간격 측정은 미실시 |
| 16 | HTTP 연결 재사용 (추가) | 클라이언트·서버 keep-alive 기본값 그대로 | 서버가 닫는 순간 재사용 → 간헐 `RemoteProtocolError`, 명령은 가짜 UNKNOWN | 수집 주기 < 클라이언트 만료 < 서버 keep-alive | 소크 테스트 중 오류 건수 | **발생 → 사례 2** |

---

## 5. 회고

- 명세로 고정한 항목(병렬 수집, 예외 분류 순서, 화이트리스트, 미들웨어 인증, UTC)은 AI 원본에서 한 번도 틀리지 않았다. 체크리스트의 "AI가 흔히 내놓는 형태"는 명세와 금지 목록만으로 대부분 막을 수 있었다.
- 실제 결함은 **명세끼리의 간섭**(사례 1)과 **명세가 말하지 않은 기본값**(사례 2: keep-alive)에서 나왔다. 둘 다 코드 리뷰로는 보이지 않았고, 수치가 붙은 완료 기준과 장애 주입, 상태 전이 기록이 있어서 드러났다.
- "AI의 완료 보고"보다 "측정 가능한 완료 기준"이 유효했다. AI가 매 단계 기준을 스스로 실측(1초 단위 전이 기록, 소크 테스트, Playwright)하고 수치로 보고하게 한 것이 발견의 대부분을 만들었다.
- 다음에 바꿀 점: 타임아웃뿐 아니라 **연결 수명(keep-alive)과 주기의 대소 관계**도 처음부터 명세 §5 예산표에 넣는다. 서로 영향을 주는 규칙(stale vs 연속 실패)은 명세 단계에서 타임라인으로 그려 검증한다.
