"""환경변수 → Settings (SPEC §5, §4.3). 모든 값은 env로 덮어쓸 수 있다."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

_DEFAULT_NODES_FILE = Path(__file__).resolve().parent.parent / "nodes.json"


class Settings(BaseSettings):
    # 대시보드·API HTTP Basic 인증 (SPEC §2.2). 데모 배포 시 비밀번호 변경.
    admin_user: str = "admin"
    admin_password: str = Field(default="nodewatch", repr=False)

    # 노드 레지스트리 (SPEC §2.1)
    nodes_file: Path = _DEFAULT_NODES_FILE
    # 저장소 (SPEC §9)
    db_path: Path = Path("/data/nodewatch.db")

    # 타임아웃·동시성 예산 (SPEC §5)
    poll_interval_sec: float = 5.0
    health_connect_timeout: float = 1.0
    health_read_timeout: float = 3.0
    slow_ms: int = 1500
    poll_concurrency: int = 20
    fail_threshold: int = 3
    stale_factor: float = 3.0
    cmd_connect_timeout: float = 2.0
    cmd_read_timeout: float = 15.0
    job_concurrency: int = 10
    output_max_bytes: int = 65536
    samples_max: int = Field(default=60, ge=1, le=17280)  # 노드별 메모리 샘플 수 (60 × 5s = 5분)
    events_retention_days: int = Field(default=30, ge=1)  # 상태 전이 이벤트 보관 기간 (SPEC §4.4)

    # 메트릭 임계치 (SPEC §4.3)
    cpu_warn_pct: float = 80
    cpu_crit_pct: float = 95
    mem_warn_pct: float = 85
    mem_crit_pct: float = 95
    disk_warn_pct: float = 80
    disk_crit_pct: float = 90

    def thresholds(self) -> dict[str, tuple[float, float]]:
        """metrics 키 → (WARNING, CRITICAL)"""
        return {
            "cpu_pct": (self.cpu_warn_pct, self.cpu_crit_pct),
            "mem_pct": (self.mem_warn_pct, self.mem_crit_pct),
            "disk_pct": (self.disk_warn_pct, self.disk_crit_pct),
        }
