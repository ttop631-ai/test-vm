"""nodes.json 로딩, 토큰 env 해석 (SPEC §2.1). 토큰 값은 파일에 두지 않는다."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    url: str
    token: str = field(repr=False)  # 로그·repr에 노출하지 않는다


class RegistryError(RuntimeError):
    pass


def load_nodes(path: Path) -> dict[str, Node]:
    """등록 순서를 유지한 {node_id: Node}. 형식 오류나 토큰 누락이면 기동을 막는다."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RegistryError(f"cannot load node registry {path}: {e}") from e
    if not isinstance(raw, list) or not raw:
        raise RegistryError(f"node registry {path} must be a non-empty list")

    nodes: dict[str, Node] = {}
    for i, item in enumerate(raw):
        try:
            node_id, name, url, token_env = item["id"], item["name"], item["url"], item["token_env"]
        except (KeyError, TypeError) as e:
            raise RegistryError(f"node registry entry #{i} is missing field {e}") from e
        if node_id in nodes:
            raise RegistryError(f"duplicate node id: {node_id}")
        token = os.environ.get(token_env, "")
        if not token:
            raise RegistryError(f"env {token_env} for {node_id} is not set")
        nodes[node_id] = Node(id=node_id, name=name, url=url.rstrip("/"), token=token)
    return nodes
