"""Breaker 샌드박스 설정이 도구를 열지 않는지 확인한다.

Breaker는 프롬프트에 필요한 정보를 모두 받으므로 파일을 읽거나 고칠 필요가 없다.
정답 파일(시나리오의 장애와 note)과 기록 파일을 모델이 직접 읽거나 덮어쓰지
못하도록, 허용 규칙을 하나도 두지 않는다. 필요한 도구가 생기면 이 테스트를
고치면서 이유를 PR 본문에 적는다.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "opencode_sandbox.json"


def _permission() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))["agent"]["netrunner"]["permission"]


def _grants(node) -> bool:
    """허용 규칙(allow)이 어디에든 있는지 본다. 중첩된 경로별 규칙도 포함한다."""
    if isinstance(node, dict):
        return any(_grants(v) for v in node.values())
    return node == "allow"


def test_breaker_agent_grants_no_tool_permission():
    assert not _grants(_permission())


def test_unlisted_tools_are_denied_by_default():
    assert _permission()["*"] == "deny"
