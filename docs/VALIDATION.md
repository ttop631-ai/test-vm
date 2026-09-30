# 교차 검증 기록

다른 코드 생성 도구(Codex)가 이 저장소의 사본에서 수행한 소스 검증 보고서를 입력으로 받아, **각 지적이 이 저장소 코드에서 실제로 재현되는지** 확인하고 재현된 것만 수정한 기록이다.

| 항목 | 값 |
|---|---|
| 검증일 | 2026-10-01 |
| 대상 | `b279bd9` (수정 전) → `04a314c` (수정 후) |
| 입력 | Codex 보고서. 작업 사본의 수정 전 원본(`/tmp/nodewatch-original.tar.gz`)이 `b279bd9`와 동일함(점 파일 제외)을 확인 |
| 원칙 | 보고 내용을 그대로 반영하지 않는다. **회귀 테스트를 먼저 작성해 수정 전 코드에서 실패(재현)시키고**, SPEC을 먼저 고친 뒤 코드를 수정해 통과시킨다. Codex 수정 코드는 가져오지 않고 독립 구현했다 |

## 1. 항목별 판정

| # | 지적 | 이 저장소에서의 재현 | 판정 | 수정 |
|---|---|---|---|---|
| 1 | 차단 전에 시작한 병렬 인증의 늦은 실패가 IP 차단을 해제 | 단위 테스트: 차단 중 `fail()`이 `(1, 0.0)`을 기록해 차단 해제 | **결함 (보안)** | `7f4ea1c` 차단 중 실패는 무시 |
| 2 | 겹친 상태 갱신의 오래된 집계가 COMPLETED를 RUNNING으로 덮어씀 | 단위 테스트: 최종 상태 `RUNNING` (기대 `COMPLETED`). job 실행 중 reconcile 시 발생 가능, 이후 갱신 계기가 없어 영구 RUNNING | **결함** | `7f4ea1c` 집계 읽기·상태 쓰기를 쓰기 락 안에서 |
| 3 | 상세의 counts와 results가 다른 시점 | 단위 테스트: results `{SUCCESS:1, RUNNING:1}` / counts `{SUCCESS:2}` | 결함 (경미) | `7f4ea1c` counts를 results에서 계산 |
| 4 | 이력 목록이 전체 이력을 먼저 집계 | 최신 3개 조회 비용이 이력에 비례: job 1,000→2,000개에 SQLite VM 단계 약 140,400→276,000 | 결함 (성능) | `7f4ea1c` 최신 limit개를 인덱스로 먼저 선택 → 약 700 단계로 일정 |
| 5 | 응답 전까지 `last_attempt_at`이 갱신되지 않음 | 단위 테스트: 요청 대기 중 `None`. 기본 설정(응답 대기 최대 약 4초)에서는 판정 영향 없음, read timeout이 약 10초 이상이거나 동시성 대기가 길면 진행 중인 수집을 stale로 오판 | 결함 (설정 의존) | `7f4ea1c` 요청 시작 시 기록 |
| 6 | 동시성 0·주기 0 허용, NaN 메트릭 | 설정 17개 경우와 메트릭 6개 경우 모두 수용됨. NaN은 모든 임계치 비교가 거짓이라 '정상' 판정 + API에서 `null` → 대시보드 `toFixed` 오류로 상태 화면 파손 | **결함** | `7f4ea1c` 설정 범위·임계치 순서, 메트릭 0~100 유한값. `b7edb5a` agent `TICK_SEC` 등 |
| 7 | 취소된 명령이 영구 RUNNING, 데몬이 RESTARTING 고정 | 테스트에서 강제 취소 시 재현. 다만 현재 코드에는 취소 경로가 없음(`asyncio.shield`, 프로세스 종료 시에만 취소되며 메모리 상태도 함께 소멸) | 도달 불가 → **방어 조치** | `b7edb5a` 취소를 실패 결과로 캐시, 데몬 STOPPED |
| 8 | 대시보드 중복 조회, 샘플 오배정, 이전 응답의 화면 덮어쓰기 | 브라우저(네트워크 가로채기): 이전 필터 이벤트 혼입, 이전 job의 404가 새 상세를 덮음, 노드당 샘플 요청 최대 3개 동시 — Chromium·WebKit 모두. 샘플 오배정은 WebKit에서 재현(`node-a`↔`node-c`) | **결함** | `04a314c` 필터 세대 토큰, 응답 시점 jobId 확인, 요청 시점 id 고정, 진행 중 요청 공유 |
| 9a | 큰 샘플 배열 스프레드로 RangeError | 노드 12개 × 샘플 17,280개에서 `Math.max(...)` RangeError (Chromium·WebKit), 응답 시간 패널이 빈 채로 남음 | 결함 (대용량) | `04a314c` 반복문 |
| 9b | 종료 중 주기 태스크를 기다리지 않음 | 재시작 반복(console 3회, agent 1회)에서 종료 로그 오류 없음 | 경미 → 방어 조치 | `7f4ea1c`, `b7edb5a` 취소한 루프 완료를 기다린 뒤 자원 종료 |

### 보고서에 없던 추가 발견

