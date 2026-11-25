"""Breaker 샌드박스 설정이 도구를 열지 않는지 확인한다.

Breaker는 프롬프트에 필요한 정보를 모두 받으므로 파일을 읽거나 고칠 필요가 없다.
정답 파일(시나리오의 장애와 note)과 기록 파일을 모델이 직접 읽거나 덮어쓰지
못하도록, 허용 규칙을 하나도 두지 않는다. 필요한 도구가 생기면 이 테스트를
고치면서 이유를 PR 본문에 적는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "opencode_sandbox.json"
DIAGNOSER_CONFIG = ROOT / "config" / "opencode_diagnoser.json"
DEV_CONFIG = ROOT / "opencode.json"
# 정답이 든 경로. 파일을 읽고, 찾고, 고치는 도구마다 거부해야 한다.
# grep은 권한 대상이 경로가 아니라 검색 문자열이라 경로 규칙이 걸리지 않는다
# (control experiment로 확인). 그래서 grep은 경로 규칙 대신 전면 거부한다.
ANSWER_PATHS = (
    "fixtures/**/*.scenario.json",
    "runs/**",
    "captures/**",
    "artifacts/benchmark-runs/**",
)
PATH_TOOLS = ("read", "glob", "edit")


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


# opencode 2.3.0이 권한 규칙 키로 받는 이름 가운데 확인한 목록이다. 전체 목록은 아니다.
# 여기 없는 이름을 쓰면 설정이 조용히 무시될 수 있으니, 추가하기 전에 바이너리로 확인한다.
KNOWN_ACTIONS = frozenset(
    {
        "*",
        "browser",
        "doom_loop",
        "edit",
        "external_directory",
        "glob",
        "grep",
        "question",
        "read",
        "shell",
        "skill",
        "subagent",
        "webfetch",
        "websearch",
    }
)


def test_every_permission_key_is_a_known_opencode_action():
    assert set(_permission()) <= KNOWN_ACTIONS


def _diagnoser_permission() -> dict:
    data = json.loads(DIAGNOSER_CONFIG.read_text(encoding="utf-8"))
    return data["agent"]["diagnoser"]["permission"]


def _dev_permission() -> dict:
    return json.loads(DEV_CONFIG.read_text(encoding="utf-8"))["permission"]


def test_diagnoser_agent_denies_every_tool_including_shell():
    perm = _diagnoser_permission()
    assert perm["*"] == "deny"
    assert all(rule == "deny" for rule in perm.values())
    assert {"read", "glob", "grep", "edit", "shell"} <= set(perm)
    assert set(perm) <= KNOWN_ACTIONS


@pytest.mark.parametrize("tool", PATH_TOOLS)
@pytest.mark.parametrize("path", ANSWER_PATHS)
def test_dev_agents_deny_answer_paths_for_every_path_tool(tool, path):
    assert _dev_permission()[tool][path] == "deny"


def test_dev_agents_deny_grep_because_its_rule_is_not_path_based():
    assert _dev_permission()["grep"] == "deny"


@pytest.mark.parametrize("tool", ("read", "glob"))
@pytest.mark.parametrize("folder", ("runs", "captures", "artifacts/benchmark-runs"))
def test_dev_agents_deny_answer_folder_names_for_pattern_tools(tool, folder):
    # "runs/**" 규칙은 "runs" 자체를 가리키는 호출에 걸리지 않는다(control experiment로 확인).
    assert _dev_permission()[tool][folder] == "deny"
