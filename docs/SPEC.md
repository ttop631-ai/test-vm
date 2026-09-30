# NodeWatch 기능 명세 (SPEC)

- 문서 상태: **v1.0 구현 기준선**. 동작을 바꿀 때는 이 문서를 먼저 고치고 코드가 따라가게 한다.
- 독자: 구현자(엔지니어, 코드 생성 AI), 리뷰어.
- 규칙 문서: 코드 작성 규칙과 금지 패턴은 `CLAUDE.md`에 있다.

---

## 1. 요구사항 추적표

| ID | 원문 요구사항 | 구현 위치 | 검증 방법 |
|---|---|---|---|
| R1-1a | 독립된 가상 노드(병원 A/B/C) | agent 컨테이너 3개 `node-a/b/c` (동일 이미지 + env) | `docker compose ps` |
| R1-1b | CPU/Memory/Disk 더미 메트릭 | `agent/app/simulator.py` random walk | agent `GET /health` |
| R1-1c | 핵심 데몬 프로세스 상태 | 노드별 데몬 3종, RUNNING/STOPPED/RESTARTING | chaos `stop_daemon` |
| R1-2a | 주기적 헬스체크 수집·갱신 | `console/app/poller.py` | 상태 뷰의 "마지막 수집" 시각 |
| R1-2b | 원격 일괄 명령 API, 비동기 실행, 결과 기록 | `POST /api/jobs` → 202, `jobs.py` 워커, SQLite | 이력 뷰 |
| R1-2c | 노드 지연·타임아웃 시 전체 비블로킹 | §5 타임아웃 예산, 노드별 병렬, §6 예외 분류 | chaos `blackhole` 주입 시 타 노드 갱신 주기 유지 |
| R1-3a | 노드 상태 현황 뷰 (정상/경고/장애) | 대시보드 탭 1 | 화면 |
| R1-3b | 일괄 제어 콘솔 (다중 선택 → 일괄 실행) | 대시보드 탭 2 | 화면 |
| R1-3c | 실행 이력 · 노드별 반환 로그 | 대시보드 탭 3 | 화면 |
| R2 | AWS 데모 URL을 README 최상단에 | `README.md` | 외부 브라우저 접속 |
| R3 | 소스, compose, README, history (커밋 히스토리 유지) | 저장소 루트 | 제출 체크리스트 |

---

## 2. 구성 요소

| 서비스 | 빌드 | 역할 | 포트 | 네트워크 |
|---|---|---|---|---|
| `console` | `./console` | API, 정적 대시보드, poller, job 워커 | 8000 → 호스트 `${CONSOLE_PORT:-8080}` | `public`, `nodenet` |
| `node-a` | `./agent` | 병원 A mock agent, `PROFILE=normal` | 9000 (호스트 미노출) | `nodenet` |
| `node-b` | `./agent` | 병원 B mock agent, `PROFILE=disk_pressure` | 9000 (호스트 미노출) | `nodenet` |
| `node-c` | `./agent` | 병원 C mock agent, `PROFILE=daemon_down` | 9000 (호스트 미노출) | `nodenet` |

- `nodenet`은 `internal: true`. 노드는 외부와 통신할 수 없고 console만 노드에 접근한다 (폐쇄망 모사).
- console은 **uvicorn 단일 워커**로 기동한다. 최신 노드 상태와 백그라운드 태스크 레지스트리가 프로세스 메모리에 있기 때문이다.
- 기동 직후 평가자가 정상/경고/장애를 한 화면에서 볼 수 있도록 노드별 프로필을 다르게 둔다 (A 정상, B 디스크 경고, C 데몬 중지).

### 2.1 노드 레지스트리

`console/nodes.json` (토큰 값은 파일에 넣지 않고 env 이름만 참조):

```json
[
  {"id": "node-a", "name": "병원 A", "url": "http://node-a:9000", "token_env": "AGENT_TOKEN_A"},
  {"id": "node-b", "name": "병원 B", "url": "http://node-b:9000", "token_env": "AGENT_TOKEN_B"},
  {"id": "node-c", "name": "병원 C", "url": "http://node-c:9000", "token_env": "AGENT_TOKEN_C"}
]
```

### 2.2 환경변수 (`.env.example`)

compose 파일에 `${VAR:-default}`로 기본값을 두어 `.env` 없이도 `docker compose up`이 동작해야 한다.

