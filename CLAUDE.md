# CLAUDE.md — NodeWatch AI 작업 지시서

> 코드 생성 AI에게 주는 고정 컨텍스트다. 모든 코드 요청 전에 이 파일과 `docs/SPEC.md`를 먼저 읽게 한다.
> 명세와 충돌하는 구현이 필요하면 코드를 쓰기 전에 충돌 지점을 먼저 보고한다.
> (다른 AI 도구를 쓰면 같은 내용을 해당 도구의 규칙 파일로 복사한다.)

## 1. 한 줄 요약

분산된 병원 노드(A/B/C)의 상태를 중앙에서 주기 수집하고, 화이트리스트 운영 명령을 여러 노드에 비동기로 일괄 실행·기록하는 미니 플랫폼.

## 2. 기술 스택 (고정)

| 영역 | 선택 | 비고 |
|---|---|---|
| console | Python 3.12, FastAPI, httpx `AsyncClient`, aiosqlite, pydantic-settings | uvicorn **단일 워커** |
| agent | Python 3.12, FastAPI | 같은 이미지를 env만 바꿔 3개 기동 |
| 프론트엔드 | 정적 HTML + Vanilla JS + CSS | 빌드 단계·CDN·프레임워크 없음 |
| 저장소 | SQLite (WAL) | `/data` 볼륨 |
| 배포 | docker compose | 호스트 노출 포트는 console 하나 |

`requirements.txt`, `requirements-dev.txt`는 버전을 `==`로 고정한다. 테스트 전용 의존성(pytest)은 `requirements-dev.txt`에만 두고 이미지에는 설치하지 않는다.

## 3. 디렉터리 구조

```
.
├── README.md
├── history.md
├── CLAUDE.md
├── docker-compose.yml
├── compose.dev.yml     # 검증용 override (agent 포트 127.0.0.1 바인딩)
├── .env.example
├── docs/
│   └── SPEC.md
├── console/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── nodes.json
│   ├── tests/
│   │   ├── test_status.py
│   │   ├── test_events.py
│   │   └── test_auth.py
│   └── app/
│       ├── main.py          # FastAPI 앱, lifespan에서 poller 시작/정지, 기동 시 job 복구
│       ├── config.py        # 환경변수 → Settings
│       ├── auth.py          # 인증 미들웨어: 세션 쿠키(로그인 페이지) + Basic 헤더(API), 공개 경로 제외
│       ├── registry.py      # nodes.json 로딩, 토큰 env 해석
│       ├── agent_client.py  # agent 호출 + 예외 분류 (예외를 밖으로 던지지 않음)
│       ├── poller.py        # 주기 헬스체크, 노드별 수집 상태
│       ├── status.py        # 상태 판정 순수 함수
│       ├── actions.py       # 액션 카탈로그 + params 검증
│       ├── jobs.py          # job 생성, 워커, reconcile, retry
│       ├── db.py            # 스키마, 쿼리
│       ├── models.py        # Pydantic 스키마
│       └── static/          # index.html, app.js, style.css
└── agent/
    ├── Dockerfile
    ├── requirements.txt
    └── app/
        ├── main.py          # 라우트, 토큰 검사
        ├── simulator.py     # 메트릭 random walk, 데몬 상태
        ├── actions.py       # 액션 핸들러 (화이트리스트), command_id 결과 캐시
        └── chaos.py         # 지연·오류·blackhole·데몬 중지
```

## 4. 반드시 지킬 것 (MUST)

1. console→agent 호출은 `httpx.Timeout`에 connect/read를 분리해 명시한다. 값은 `SPEC.md §5` 설정에서 읽는다.
2. 노드 단위 호출은 병렬로 한다. 한 노드의 지연이 다른 노드의 수집·실행 시각을 늦추면 안 된다.
3. 노드 단위 호출 함수는 **모든 예외를 내부에서 잡고 결과 객체를 반환**한다. `asyncio.TaskGroup`은 한 태스크의 예외가 형제 태스크를 취소하고, `gather`는 `return_exceptions` 없이 첫 예외를 전파하기 때문이다.
4. 예외는 `SPEC.md §6` 표대로 분류한다. `ConnectTimeout`은 `TimeoutException`의 하위 클래스이므로 Connect 계열을 먼저 잡는다. `except Exception: pass` 금지.
5. `asyncio.create_task`로 만든 백그라운드 태스크는 모듈 레벨 `set`에 참조를 보관하고, done callback에서 제거하면서 예외를 로그로 남긴다.
6. 명령은 `SPEC.md §7` 카탈로그에 있는 것만 허용한다. 파라미터는 enum/검증된 값만 받는다.
7. 명령 요청마다 `command_id`(UUID4)를 발급해 agent로 보낸다. agent는 같은 `command_id`를 재실행하지 않는다.
8. 명령은 자동 재시도하지 않는다. 헬스체크도 재시도하지 않는다 (다음 주기가 재시도).
9. 같은 노드에 대한 명령은 노드별 `asyncio.Lock`으로 직렬화한다. 획득 순서: 노드 락 → 전역 세마포어.
10. poller 루프는 어떤 예외에도 종료되지 않는다. 다음 주기는 사이클 시작 시각 기준으로 계산한다.
11. 시간은 저장·API 모두 UTC ISO8601 (`Z`). 표시 변환은 브라우저에서만.
12. 명령 출력은 `OUTPUT_MAX_BYTES`로 자르고 `output_truncated`를 기록한다.
13. 설정은 전부 환경변수 + 기본값. 호스트·포트·토큰 하드코딩 금지. 프론트는 상대경로(`/api/...`)만 쓴다.
14. 인증은 미들웨어로 건다 (StaticFiles mount까지 보호). 토큰 비교는 `hmac.compare_digest`.
15. 로그는 한 줄 단위 key=value 형식, `node_id` / `job_id` / `command_id`를 포함한다. 토큰·비밀번호는 로그에 남기지 않는다.

