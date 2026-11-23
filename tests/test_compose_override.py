"""Windows 덮어쓰기 파일(compose.windows.yaml)과 실험망 compose 인자 목록을 확인한다(#47)."""

from __future__ import annotations

import subprocess

from netrunner import docker_lab
from netrunner.docker_lab import ROOT, DockerLab


def test_only_windows_adds_the_override_file():
    assert docker_lab.compose_files(windows=False) == [ROOT / "compose.yaml"]
    assert docker_lab.compose_files(windows=True) == [
        ROOT / "compose.yaml",
        ROOT / "compose.windows.yaml",
    ]


def test_windows_override_only_turns_off_the_internal_flag():
    lines = [
        line.strip()
        for line in (ROOT / "compose.windows.yaml").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert [line for line in lines if "internal" in line] == ["internal: false"] * 2
    assert not any(line.startswith("services") for line in lines)


def test_client_capture_drops_to_nobody():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    client = compose.split("  client:\n", 1)[1].split("  dns:\n", 1)[0]
    backend = compose.split("  backend:\n", 1)[1].split("  blockpage:\n", 1)[0]
    capture = (ROOT / "lab" / "capture.sh").read_text(encoding="utf-8")
    start = (ROOT / "lab" / "start.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "lab" / "Dockerfile").read_text(encoding="utf-8")
    assert "init: true" not in client and "init: true" not in backend
    assert start.count("exec su-exec nobody tini -- sleep infinity") == 2
    assert "tini" in dockerfile
    assert "-Z nobody" in capture
    assert 'su-exec nobody kill -0 "$capture_pid"' in capture
    assert 'su-exec nobody kill -TERM "$capture_pid"' in capture
    assert "su-exec nobody tcpdump --number" in capture


def test_docker_commands_pass_the_compose_files_for_the_platform(monkeypatch):
    seen: list = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(docker_lab.subprocess, "run", fake_run)
    lab = DockerLab()
    monkeypatch.setattr(docker_lab, "IS_WINDOWS", False)
    lab._run("client", "true")
    monkeypatch.setattr(docker_lab, "IS_WINDOWS", True)
    lab._run("client", "true")
    base = str(ROOT / "compose.yaml")
    override = str(ROOT / "compose.windows.yaml")
    assert seen[0] == ["docker", "compose", "-f", base, "exec", "-T", "client", "true"]
    assert seen[1] == [
        "docker",
        "compose",
        "-f",
        base,
        "-f",
        override,
        "exec",
        "-T",
        "client",
        "true",
    ]
