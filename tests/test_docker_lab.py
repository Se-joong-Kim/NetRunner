import os
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from netrunner.contracts import FaultSpec, LabResult, Scenario, read_json
from netrunner.__main__ import cmd_lab_check
from netrunner.diagnoser import ReplayDiagnoser
from netrunner.docker_lab import DockerLab, ROOT
from netrunner.referee import Referee
from netrunner.validate import LabInfo


def test_invalid_faults_and_urls_never_run_commands(monkeypatch):
    lab = DockerLab()
    monkeypatch.setattr(lab, "_run", lambda *args, **kwargs: pytest.fail("unexpected command"))
    assert not lab.inject(FaultSpec("D2", {"domain": "shop.lab", "ip": "8.8.8.8"})).ok
    assert not lab.inject(FaultSpec("D2", {"domain": "shop.lab", "ip": "10.20.0.10"})).ok
    assert not lab.inject(FaultSpec("T2", {"port": 443})).ok
    assert not lab.inject(FaultSpec("N1", {"delay_ms": 100})).ok
    assert not lab.health_check("http://example.com/").ok
    assert not lab.health_check("http://shop.lab:0/").ok


def test_reset_attempts_firewall_even_if_dns_fails(monkeypatch):
    lab = DockerLab()
    calls = []
    monkeypatch.setattr(lab, "_set_dns", lambda: {"exit_code": 1})
    monkeypatch.setattr(lab, "_run", lambda *args, **kw: calls.append(args) or {"exit_code": 0})
    assert not lab.reset().ok
    assert calls == [("client", "sh", "/stop-capture.sh"), ("web", "iptables", "-F", "NETRUNNER")]


def test_timeout_preserves_partial_output(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output=b"partial output", stderr=b"partial error")
    monkeypatch.setattr(subprocess, "run", timeout)
    result = DockerLab()._run("client", "curl")
    assert result["exit_code"] is None
    assert result["stdout"] == "partial output" and result["stderr"] == "partial error"


def test_dns_waits_for_expected_answers(monkeypatch):
    lab = DockerLab()
    monkeypatch.setattr(lab, "_run", lambda *a, **kw: {"exit_code": 0, "stderr": ""})
    answers = iter(["10.20.0.10", "10.20.0.10", "10.20.0.99", "10.20.0.10"])
    monkeypatch.setattr(lab, "_dns", lambda *a: {"exit_code": 0, "stdout": next(answers)})
    assert lab._set_dns("shop.lab", "10.20.0.99")["exit_code"] == 0


def test_capture_numbers_packets_and_always_cleans_up(monkeypatch):
    lab = DockerLab()
    calls = []
    def run(service, *args, **kwargs):
        calls.append(args)
        if args[:2] == ("sh", "-c"):
            return {"exit_code": 0, "stdout": "    1 packet one\n    2 packet two\n", "stderr": ""}
        return {"exit_code": 0}
    monkeypatch.setattr(lab, "_run", run)
    result, _ = lab._capture("http://shop.lab/")
    assert result["packets"] == 2
    assert result["output"] == "#1 packet one\n#2 packet two\n"
    assert calls[-1] == ("sh", "/stop-capture.sh")


def test_lab_check_recovers_dirty_start(monkeypatch, tmp_path):
    scenario_dir = tmp_path / "input"
    scenario_dir.mkdir()
    source = ROOT / "fixtures/first_integration/r2_tcp_refused.scenario.json"
    (scenario_dir / source.name).write_text(source.read_text())
    class FakeLab:
        def __init__(self, config):
            self.checks = iter([False, True, True])
        def health_check(self, url):
            return LabResult(next(self.checks))
        def inject(self, fault):
            return LabResult(True)
        def verify_symptom(self, scenario):
            return LabResult(True)
        def reset(self):
            return LabResult(True)
    monkeypatch.setattr("netrunner.__main__.DockerLab", FakeLab)
    args = SimpleNamespace(lab=ROOT / "config/lab.json", dir=scenario_dir, out=tmp_path, repeat=1)
    assert cmd_lab_check(args) == 0
    steps = json.loads(next(tmp_path.glob("*.jsonl")).read_text())["steps"]
    assert not steps["health_check_initial"]["ok"] and steps["health_check"]["ok"]


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
@pytest.mark.parametrize("filename", ["r1_dns_old_ip", "r2_tcp_refused"])
def test_real_lab_recovers_after_each_fault(filename):
    lab = DockerLab()
    path = ROOT / "fixtures" / "first_integration" / f"{filename}.scenario.json"
    scenario = Scenario.from_dict(read_json(path))
    try:
        assert lab.health_check(scenario.target_url).ok
        assert lab.inject(scenario.faults[0]).ok
        result = lab.verify_symptom(scenario)
        assert result.ok, result.observations
        assert Path(result.observations["pcap"]).is_file()
        assert result.observations["capture"]["packets"] > 0
        assert result.observations["capture"]["output"].startswith("#1 ")
    finally:
        assert lab.reset().ok
        assert lab.health_check(scenario.target_url).ok


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
def test_referee_runs_real_lab_with_replayed_diagnosis():
    fixtures = ROOT / "fixtures" / "first_integration"
    scenario = Scenario.from_dict(read_json(fixtures / "r2_tcp_refused.scenario.json"))
    replay = ReplayDiagnoser([read_json(fixtures / "r2_tcp_refused.diagnosis.json")])
    referee = Referee(DockerLab(), replay, LabInfo.load(ROOT / "config" / "lab.json"))
    record = referee.run_round(scenario)
    assert record["status"] == "scored", record["lab_log"]
    assert record["reset_verified"]
    assert next(step for step in record["lab_log"] if step["step"] == "verify_symptom")["ok"]


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
def test_capture_timeout_stops_remote_process(monkeypatch):
    lab = DockerLab()
    original = lab._run
    def run(service, *args, **kwargs):
        if args[:2] == ("sh", "-c") and "setsid" in args[2]:
            kwargs["timeout"] = 1
        return original(service, *args, **kwargs)
    monkeypatch.setattr(lab, "_run", run)
    result, _ = lab._capture("http://shop.lab/")
    assert result["exit_code"] is None
    assert result["cleanup"]["exit_code"] == 0
    assert original("client", "sh", "-c", "[ ! -f /tmp/netrunner-capture.pid ]")["exit_code"] == 0
    processes = original("client", "ps", "-o", "comm")
    assert processes["exit_code"] == 0 and "tcpdump" not in processes["stdout"].split()
    assert lab.reset().ok


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
def test_partial_injection_failure_is_recovered(monkeypatch):
    lab = DockerLab()
    original = lab._run
    def run(service, *args, **kwargs):
        result = original(service, *args, **kwargs)
        if service == "web" and args[:2] == ("iptables", "-A"):
            assert result["exit_code"] == 0
            result["exit_code"] = 1  # 실제 주입 후 명령 실패가 보고되는 상황.
        return result
    monkeypatch.setattr(lab, "_run", run)
    scenario = Scenario.from_dict(read_json(ROOT / "fixtures/first_integration/r2_tcp_refused.scenario.json"))
    record = Referee(lab, ReplayDiagnoser([]), LabInfo.load(ROOT / "config/lab.json")).run_round(scenario)
    assert record["status"] == "invalid" and record["reset_verified"]
    assert lab.health_check(scenario.target_url).ok