## 5. 하지 말 것 (MUST NOT)

- `subprocess(..., shell=True)`, `os.system`, `eval`, `exec` — 문자열을 실행하는 모든 경로
- `requests` 등 동기 HTTP 클라이언트, async 함수 안의 `time.sleep` / 동기 `sqlite3` 호출
- uvicorn `--workers` 2 이상
- requirements에 없는 라이브러리 추가 (필요하면 먼저 제안)
- 프론트엔드 프레임워크, 번들러, CDN, 웹폰트
- `SPEC.md`에 없는 API, 필드, 상태값 임의 추가
- Docker healthcheck에 `curl`/`wget` 사용 (slim 이미지에 없음)

## 6. 작업 방식

- 한 번에 한 단계(§7). 요청에는 단계 목표와 완료 기준이 함께 온다.
- 코드 생성 후 §4 MUST 항목별 준수 여부를 표로 보고한다. 지키지 못한 항목은 이유를 적는다.
- 불확실한 부분은 추측으로 채우지 말고 `# TODO(question): ...` 주석을 남기고 보고한다.
- 기존 파일 수정 시 변경 범위 밖의 코드는 건드리지 않는다.

## 7. 구현 단계와 완료 기준

| 단계 | 범위 | 완료 기준 |
|---|---|---|
| S1 | agent: simulator, `/livez`, `/health`, `/commands`, `/commands/{id}`, `/chaos` | 3개 노드 응답. 같은 `command_id` 2회 POST 시 실행 1회. 토큰 불일치 401 |
| S2 | console: registry, agent_client, poller, status (+ 단위 테스트), 검증용 최소 기동(main의 lifespan·`/healthz`·`GET /api/nodes[/{id}]`, compose console 서비스) | node-c `blackhole` 주입 시 node-a/b의 `last_success_at` 간격 5±1초 유지. node-c는 3회 실패 후 UNREACHABLE |
| S3 | console: actions, jobs, db, reconcile, retry, 기동 시 복구 | `POST /api/jobs` 즉시 202. blackhole 노드는 UNKNOWN, 나머지 SUCCESS. chaos 해제 후 reconcile로 SUCCESS 확인 |
| S4 | 대시보드 4개 탭 | 새로고침 없이 상태 갱신. HIGH 액션 확인 모달. console 중지 시 연결 끊김 배너 |
| S5 | compose, auth(로그인 페이지 + 세션), `.env.example`, AWS 배포 | `docker compose up -d --build` 한 번으로 기동. 외부에서는 console 포트만 열림. 인증 없이 `/` → 302 `/login`, `/api/*` 401. 로그인 후 대시보드, 로그아웃 후 다시 302. 연속 실패 5회 → 429 |
| S6a | 판정을 poller로 이동, 상태 전이 이벤트 저장(`node_events`), `GET /api/events`, 이벤트 타임라인 탭 (+ 단위 테스트) | node-c blackhole → `CRITICAL → UNREACHABLE` 1건, 해제 → `UNREACHABLE → CRITICAL` 1건. 상태 불변 주기에는 기록 없음. console 재기동 후에도 이력 유지, 첫 판정은 `from_status = null`. poller 정지 시 API는 UNREACHABLE(판정 갱신 중단) |

## 8. 커밋 규칙

- AI 생성 원본은 **수정 전에 그대로 먼저 커밋**한다: `ai(S2): poller 초안`
- 엔지니어 보정은 별도 커밋: `fix(S2): 순차 폴링 → 노드별 병렬 + 타임아웃 분리`
- 문서: `docs: ...`
- `history.md` 트러블슈팅 사례는 두 커밋 해시로 Before/After를 연결한다.