| 변수 | 기본값 | 용도 |
|---|---|---|
| `CONSOLE_PORT` | `8080` | 호스트 노출 포트 (AWS 데모는 `80`) |
| `ADMIN_USER` | `admin` | 관리자 계정 (역할 `admin`: 전체 기능). API는 Basic 헤더로도 사용 |
| `ADMIN_PASSWORD_HASH` | `nodewatch`의 해시 | 관리자 비밀번호 **scrypt 해시** (`scrypt:N:r:p:salt:hash`, base64url). 생성: `python -m app.auth hash`. 평문 `ADMIN_PASSWORD`가 설정돼 있으면 기동 거부 |
| `MONITOR_USER` | `monuser` | 모니터링 계정 (역할 `monitor`: 조회 전용). 빈 값이면 비활성 |
| `MONITOR_PASSWORD_HASH` | `monwatch`의 해시 | 모니터링 계정 비밀번호 scrypt 해시 |
| `SESSION_TTL_SEC` | `28800` | 로그인 세션 유효 시간 (8시간, 절대 만료) |
| `COOKIE_SECURE` | `false` | 세션 쿠키 `Secure` 속성. HTTPS 뒤에 둘 때 `true` |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_SEC` | `5` / `60` | 같은 클라이언트 IP의 연속 로그인 실패 n회 → n초 차단 (Basic 헤더 실패 포함) |
| `AGENT_TOKEN_A/B/C` | `dev-token-a/b/c` | console ↔ agent 인증 |

---

## 3. Mock Agent 명세

### 3.1 설정

| env | 예시 | 설명 |
|---|---|---|
| `NODE_ID` | `node-a` | |
| `NODE_NAME` | `병원 A` | |
| `AGENT_TOKEN` | (비밀) | 요청 헤더 `X-Agent-Token`과 비교 |
| `PROFILE` | `normal` / `disk_pressure` / `daemon_down` | 초기 상태 프로필 |
| `TICK_SEC` | `2` | 메트릭 갱신 주기 |
| `AGENT_KEEPALIVE_SEC` | `30` | HTTP keep-alive 유지 시간 (uvicorn `--timeout-keep-alive`). console의 `HTTP_KEEPALIVE_EXPIRY`보다 길어야 한다 (§5) |

### 3.2 시뮬레이션

- 메트릭은 프로필 기준 범위에서 random walk (tick당 ±3%p), 0~100 clamp.
- 데몬(공통 3종): `pacs-gateway`, `hl7-interface`, `emr-sync`. 각 `status`, `pid`, `uptime_sec` 보유.
- 상태는 agent 프로세스 메모리에만 있다. 컨테이너 재기동 시 프로필 초기값으로 돌아간다.

| PROFILE | CPU % | MEM % | DISK % | 초기 데몬 |
|---|---|---|---|---|
| `normal` | 20~50 | 40~60 | 50~65 | 전부 RUNNING |
| `disk_pressure` | 20~50 | 40~60 | 82~88, tick당 +0.05%p 누적 증가. 기준 범위 상단과 값 모두 **89 상한** (CRITICAL 90 미만 유지) | 전부 RUNNING |
| `daemon_down` | 20~50 | 40~60 | 50~65 | `emr-sync` STOPPED |

### 3.3 API

`/livez`를 제외한 모든 엔드포인트는 `X-Agent-Token` 헤더 필수 (불일치 시 401, `hmac.compare_digest`).

| Method | Path | 설명 |
|---|---|---|
| GET | `/livez` | 컨테이너 헬스체크용. 인증·chaos 미적용 |
| GET | `/health` | 메트릭 + 데몬 상태 |
| POST | `/commands` | 명령 실행 (멱등) |
| GET | `/commands/{command_id}` | 명령 결과 재조회 (reconcile용) |
| GET/POST | `/chaos` | 장애 주입 설정 조회/변경. chaos 자체에는 chaos 미적용 |

**GET /health → 200**

```json
{
  "node_id": "node-c",
  "node_name": "병원 C",
  "agent_time": "2026-10-01T03:00:00Z",
  "metrics": {"cpu_pct": 34.2, "mem_pct": 51.0, "disk_pct": 60.3},
  "daemons": [
    {"name": "pacs-gateway",  "status": "RUNNING", "pid": 1234, "uptime_sec": 86400},
    {"name": "hl7-interface", "status": "RUNNING", "pid": 1240, "uptime_sec": 86390},
    {"name": "emr-sync",      "status": "STOPPED", "pid": null, "uptime_sec": 0}
  ]
}
```

**POST /commands**

요청:

```json
{"command_id": "7b1e…", "action": "RESTART_DAEMON", "params": {"daemon": "emr-sync"}}
```

응답 200:

```json
{
  "command_id": "7b1e…",
  "state": "DONE",
  "exit_code": 0,
  "output": "stopping emr-sync ... ok\nstarting emr-sync ... ok (pid 2345)",
  "started_at": "2026-10-01T03:00:01Z",
  "finished_at": "2026-10-01T03:00:05Z"
}
```

- 알 수 없는 `action` 또는 허용되지 않은 `params` → 400, **실행하지 않는다**.
- 같은 `command_id` 재수신 시 재실행 금지. 완료 건은 저장된 결과를 그대로 반환, 실행 중이면 `{"command_id": "…", "state": "RUNNING"}` 200 반환.
- 결과 캐시는 최근 500건 (메모리). agent 재기동 시 유실된다.

**GET /commands/{command_id}** → 저장 결과 200, 기록 없으면 404.

**POST /chaos**

```json
{"latency_ms": 0, "error_rate": 0.0, "blackhole": false, "stop_daemon": null}
```

`{"reset": true}`는 전체 초기화 (데몬 상태는 유지).

| 필드 | 효과 | 시연 목적 |
|---|---|---|
| `latency_ms` | `/health`, `/commands` 응답 전 지연 | 지연 경고, read timeout |
| `error_rate` | 해당 확률로 500 반환. `/commands`는 **실행 전에** 반환 | 간헐 오류에서 오탐 억제 |
| `blackhole` | `/health`는 응답 보류. `/commands`는 **실행은 하고 응답만 보류** | read timeout → UNKNOWN → reconcile로 실제 성공 확인 |
| `stop_daemon` | 지정 데몬을 STOPPED로 전환 | 데몬 장애 → 재시작 액션으로 복구 |

연결 거부(connection refused)는 chaos가 아니라 `docker compose stop node-c`로 재현한다.

---

## 4. 상태 판정 (console)

### 4.1 노드별 수집 상태 (메모리)

`last_attempt_at`, `last_success_at`, `consecutive_failures`, `last_error {type, message}`, `latency_ms`, `last_health`(마지막 성공 payload), `skipped_cycles`, `samples`(최근 `SAMPLES_MAX`건 ring buffer: ts, cpu, mem, disk, latency. 성공한 수집만 기록하므로 실패 구간은 비어 있다).

### 4.2 판정 규칙 — 위에서부터 첫 번째로 맞는 규칙 적용

| 순위 | 상태 | UI 표시 | 조건 |
|---|---|---|---|
| 1 | `UNKNOWN` | 확인 중 (회색) | 성공 이력 없음 AND `consecutive_failures < FAIL_THRESHOLD` |
| 2 | `UNREACHABLE` | 장애 · 통신두절 (적색) | `consecutive_failures ≥ FAIL_THRESHOLD` OR `now − last_attempt_at > STALE_FACTOR × POLL_INTERVAL_SEC` |
| 3 | `CRITICAL` | 장애 (적색) | STOPPED 데몬 존재 OR 메트릭 중 하나라도 CRITICAL 임계 이상 |
| 4 | `WARNING` | 경고 (황색) | 메트릭 WARNING 임계 이상 OR `0 < consecutive_failures < FAIL_THRESHOLD` OR `latency_ms ≥ SLOW_MS` OR RESTARTING 데몬 존재 |
| 5 | `HEALTHY` | 정상 (녹색) | 그 외 |

- 연속 실패 1~2회 동안은 마지막 성공 메트릭을 계속 표시하되 "수집 실패 n회" 배지를 붙인다. 값이 오래된 것임을 숨기지 않는다.
- 판정은 항상 `reasons: list[str]`를 함께 돌려준다. 예: `disk 91.2% ≥ 90`, `daemon emr-sync STOPPED`, `3회 연속 TIMEOUT`, `응답 1820ms ≥ 1500`.
- 판정 로직은 `console/app/status.py`의 순수 함수 `evaluate(state, now, settings) -> (status, reasons)`. I/O 금지, 단위 테스트 대상.
- **판정 시점은 poller**다. 노드별 수집 결과를 반영한 직후, 그리고 매 주기 시작 시 전 노드를 평가해 `status`, `reasons`, `evaluated_at`을 수집 상태에 저장한다. API는 저장된 판정을 그대로 반환한다.
- 안전장치: poller의 마지막 평가 시각이 `STALE_FACTOR × POLL_INTERVAL_SEC`보다 오래되면(poller 정지) API는 저장값 대신 `UNREACHABLE`, 사유 `판정 갱신 중단 n초 (poller 정지 의심)`을 반환한다. poller가 멈춘 상태이므로 이벤트는 기록되지 않는다.

### 4.4 상태 전이 이벤트

- poller가 판정할 때 노드의 `status`가 직전 판정과 다르면 `node_events`(§9)에 한 행을 기록한다. `reasons`만 바뀌고 상태가 같으면 기록하지 않는다 (사유에 측정값이 들어 있어 매 주기 바뀌기 때문).
- console 기동 후 노드별 첫 확정 판정(UNKNOWN이 아닌 첫 상태)은 `from_status = null`로 기록한다 ("기동 후 첫 판정"). 이전 프로세스의 마지막 상태와 잇지 않는다.
- 이벤트 기록 실패는 로그만 남기고 수집·판정을 멈추지 않는다.
- 임계치 근처에서 값이 오르내리면 이벤트가 반복될 수 있다 (히스테리시스 없음, 알려진 한계).
- 보관 기간 `EVENTS_RETENTION_DAYS`(기본 30일). 기동 시와 1시간마다 오래된 행을 삭제한다.

### 4.3 임계치

| 메트릭 | WARNING | CRITICAL |
|---|---|---|
| `cpu_pct` | 80 | 95 |
| `mem_pct` | 85 | 95 |
| `disk_pct` | 80 | 90 |

---

## 5. 타임아웃 · 동시성 예산

| 설정 | 기본값 | 근거 |
|---|---|---|
| `POLL_INTERVAL_SEC` | 5 | 준실시간. 노드 부하 최소화 |
| `HEALTH_CONNECT_TIMEOUT` | 1.0s | 사내망 연결은 ms 단위. 1초 초과면 경로 이상으로 본다 |
| `HEALTH_READ_TIMEOUT` | 3.0s | connect+read(4s) < interval(5s) → 사이클이 겹치지 않음 |
| `SLOW_MS` | 1500 | 이 이상이면 지연 경고 |
| `POLL_CONCURRENCY` | 20 | 노드 수가 늘어도 console 소켓·CPU 보호 |
| `FAIL_THRESHOLD` | 3 | 1회 실패로 장애 판정하지 않음 (오탐 억제) |
| `STALE_FACTOR` | 3 | poller 자체 이상(루프 정지 등) 감지. 기준은 `last_attempt_at` — `last_success_at` 기준이면 실패 기록이 read timeout만큼 늦어 노드 장애 시 `FAIL_THRESHOLD`보다 먼저 걸린다 |
| `HTTP_KEEPALIVE_EXPIRY` | 15s | console → agent idle 연결 재사용 상한. **수집 주기(5s) < 이 값 < agent `AGENT_KEEPALIVE_SEC`(30s)**. 두 값이 같으면(과거 기본값 5s = 5s = 주기) 서버가 닫는 순간 연결을 재사용해 `RemoteProtocolError`가 가끔 발생한다 (헬스: 가짜 실패 이벤트, 명령: 가짜 UNKNOWN) |
| `CMD_CONNECT_TIMEOUT` | 2.0s | |
| `CMD_READ_TIMEOUT` | 15s | 최장 액션(재시작 ~5s)의 3배 여유 |
| `JOB_CONCURRENCY` | 10 | 동시에 명령을 받는 노드 수 상한 |
| `OUTPUT_MAX_BYTES` | 65536 | 노드 반환 로그 저장 상한 |
| `EVENTS_RETENTION_DAYS` | 30 | 상태 전이 이벤트 보관 기간 (§4.4) |
| `SAMPLES_MAX` | 60 | 노드별 메모리 샘플 수 (1~17280). 60 × 5s = 5분, 720 = 1시간. 대시보드 시계열 창은 보유 샘플 범위에 맞춰 늘어난다. 재기동 시 초기화 |

규칙:

1. 한 사이클 소요 시간 = 노드별 소요의 **최댓값**(≤ 4s). 노드 수의 합이 아니다 (`POLL_CONCURRENCY` 이내).
2. 노드별 in-flight 플래그. 이전 수집이 끝나지 않았으면 이번 주기는 건너뛰고 `skipped_cycles` 증가.
3. poller 루프는 어떤 예외에도 죽지 않는다 (루프 레벨 try/except + 로그). 다음 주기는 사이클 **시작 시각** 기준으로 sleep 해서 drift를 없앤다.
4. 헬스체크는 재시도하지 않는다. 다음 주기가 곧 재시도다.
5. 같은 노드에 대한 명령은 노드별 `asyncio.Lock`으로 직렬화한다 (재시작 두 건 동시 실행 방지). 대기 중인 대상은 PENDING. 획득 순서는 **노드 락 → 전역 세마포어** (락을 기다리며 세마포어 슬롯을 점유하지 않도록).
6. Job 최악 소요 ≈ ⌈대상 수 / JOB_CONCURRENCY⌉ × (CMD_CONNECT + CMD_READ).

---

## 6. 예외 분류 (`console/app/agent_client.py`)

모든 agent 호출은 예외를 밖으로 던지지 않고 결과 객체로 돌려준다.

| 조건 | `error_type` | 명령 결과 상태 | 의미 | 재시도 |
|---|---|---|---|---|
| `httpx.ConnectError`, `ConnectTimeout`, `PoolTimeout` | `CONNECT_ERROR` | FAILED | 요청이 전달되지 않음 | 안전 |
| `httpx.ReadTimeout`, `WriteTimeout`, `ReadError`, `RemoteProtocolError` | `TIMEOUT` | UNKNOWN | 전달됐을 수 있음, 실행 여부 불명 | **자동 재시도 금지** → reconcile |
| HTTP 400 / 401 | `AGENT_ERROR` | FAILED | agent가 거부 (실행 안 함) | 요청·설정 수정 후 |
| HTTP 5xx | `AGENT_ERROR` | FAILED | agent 내부 오류 | 수동 확인 후 |
| 200인데 응답 스키마 불일치 | `AGENT_ERROR` | 헬스: 실패 / 명령: UNKNOWN | 명령은 실행됐을 수 있음 | reconcile |
| `exit_code ≠ 0` | `EXEC_ERROR` | FAILED | 실행했으나 실패 | 수동 |

> **주의**: httpx에서 `ConnectTimeout`은 `TimeoutException`의 하위 클래스다. `except httpx.TimeoutException`을 먼저 두면 "미전달"이 "결과 미확인"으로 잘못 분류된다. Connect 계열을 먼저 잡는다.

헬스체크에서는 위 분류가 `last_error.type`과 `consecutive_failures` 증가로만 쓰인다.

---

## 7. 액션 카탈로그

| action | UI 이름 | params | 위험도 | 시뮬레이션 소요 | agent 동작 |
|---|---|---|---|---|---|
| `CLEAN_LOGS` | 로그 정리 | `older_than_days`: 1 / 3 / 7 (기본 7) | LOW | 1~3s | `disk_pct` 5~15%p 감소, 삭제 파일 목록(더미) 출력 |
| `FLUSH_CACHE` | 캐시 플러시 | 없음 | MEDIUM | 1~2s | `mem_pct` 10~25%p 감소 |
| `RESTART_DAEMON` | 데몬 재시작 | `daemon`: `pacs-gateway` / `hl7-interface` / `emr-sync` | HIGH | 3~5s | RESTARTING → RUNNING, pid·uptime 갱신 |
| `COLLECT_DIAG` | 진단 정보 수집 | 없음 | NONE (읽기 전용) | < 1s | 변경 없음, 상세 스냅샷 텍스트 반환 |

- 카탈로그의 원본은 console이며 `GET /api/actions`로 제공한다. UI는 이 응답으로 액션 목록과 파라미터 폼을 만든다 (UI 하드코딩 금지).
- 파라미터 검증은 console(1차)과 agent(2차) 양쪽에서 한다.
- 위험도 HIGH는 UI에서 대상 노드 목록을 보여주는 확인 단계를 거친다.

---

## 8. Console API

- 인증은 **미들웨어로 적용**한다 — `StaticFiles` mount에는 라우터 dependency가 적용되지 않아 대시보드가 무인증으로 열리기 때문이다.
  - 브라우저: 로그인 페이지(`/login`)에서 ID/PW 입력 → 세션 쿠키 `nw_session` (HttpOnly, SameSite=Strict, `COOKIE_SECURE`). 세션은 console 메모리에 두며 재기동 시 다시 로그인한다 (단일 워커).
  - 스크립트: `Authorization: Basic` 헤더도 허용한다. 인증 창 팝업을 띄우지 않도록 `WWW-Authenticate`는 보내지 않는다.
  - 무인증 공개 경로: `/healthz`, `/login`, `/api/login`, `/style.css`, `/img.png`.
  - 무인증 요청: `/api/*` → 401 JSON, 그 외 GET/HEAD → 302 `/login`, 그 외 메서드 → 401.
  - 비밀번호는 scrypt(N=2^14, r=8, p=1, salt 16B) 해시로만 보관하고 `hmac.compare_digest`로 비교한다. 해시 계산(약 50ms)은 이벤트 루프를 막지 않도록 스레드에서 실행한다. 없는 사용자명도 더미 해시를 계산해 응답 시간으로 계정 존재 여부가 드러나지 않게 한다.
  - 로그인·Basic 실패는 클라이언트 IP별로 세어 `LOGIN_MAX_FAILURES`회 연속이면 `LOGIN_LOCKOUT_SEC`초 동안 429.
  - 기동 시 비밀번호가 기본값(`nodewatch`, `monwatch`)이면 경고 로그를 남긴다.

**역할 (기본 차단)**

| 역할 | 허용 | 그 외 |
|---|---|---|
| `admin` | 전체 | — |
| `monitor` | `GET /api/*` (단, `/api/nodes/{id}/chaos` 제외), `POST /api/logout`, 정적 파일 | 403 `{"detail": "권한이 없습니다 (monitor)"}` — 일괄 명령, reconcile, retry, 장애 주입(chaos) 조회·변경 |

- 권한 검사는 인증 미들웨어에서 한다 (라우트가 추가돼도 monitor는 기본 차단).
- 오류 응답은 FastAPI 기본 형식 `{"detail": "..."}`.
- 요청자(`requested_by`)는 인증된 사용자명으로 기록한다.

| Method | Path | 설명 | 응답 |
|---|---|---|---|
| GET | `/healthz` | 컨테이너 헬스체크 (무인증) | 200 `{"status":"ok"}` |
| GET | `/login` | 로그인 페이지 (무인증). 이미 로그인돼 있으면 302 `/` | 200 / 302 |
| POST | `/api/login` | `{"username","password"}` → 세션 쿠키 발급 (무인증) | 200 `{"username"}` / 401 / 429 |
| POST | `/api/logout` | 세션 삭제, 쿠키 만료 | 200 |
| GET | `/api/me` | 현재 사용자와 역할 | 200 `{"username", "role"}` (`admin` / `monitor`) |
| GET | `/api/nodes` | 전체 노드 현재 상태 | 200 `NodeView[]` |
| GET | `/api/nodes/{node_id}` | 단일 노드 + 최근 샘플 `SAMPLES_MAX`건 | 200 / 404 |
| GET | `/api/actions` | 액션 카탈로그 | 200 |
| POST | `/api/jobs` | 일괄 명령 생성 | 202 `{job_id}` / 400 |
| GET | `/api/jobs?limit=50` | 이력 목록 (최신순) | 200 `JobSummary[]` |
| GET | `/api/jobs/{job_id}` | 상세 + 노드별 결과·출력 | 200 / 404 |
| POST | `/api/jobs/{job_id}/reconcile` | UNKNOWN 대상 결과 재조회 | 202 |
| POST | `/api/jobs/{job_id}/retry` | 실패 대상 재실행 (새 job) | 202 `{job_id}` |
| GET/POST | `/api/nodes/{node_id}/chaos` | 데모용 장애 주입 중계. agent 응답 코드를 전달하되 agent의 401/403(토큰 설정 오류)과 전송 실패는 502 — 브라우저가 console 세션 만료로 오인하지 않게 | 200 / 400 / 502 |
| GET | `/api/events?limit=100&node_id=&before_id=` | 상태 전이 이벤트 (최신순). `node_id`로 필터, `before_id`로 이전 페이지 | 200 `NodeEvent[]` / 400 |

**POST /api/jobs**

```json
{"targets": ["node-a", "node-c"], "action": "RESTART_DAEMON", "params": {"daemon": "emr-sync"}}
```

- `"targets": "all"` → 등록된 전체 노드 (디스패치 시점 기준).
- 빈 목록, 미등록 node_id, 카탈로그에 없는 action, 허용되지 않은 params → 400.
- 응답은 DB에 job과 대상별 PENDING 행을 기록한 직후 반환한다. 실행 완료를 기다리지 않는다.

**retry**: 대상 = 부모 job의 FAILED 대상. UNKNOWN은 기본 제외하고 `{"include_unknown": true}`일 때만 포함 (UI 경고 확인 후). 새 job, 새 `command_id`, `parent_job_id` 기록.

**reconcile**: UNKNOWN 대상마다 agent `GET /commands/{command_id}`.
- 200 `DONE` → 결과 반영 (SUCCESS 또는 FAILED/EXEC_ERROR), `reconciled = 1`.
- 200 `RUNNING` → UNKNOWN 유지, 메시지 "agent에서 실행 중".
- 404 → UNKNOWN 유지, 메시지 "agent에 기록 없음 (미수신 또는 agent 재기동)". 자동으로 FAILED 처리하지 않는다.
- 반영 후 job 상태 재계산.

**NodeView**

```json
{
  "node_id": "node-c",
  "node_name": "병원 C",
  "status": "CRITICAL",
  "reasons": ["daemon emr-sync STOPPED"],
  "metrics": {"cpu_pct": 31.0, "mem_pct": 48.2, "disk_pct": 58.9},
  "daemons": [{"name": "emr-sync", "status": "STOPPED", "pid": null, "uptime_sec": 0}],
  "latency_ms": 12,
  "consecutive_failures": 0,
  "skipped_cycles": 0,
  "last_attempt_at": "2026-10-01T03:00:05Z",
  "last_success_at": "2026-10-01T03:00:05Z",
  "last_error": null
}
```

**NodeEvent**

```json
{"id": 42, "node_id": "node-c", "node_name": "병원 C", "ts": "2026-10-01T03:00:20Z",
 "from_status": "CRITICAL", "to_status": "UNREACHABLE", "reasons": ["3회 연속 TIMEOUT"]}
```

**JobDetail**

```json
{
  "job_id": "…",
  "parent_job_id": null,
  "action": "CLEAN_LOGS",
  "params": {"older_than_days": 7},
  "requested_by": "admin",
  "status": "PARTIAL",
  "created_at": "…Z",
  "finished_at": "…Z",
  "counts": {"PENDING": 0, "RUNNING": 0, "SUCCESS": 2, "FAILED": 0, "UNKNOWN": 1},
  "results": [
    {
      "node_id": "node-c",
      "command_id": "…",
      "status": "UNKNOWN",
      "error_type": "TIMEOUT",
      "error_message": "ReadTimeout after 15.0s",
      "exit_code": null,
      "output": null,
      "output_truncated": false,
      "reconciled": false,
      "started_at": "…Z",
      "finished_at": "…Z",
      "duration_ms": 15003
    }
  ]
}
```

**Job 상태 규칙**

| 상태 | 조건 |
|---|---|
| `RUNNING` | PENDING 또는 RUNNING 대상 존재 |
| `COMPLETED` | 전 대상 SUCCESS |
| `PARTIAL` | SUCCESS 1개 이상 + 비성공 1개 이상 |
| `FAILED` | SUCCESS 0개 |
| `INTERRUPTED` | console 재기동으로 중단됨 (§9 복구). reconcile 후에도 재계산하지 않고 유지한다 (중단 이력 보존, 대상별 결과는 갱신됨) |

---

## 9. 저장소 (SQLite)

PRAGMA: `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`. 파일 `/data/nodewatch.db` (named volume).

```sql
CREATE TABLE IF NOT EXISTS jobs (
  id             TEXT PRIMARY KEY,
  parent_job_id  TEXT REFERENCES jobs(id),
  action         TEXT NOT NULL,
  params_json    TEXT NOT NULL,
  requested_by   TEXT NOT NULL,
  status         TEXT NOT NULL,  -- RUNNING | COMPLETED | PARTIAL | FAILED | INTERRUPTED
  created_at     TEXT NOT NULL,  -- UTC ISO8601
  finished_at    TEXT
);

CREATE TABLE IF NOT EXISTS job_results (
  job_id           TEXT NOT NULL REFERENCES jobs(id),
  node_id          TEXT NOT NULL,
  command_id       TEXT NOT NULL UNIQUE,
  status           TEXT NOT NULL,  -- PENDING | RUNNING | SUCCESS | FAILED | UNKNOWN
  error_type       TEXT,           -- CONNECT_ERROR | TIMEOUT | AGENT_ERROR | EXEC_ERROR | INTERRUPTED
  error_message    TEXT,
  exit_code        INTEGER,
  output           TEXT,
  output_truncated INTEGER NOT NULL DEFAULT 0,
  reconciled       INTEGER NOT NULL DEFAULT 0,
  started_at       TEXT,
  finished_at      TEXT,
  duration_ms      INTEGER,
  PRIMARY KEY (job_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS node_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id       TEXT NOT NULL,
  ts            TEXT NOT NULL,   -- UTC ISO8601, 판정 시각
  from_status   TEXT,            -- NULL = console 기동 후 첫 판정
  to_status     TEXT NOT NULL,   -- UNKNOWN | UNREACHABLE | CRITICAL | WARNING | HEALTHY
  reasons_json  TEXT NOT NULL    -- to_status 판정 사유 (list[str])
);

CREATE INDEX IF NOT EXISTS idx_node_events_node ON node_events(node_id, id DESC);
```

**기동 시 복구** (console 재시작으로 끊긴 job 처리):

- `status = RUNNING`인 job → `INTERRUPTED`.
- 그 job의 PENDING 대상 → FAILED / `INTERRUPTED` (전송 전이므로 재시도 안전).
- 그 job의 RUNNING 대상 → UNKNOWN / `INTERRUPTED` (전송 후일 수 있으므로 reconcile 대상).

헬스 샘플은 메모리 ring buffer에만 두고 영속화하지 않는다 (상태 전이 이벤트만 `node_events`에 영속화). 시계열 보관은 모니터링 시스템의 책임이며 이 프로토타입은 현재 상태 중심이다.

---

## 10. 대시보드

- 정적 파일: `index.html`, `app.js`, `style.css`, `login.html`, `img.png`(로그인 화면 로고). 외부 CDN, 웹폰트, 빌드 단계 없음 (폐쇄망에서도 그대로 동작).
- 로그인 페이지: 로고를 화면 중앙에 두고 그 아래 ID/PW 입력. 성공 시 `/`로 이동. 대시보드 상단에 사용자명·역할과 [로그아웃]. API가 401을 돌려주면(세션 만료, console 재기동) 로그인 페이지로 이동한다.
- 브라우저 탭이 백그라운드(`document.hidden`)이면 폴링을 멈추고, 다시 보이면 즉시 갱신한다 (보이지 않는 화면이 샘플 전체를 반복 조회하지 않게).
- `monitor` 역할: 일괄 제어·데모 제어 탭을 표시하지 않고, 해당 탭으로 직접 들어오면 상태 현황으로 보낸다. 실행 이력은 조회만 가능하며 [결과 재확인]·[실패 대상 재실행] 버튼을 표시하지 않는다. 역할을 확인하기 전에는 제어 탭을 숨긴 상태로 시작한다.
- 모든 API 호출은 상대경로 (`/api/...`).
- 시각은 API의 UTC 값을 브라우저 로컬 시간으로 변환해 표시.

| 탭 | 내용 | 갱신 |
|---|---|---|
| 1. 상태 현황 | 노드 카드: 상태 색·라벨, reasons, CPU/MEM/DISK 바, 데몬 목록, 응답시간, "마지막 수집 n초 전", 수집 실패 배지. 상단 요약(정상/경고/장애 개수). 시계열 패널 4개(CPU/MEM/DISK/응답시간): 노드별 선, 임계선, 최근 샘플(`GET /api/nodes/{id}`의 `samples`), 수집 실패 구간은 끊어서 표시, hover 시 시각별 값 | 3초 폴링 |
| 2. 일괄 제어 | 노드 체크박스(상태 배지 포함, 전체 선택), 액션 선택 → 카탈로그 기반 params 폼, 실행. HIGH 위험도 → 대상 확인 모달. UNREACHABLE 노드 선택 시 경고 문구 (실행은 허용). 실행 후 해당 job 상세로 이동 | — |
| 3. 실행 이력 | job 목록(시각, 액션, 대상 수, 결과 카운트, 요청자, 상태). 상세: 노드별 상태, error_type, 소요시간, 출력(`<pre>`, 접기). [결과 재확인] [실패 대상 재실행] 버튼 | RUNNING job은 2초 폴링 |
| 4. 이벤트 | 상태 전이 타임라인 (최신순, 날짜별 묶음): 시각, 노드, 이전 → 새 상태 배지, 사유, 이전 상태 지속 시간. 노드 필터, 더 보기(`before_id`) | 5초 폴링 |
| 5. 데모 제어 | 노드별 chaos 설정 (지연, 오류율, blackhole, 데몬 중지, 초기화). "데모 전용" 표기 | — |

- console API 호출 자체가 실패하면 상단에 "콘솔 연결 끊김 — 마지막 갱신 hh:mm:ss" 배너를 띄우고 기존 화면 값을 흐리게 표시한다.

---

## 11. 보안

| 위협 | 대응 |
|---|---|
| 원격 임의 명령 실행 | 액션 화이트리스트 + enum 파라미터. 셸 문자열 실행 경로 자체가 없음 |
| 무단 agent 호출 | 노드별 토큰 `X-Agent-Token`, `hmac.compare_digest`. 토큰은 env로만 주입, 로그에 기록 금지 |
| 노드 직접 노출 | 노드 포트 호스트 미노출, `nodenet` internal 네트워크 |
| 대시보드 무단 접근 | 로그인 페이지 + 세션 쿠키(HttpOnly, SameSite=Strict), API는 Basic 헤더 허용. IP별 연속 실패 차단. 요청자 감사 기록 |
| 비밀번호 노출 (`.env`, `docker inspect`) | 평문 대신 scrypt 해시만 보관 |
| 조회 사용자의 오조작 | `monitor` 역할: 서버 미들웨어에서 제어 API 403, 화면에서 제어 탭·버튼 제거 |
| CSRF | 세션 쿠키 SameSite=Strict (다른 사이트에서 온 요청에 쿠키 미전송) |
| 클릭재킹 (제어 화면을 다른 사이트 iframe에 삽입) | 모든 응답에 `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`. 그 외 `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` |
| 로그인 실패 카운터 메모리 증가 (다수 IP) | IP 상태 보관 상한 10,000개. 넘으면 차단 중이 아닌 항목부터 정리 |
| 과대 출력으로 인한 DB·화면 장애 | `OUTPUT_MAX_BYTES` 절단 + `output_truncated` 플래그 |

범위 밖 (README 한계 절에 기술): TLS/mTLS, 토큰 로테이션, RBAC, 2인 승인.

---

## 12. 컨테이너 운영 설정 (docker-compose)

| 항목 | 값 | 이유 |
|---|---|---|
| `restart` | `unless-stopped` | EC2 재부팅·프로세스 크래시 자동 복구 |
| `healthcheck` | console `/healthz`, agent `/livez` | 상태 가시화 |
| healthcheck 명령 | `python -c "import urllib.request; urllib.request.urlopen('http://localhost:PORT/...')"` | slim 이미지에는 `curl`이 없다 |
| `logging` | `json-file`, `max-size: 10m`, `max-file: 3` | 소형 EBS 디스크 고갈 방지 |
| `mem_limit` | console 256m, agent 96m | 1GB 인스턴스에서 OOM 격리 |
| volume | `nodewatch-data:/data` | job 이력 보존 |
| 시간대 | 컨테이너 TZ 설정 불필요 | UTC 저장, 브라우저에서 변환 |

---

## 13. 범위 밖

실제 SSH/원격 셸 실행, 알림(메일·메신저), 시계열 DB, 다중 사용자·권한, console 이중화.
