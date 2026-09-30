# history.md — AI 협업 및 트러블슈팅 기록

> 작성 원칙: 실제로 발생한 건만 기록한다. Before/After는 커밋 해시로 연결해 저장소 히스토리에서 그대로 확인할 수 있게 한다.

---

## 1. 사용 AI 도구

| 도구 | 모델 | 사용 범위 |
|---|---|---|
| Claude (claude.ai 프로젝트) | Claude Opus 5.5 | 요구사항 분석, 설계 문서(`docs/SPEC.md`, `README.md`, `CLAUDE.md`) 초안, 설계 리뷰 |
| TBD (코드 생성 도구) | TBD | 단계별(S1~S5) 코드 생성 |

엔지니어 역할: 설계 결정과 명세 확정, 생성 코드 리뷰·보정, 장애 주입 검증, 배포.

---

## 2. 초기 프롬프트 전략

### 2.1 명세 우선 (Spec-first)

코드를 요청하기 전에 `docs/SPEC.md`(무엇을 만들지)와 `CLAUDE.md`(어떻게 만들지, 무엇을 금지할지)를 먼저 확정했다.

이유: 요구사항 원문만 주고 코드를 요청하면 AI는 해피패스 중심, 순차 호출, 타임아웃 미지정, 명령 문자열을 그대로 실행하는 구조로 수렴하는 경향이 있다. 온프레미스 원격 운영에서 중요한 판단(타임아웃 예산, 실패와 미확인의 구분, 오탐 억제 기준)은 AI가 추론하게 두지 않고 명세에 숫자와 규칙으로 고정했다.

### 2.2 단계 분할과 완료 기준

S1(agent) → S2(poller·상태 판정) → S3(job·이력) → S4(대시보드) → S5(배포)로 나누고, 각 단계에 **장애 주입으로 확인 가능한 완료 기준**을 붙였다 (`CLAUDE.md §7`). AI의 "구현 완료" 보고가 아니라 이 기준으로 통과 여부를 판단했다.

### 2.3 금지 패턴 명시

도메인 경험상 사고로 이어지는 패턴을 `CLAUDE.md §4, §5`에 명시했다. 예: 참조 없는 `create_task`, `TaskGroup` 예외 전파, `ConnectTimeout` 오분류, `shell=True`, 멀티 워커.

### 2.4 자기 점검 보고 요구

코드 생성 후 MUST 항목별 준수 여부를 표로 보고하게 했다. 보고와 실제 코드가 다른 경우도 기록 대상으로 삼았다.

### 2.5 커밋 규칙

AI 원본을 수정 없이 `ai(Sx):`로 먼저 커밋하고, 엔지니어 보정은 `fix(Sx):`로 분리했다. 아래 사례의 Before/After는 이 두 커밋이다.

### 2.6 실제 사용 프롬프트 (발췌)

S2 요청 예:

```text
docs/SPEC.md §4(상태 판정), §5(타임아웃·동시성 예산), §6(예외 분류), §3.3(agent API)과
CLAUDE.md를 기준으로 console/app/agent_client.py, poller.py, status.py를 작성해줘.

- status.py는 I/O 없는 순수 함수 evaluate(state, now, settings) -> (status, reasons)로 만들고
  tests/test_status.py에 판정 규칙 표의 각 행을 검증하는 테스트를 포함.
- 완료 기준: node-c에 blackhole 주입 시 node-a/b의 last_success_at 간격이 5±1초로 유지되고,
  node-c는 3회 연속 실패 후 UNREACHABLE.
- 작성 후 CLAUDE.md §4 MUST 항목별 준수 여부를 표로 보고.
```

TBD: 실제 사용한 프롬프트로 교체·추가.

---

## 3. 엔지니어 개입 및 트러블슈팅 사례

> 최소 2건 필수. 각 사례는 아래 형식을 따른다.

### 사례 1. TBD 제목

| 항목 | 내용 |
|---|---|
| 단계 | TBD (S1~S5) |
| 발견 경위 | TBD (리뷰 / 장애 주입 테스트 / 배포 중 등) |
| 증상 | TBD |
| AI 원본 커밋 | `TBD` |
| 보정 커밋 | `TBD` |

**Before (AI 생성)**

```python
# TBD
```

**After (보정)**

```python
# TBD
```

**판단 근거**