| 내용 | 수정 |
|---|---|
| 대시보드 폴링의 catch가 **렌더 코드 예외까지 삼켜** 콘솔에도 남지 않음. 9a 검사가 처음에 '통과'로 보인 원인 (패널 4개 중 3개만 그려졌는데 오류 0건) | `04a314c` API 오류 외 예외는 `console.error` |
| agent `TICK_SEC=0`이면 시뮬레이터 busy loop | `b7edb5a` 0 이하·NaN·무한대 기동 거부 |

## 2. 검증 결과

| 구분 | 수정 전 (`b279bd9` + 새 테스트) | 수정 후 (`04a314c`) |
|---|---|---|
| console 단위 테스트 | 79 통과 / **30 실패** | **109 통과** |
| agent 단위 테스트 (신설) | 10 통과 / **7 실패** | **17 통과** |
| 대시보드 경합·대용량 (Chromium, WebKit 각 5개) | Chromium 필터·404·중복·차트 실패, WebKit 5개 모두 실패 | **10/10 통과** |
| 병렬 로그인 (실서비스) | — | 틀린 비밀번호 20건 동시 → 차단 1회, 이후 올바른 비밀번호 429 ×3, 60초 후 200 |
| 신규 배포 (HEAD clone, 캐시 없는 빌드, README 절차, 격리 스택) | — | 시연 시나리오 28/28 |
| 브라우저 회귀 (로그인·역할·대시보드 전 흐름·연결 끊김 배너·백그라운드 폴링 중지) | — | 통과. JS 오류는 console 중지 시 의도된 연결 오류 1건 |
| 종료 로그 (재시작 반복) | — | 오류 없음 |

## 3. 재현 방법

```bash
# 단위 테스트 (console, agent 각각 — 둘 다 app 패키지를 쓰므로 따로 실행)
for d in console agent; do
  docker run --rm -v "$PWD/$d:/src:ro" -w /src python:3.12-slim \
    sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider"
done

# 수정 전 재현: 수정 전 코드(b279bd9)를 별도 worktree로 꺼내 새 테스트만 복사해 실행 (작업 트리는 건드리지 않음)
git worktree add ../nw-before b279bd9
cp -r console/tests/. ../nw-before/console/tests/
cp -r agent/tests agent/requirements-dev.txt ../nw-before/agent/
(cd ../nw-before && for d in console agent; do
  docker run --rm -v "$PWD/$d:/src:ro" -w /src python:3.12-slim \
    sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider"
done)   # console 30 실패, agent 7 실패
git worktree remove --force ../nw-before
```

- `test_jobs.py`의 경합 테스트는 `Database.counts`를 늦게 반환하도록 바꿔 두 갱신이 겹치는 순서를 결정적으로 만든다.
- 이력 조회 비용은 `sqlite3.set_progress_handler`로 VM 단계를 센다. 응답 시간이나 전체 처리량이 아니라 해당 쿼리의 처리 단계 비교다.
- 대시보드 경합 검사는 Playwright(`mcr.microsoft.com/playwright/python:v1.49.0-noble`)로 `/api/events`, `/api/jobs/{id}`, `/api/nodes[/{id}]` 응답을 가로채 지연·순서를 조작한다. 검사 스크립트는 저장소 밖(로컬 `.e2e/`)에 있다.

## 4. 한계

- 노드 12개 × 샘플 17,280개 조건에서 차트 렌더 한 번에 약 650ms(Chromium). 기본 설정(노드 3개 × 샘플 60개)에서는 영향 없음. 대용량 운영이라면 서버 다운샘플링 API가 필요하다 (SPEC 변경).
- 동시에 몰아 보낸 로그인 요청은 차단 전에 모두 비밀번호 검사까지 간다(요청마다 scrypt 약 50ms). 차단은 유지되지만 한 번의 몰아 보내기로 여러 번 시도할 수 있다.
- 7·9b는 현재 도달 경로가 없는 방어 조치다.

## 5. CLAUDE.md MUST 점검 (이번 변경 기준)

| 항목 | 결과 |
|---|---|
| 1·2·4 타임아웃 분리, 노드별 병렬, 예외 분류 순서 | 변경 없음. 시도 시각 기록 위치만 요청 시작으로 이동 |
| 3 예외를 결과로 반환 | 유지. 메트릭 범위 위반은 스키마 불일치(AGENT_ERROR)로 결과 객체에 담김 |
| 5 백그라운드 태스크 참조·로그 | 유지. 종료 시 취소한 루프 완료 대기 추가 |
| 6·7 화이트리스트, command_id 멱등 | 유지. 취소된 명령도 결과를 남겨 재실행 방지 |
| 8 자동 재시도 금지 | 유지. 재시도 경로 추가 없음 |
| 9 노드 락 → 세마포어 | 유지. 상태 재계산 락은 원격 호출을 포함하지 않음 (DB 쓰기 락 안에서 집계·쓰기만) |
| 10 poller 유지·주기 | 유지 |
| 11·12·13 UTC, 출력 절단, env 설정 | 유지. 설정 범위 검증 추가 |
| 14 미들웨어 인증·안전한 비교 | 유지. 차단 해제 경합만 수정 |
| 15 key=value 로그·비밀값 미기록 | 유지. `event=command_cancelled`에 command_id 포함 |
