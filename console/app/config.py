"""환경변수 → Settings (SPEC §5, §4.3). 모든 값은 env로 덮어쓸 수 있다."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

_DEFAULT_NODES_FILE = Path(__file__).resolve().parent.parent / "nodes.json"

# 로컬 개발용 기본 비밀번호의 scrypt 해시 (평문: nodewatch / monwatch). 기동 시 기본값이면 경고한다.
# 배포 시 python -m app.auth hash 로 만든 값을 .env에 넣는다.
DEFAULT_ADMIN_PASSWORD_HASH = "scrypt:16384:8:1:aoDLaDST1sgcQP-CvnOoaQ:5SoEo6qsVFhuLBADNWU3fVrjOASDC6ce6vVJOaMJ-T0"
DEFAULT_MONITOR_PASSWORD_HASH = "scrypt:16384:8:1:XR6RbF9eIJXh4vFWfHDz9w:XIQnORzfQw7P0uB98lhLYd-ZVhmzFplqtT1Chk3isac"
DEFAULT_PASSWORDS = {"admin": "nodewatch", "monitor": "monwatch"}


class Settings(BaseSettings):
    # 계정 (SPEC §2.2, §8). 비밀번호는 scrypt 해시로만 받는다.
    admin_user: str = "admin"
    admin_password_hash: str = Field(default=DEFAULT_ADMIN_PASSWORD_HASH, repr=False)
    monitor_user: str = "monuser"  # 빈 값이면 비활성
    monitor_password_hash: str = Field(default=DEFAULT_MONITOR_PASSWORD_HASH, repr=False)
    session_ttl_sec: int = Field(default=28800, ge=60)   # 로그인 세션 절대 만료 (8시간)
    cookie_secure: bool = False                          # HTTPS 뒤에 둘 때 true
    login_max_failures: int = Field(default=5, ge=1)     # IP별 연속 실패 n회 →
    login_lockout_sec: int = Field(default=60, ge=1)     # n초 차단 (429)

    # 노드 레지스트리 (SPEC §2.1)
    nodes_file: Path = _DEFAULT_NODES_FILE
    # 저장소 (SPEC §9)
    db_path: Path = Path("/data/nodewatch.db")

    # 타임아웃·동시성 예산 (SPEC §5). 범위를 벗어나면 기동을 거부한다 (§5 규칙 7):
    # 동시성 0은 수집·job을 영구 대기시키고, 주기 0은 busy loop, 타임아웃 0은 모든 호출을 실패시킨다.
    poll_interval_sec: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    health_connect_timeout: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    health_read_timeout: float = Field(default=3.0, gt=0, allow_inf_nan=False)
    slow_ms: int = Field(default=1500, ge=1)
    poll_concurrency: int = Field(default=20, ge=1)
    fail_threshold: int = Field(default=3, ge=1)
    stale_factor: float = Field(default=3.0, gt=0, allow_inf_nan=False)
    # idle 연결 재사용 상한. 수집 주기 < 이 값 < agent keep-alive(30s) (SPEC §5)
    http_keepalive_expiry: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    cmd_connect_timeout: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    cmd_read_timeout: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    job_concurrency: int = Field(default=10, ge=1)
    output_max_bytes: int = Field(default=65536, ge=1)
    samples_max: int = Field(default=60, ge=1, le=17280)  # 노드별 메모리 샘플 수 (60 × 5s = 5분)
    events_retention_days: int = Field(default=30, ge=1)  # 상태 전이 이벤트 보관 기간 (SPEC §4.4)

    # 메트릭 임계치 (SPEC §4.3). 0~100, WARNING < CRITICAL
    cpu_warn_pct: float = Field(default=80, ge=0, le=100)
    cpu_crit_pct: float = Field(default=95, ge=0, le=100)
    mem_warn_pct: float = Field(default=85, ge=0, le=100)
    mem_crit_pct: float = Field(default=95, ge=0, le=100)
    disk_warn_pct: float = Field(default=80, ge=0, le=100)
    disk_crit_pct: float = Field(default=90, ge=0, le=100)

    @model_validator(mode="before")
    @classmethod
    def _empty_means_default(cls, data: Any) -> Any:
        # compose는 .env에 없는 변수를 ${VAR:-}로 빈 문자열을 넘긴다 (SPEC §2.2). 빈 값은 '설정 안 함'으로 보고
        # 기본값을 쓴다. 단 MONITOR_USER는 빈 값이 '모니터링 계정 비활성'을 뜻하므로 그대로 둔다.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (v == "" and k != "monitor_user")}
        return data

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> Settings:
        for key, (warn, crit) in self.thresholds().items():
            if not warn < crit:
                raise ValueError(f"{key}: WARNING threshold ({warn}) must be lower than CRITICAL ({crit})")
        return self

    def thresholds(self) -> dict[str, tuple[float, float]]:
        """metrics 키 → (WARNING, CRITICAL)"""
        return {
            "cpu_pct": (self.cpu_warn_pct, self.cpu_crit_pct),
            "mem_pct": (self.mem_warn_pct, self.mem_crit_pct),
            "disk_pct": (self.disk_warn_pct, self.disk_crit_pct),
        }