TBD — 왜 문제인지(운영 관점 영향), 왜 이 방식으로 고쳤는지, 검토했으나 채택하지 않은 대안.

**검증**

```bash
# TBD — 재현 명령과 결과
```

---

### 사례 2. TBD 제목

(사례 1과 같은 형식)

---

### 사례 3. TBD 제목 (선택)

(사례 1과 같은 형식)

---

## 4. 개입 포인트 체크리스트

구현 중 아래 항목을 리뷰·테스트로 확인하고, 실제로 발생한 건만 §3 사례로 옮긴다. 발생하지 않은 항목은 "확인함 / 해당 없음"으로 표시한다.

| # | 영역 | AI가 흔히 내놓는 형태 | 운영 관점 문제 | 보정 방향 | 확인 방법 | 결과 |
|---|---|---|---|---|---|---|
| 1 | 헬스체크 | `for` 루프 순차 `await`, 타임아웃 미지정 | 한 노드 지연이 전체 사이클 지연 | 노드별 병렬 + connect/read 분리 | blackhole 주입 후 A/B 갱신 간격 | |
| 2 | 병렬 예외 | `TaskGroup` 또는 `gather` 기본값 | 한 노드 예외가 다른 노드 수집 취소·전체 실패 | 노드 함수가 예외를 흡수하고 결과 객체 반환 | `error_rate=1.0` 주입 | |
| 3 | 예외 분류 | `except httpx.TimeoutException` 하나로 처리 | 미전달(ConnectTimeout)을 결과 미확인으로 오분류 → 재시도 판단 오류 | Connect 계열을 먼저 분기 | agent 중지 후 명령 결과 error_type | |
| 4 | 장애 판정 | 1회 실패 = DOWN | 상태 깜빡임, 경보 피로 | 연속 N회 + 중간 단계 경고 | `error_rate=0.3` 주입 | |
| 5 | 타임아웃 의미 | Timeout → FAILED + 자동 재시도 | 데몬 재시작 중복 실행 | UNKNOWN + `command_id` 멱등 + reconcile | blackhole 중 명령 후 agent 실행 횟수 | |
| 6 | 백그라운드 작업 | 참조 없는 `create_task`, 예외 미처리 | job이 조용히 멈추고 로그도 없음 | 태스크 레지스트리 + done callback 로깅 | 워커 내부 강제 예외 | |
| 7 | 명령 실행 | 명령 문자열 수신 → `subprocess(shell=True)` | 원격 명령 주입 | 액션 화이트리스트 + enum 파라미터 | 미등록 action 요청 → 400 | |
| 8 | 이벤트 루프 블로킹 | async 함수 안 `time.sleep`, 동기 `sqlite3` | 명령 실행 중 API 전체 정지 | `asyncio.sleep`, aiosqlite | 명령 실행 중 `/api/nodes` 응답 시간 | |
| 9 | 인증 범위 | 라우터 dependency로 Basic 인증 | StaticFiles mount가 무인증으로 열림 | 미들웨어로 전환 | 인증 없이 `/` 요청 → 401 | |
| 10 | 프론트 주소 | `http://localhost:8000` 하드코딩 | AWS에서 API 호출 실패 | 상대경로 | 공인 IP로 접속 | |
| 11 | 워커 수 | `uvicorn --workers 4` | poller 중복 실행, 워커별 상태 불일치 | 단일 워커 명시 | 로그의 수집 횟수 | |
| 12 | 컨테이너 헬스체크 | `curl -f http://localhost/...` | slim 이미지에 curl 없음 → 계속 unhealthy | Python urllib 한 줄 | `docker compose ps` | |
| 13 | 출력 저장 | 반환 로그 전체 저장 | DB 비대, 화면 멈춤 | 64KB 절단 + 플래그 | 대용량 출력 액션 | |
| 14 | 시간 | naive `datetime.now()` | 컨테이너 TZ에 따라 표시 불일치 | UTC 저장, 브라우저 변환 | API 응답 시각 형식 | |
| 15 | 폴링 주기 | 사이클 종료 후 `sleep(interval)` | 주기가 조금씩 밀림 (drift) | 사이클 시작 시각 기준 대기 | 10분간 수집 간격 | |

---

## 5. 회고

TBD — AI 생성 코드의 한계로 확인된 것, 명세·규칙으로 막을 수 있었던 것과 없었던 것, 다음에 바꿀 프롬프트 전략 (3~5줄).
