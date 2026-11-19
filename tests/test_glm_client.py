"""GLM 연결부 테스트. 기본은 가짜 실행기로 돈다. 실제 호출은 NETRUNNER_GLM=1일 때만."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from netrunner.breaker import CompletionError
from netrunner.glm_client import DEFAULT_AGENT, DEFAULT_MODEL, OpencodeComplete, extract_text

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "opencode_sandbox.json"


def _text_event(text: str) -> str:
    return json.dumps({"type": "text", "part": {"type": "text", "text": text}})


class FakeRun:
    """subprocess.run 대신 받아 호출 인자를 기록하고 미리 정한 결과를 돌려준다."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "", exc=None):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.exc = exc
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def _complete(run: FakeRun) -> OpencodeComplete:
    return OpencodeComplete(binary="opencode", root=ROOT, config=CONFIG, run=run)


def test_returns_joined_text_parts():
    stdout = "\n".join(
        [
            json.dumps({"type": "step_start"}),
            _text_event("안"),
            _text_event("녕"),
            json.dumps({"type": "step_finish"}),
        ]
    )
    run = FakeRun(stdout=stdout)
    assert _complete(run)("안녕하세요") == "안녕"


def test_passes_prompt_on_stdin_with_agent_model_and_sandbox_config():
    run = FakeRun(stdout=_text_event("ok"))
    _complete(run)("긴 프롬프트")
    call = run.calls[0]
    assert call["input"] == "긴 프롬프트"
    assert call["cwd"] == ROOT
    assert call["env"]["OPENCODE_CONFIG"] == str(CONFIG.resolve())
    argv = call["argv"]
    assert argv[argv.index("--agent") + 1] == DEFAULT_AGENT
    assert argv[argv.index("-m") + 1] == DEFAULT_MODEL
    assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
    assert "prompt" not in " ".join(argv)


def test_runs_a_private_server_and_never_auto_approves_permissions():
    run = FakeRun(stdout=_text_event("ok"))
    _complete(run)("x")
    argv = run.calls[0]["argv"]
    assert "--standalone" in argv
    assert "--auto" not in argv


def test_child_environment_drops_secret_looking_variables(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "값은-여기-없음")
    monkeypatch.setenv("DEMO_API_KEY", "값은-여기-없음")
    monkeypatch.setenv("LC_ALL", "ko_KR.UTF-8")
    run = FakeRun(stdout=_text_event("ok"))
    _complete(run)("x")
    env = run.calls[0]["env"]
    assert "CLAUDE_CODE_MESSAGING_TOKEN" not in env
    assert "DEMO_API_KEY" not in env
    assert "PATH" in env and "HOME" in env and "LC_ALL" in env
    assert env["OPENCODE_CONFIG"] == str(CONFIG.resolve())


def test_nonzero_exit_raises_with_stderr():
    run = FakeRun(stdout="", returncode=2, stderr="인증 실패")
    with pytest.raises(CompletionError, match="종료 코드 2.*인증 실패"):
        _complete(run)("x")


def test_timeout_raises_completion_error():
    run = FakeRun(exc=subprocess.TimeoutExpired(cmd="opencode", timeout=5))
    with pytest.raises(CompletionError, match="시간 초과"):
        _complete(run)("x")


def test_missing_binary_raises_completion_error():
    run = FakeRun(exc=FileNotFoundError("opencode"))
    with pytest.raises(CompletionError, match="실행 실패"):
        _complete(run)("x")


def test_error_event_raises_completion_error():
    stdout = json.dumps({"type": "error", "error": {"message": "Agent not found"}})
    with pytest.raises(CompletionError, match="모델 오류"):
        extract_text(stdout)


def test_empty_response_raises_completion_error():
    with pytest.raises(CompletionError, match="비어 있음"):
        extract_text(json.dumps({"type": "step_finish"}))


def test_non_json_noise_lines_are_ignored():
    stdout = "warning: something\n" + _text_event("답")
    assert extract_text(stdout) == "답"


@pytest.mark.skipif(os.environ.get("NETRUNNER_GLM") != "1", reason="실제 GLM 호출 생략")
def test_real_call_roundtrip():
    binary = os.environ.get("NETRUNNER_OPENCODE", "opencode")
    complete = OpencodeComplete(binary=binary, root=ROOT, config=CONFIG)
    assert complete("한 단어로만 답해: 안녕").strip()
