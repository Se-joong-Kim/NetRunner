"""외부 프로세스 환경 허용 목록(child_env)과 Docker 호출 지점의 적용 테스트."""

from __future__ import annotations

import subprocess

from netrunner import child_env, docker_lab
from netrunner.child_env import child_environment, is_allowed

WINDOWS_SAMPLE = {
    "SYSTEMROOT": r"C:\Windows",
    "COMSPEC": r"C:\Windows\system32\cmd.exe",
    "USERPROFILE": r"C:\Users\x",
}

SAMPLE = {
    "PATH": "/usr/bin",
    "HOME": "/home/x",
    "LC_ALL": "C",
    "XDG_RUNTIME_DIR": "/run/x",
    "DOCKER_HOST": "unix:///var/run/docker.sock",
    "CLAUDE_CODE_MESSAGING_TOKEN": "sample-1",
    "DEMO_API_KEY": "sample-2",
    "LC_TOKEN": "sample-3",
    "XDG_API_KEY": "sample-4",
    "AWS_SECRET_ACCESS_KEY": "sample-5",
    "GITHUB_PASSWORD": "sample-6",
    "RANDOM_VAR": "sample-7",
}


def test_listed_names_and_prefixes_pass_through():
    kept = child_environment(SAMPLE)
    assert set(kept) == {"PATH", "HOME", "LC_ALL", "XDG_RUNTIME_DIR", "DOCKER_HOST"}


def test_secret_markers_block_names_even_under_allowed_prefixes_and_elsewhere():
    kept = child_environment(SAMPLE)
    for name in (
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "DEMO_API_KEY",
        "LC_TOKEN",
        "XDG_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_PASSWORD",
    ):
        assert name not in kept
        assert not is_allowed(name)


def test_unlisted_names_are_dropped():
    assert "RANDOM_VAR" not in child_environment(SAMPLE)


def test_extra_values_are_added_without_name_check():
    assert child_environment({}, {"OPENCODE_CONFIG": "/p"}) == {"OPENCODE_CONFIG": "/p"}


def test_windows_system_names_pass_only_on_windows(monkeypatch):
    monkeypatch.setattr(child_env, "IS_WINDOWS", False)
    assert child_environment(WINDOWS_SAMPLE) == {}
    monkeypatch.setattr(child_env, "IS_WINDOWS", True)
    # USERPROFILE은 아직 넣지 않는다(실제 GLM 호출로 필요성을 확인한 뒤 추가).
    assert set(child_environment(WINDOWS_SAMPLE)) == {"SYSTEMROOT", "COMSPEC"}


def test_docker_commands_get_only_the_allowed_environment(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(docker_lab.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_lab.os, "environ", SAMPLE)
    docker_lab.DockerLab()._run("client", "true")
    env = seen["env"]
    assert "PATH" in env and "DOCKER_HOST" in env
    assert "CLAUDE_CODE_MESSAGING_TOKEN" not in env
    assert "RANDOM_VAR" not in env
