"""액션 카탈로그 + params 검증 (SPEC §7).

카탈로그의 원본은 console이며 GET /api/actions로 제공한다. UI는 이 응답으로 폼을 만든다.
파라미터는 enum 값만 허용한다 (console 1차 검증, agent 2차 검증).
"""
from __future__ import annotations

from typing import Any

DAEMONS = ["pacs-gateway", "hl7-interface", "emr-sync"]

CATALOG: list[dict[str, Any]] = [
    {
        "action": "CLEAN_LOGS",
        "name": "로그 정리",
        "risk": "LOW",
        "params": [
            {"name": "older_than_days", "label": "보관 기간(일) 초과 삭제", "choices": [1, 3, 7], "default": 7},
        ],
    },
    {
        "action": "FLUSH_CACHE",
        "name": "캐시 플러시",
        "risk": "MEDIUM",
        "params": [],
    },
    {
        "action": "RESTART_DAEMON",
        "name": "데몬 재시작",
        "risk": "HIGH",
        "params": [
            {"name": "daemon", "label": "대상 데몬", "choices": DAEMONS, "default": None},
        ],
    },
    {
        "action": "COLLECT_DIAG",
        "name": "진단 정보 수집",
        "risk": "NONE",
        "params": [],
    },
]

_BY_ACTION = {a["action"]: a for a in CATALOG}


class InvalidAction(ValueError):
    pass


def validate(action: str, params: dict) -> dict:
    """허용되지 않은 action/params면 InvalidAction. 기본값을 채운 params를 돌려준다."""
    spec = _BY_ACTION.get(action)
    if spec is None:
        raise InvalidAction(f"unknown action: {action!r}")
    allowed = {p["name"]: p for p in spec["params"]}
    unknown = set(params) - set(allowed)
    if unknown:
        raise InvalidAction(f"unknown params for {action}: {sorted(unknown)}")
    out: dict = {}
    for name, p in allowed.items():
        if name not in params:
            if p["default"] is None:
                raise InvalidAction(f"missing param for {action}: {name}")
            out[name] = p["default"]
            continue
        value = params[name]
        choices = p["choices"]
        # bool은 int의 하위 타입이므로 True == 1 로 통과하지 않게 막는다.
        if isinstance(value, bool) or value not in choices or type(value) is not type(choices[0]):
            raise InvalidAction(f"invalid value for {action}.{name}: {value!r} (allowed: {choices})")
        out[name] = value
    return out
