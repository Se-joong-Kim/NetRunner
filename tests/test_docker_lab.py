import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from netrunner.__main__ import cmd_lab_check
from netrunner.contracts import FaultSpec, LabResult, Scenario, read_json
from netrunner.diagnoser import ReplayDiagnoser
from netrunner.docker_lab import ROOT, DockerLab
from netrunner.faults import FAULTS
from netrunner.referee import Referee
from netrunner.tools import missing_parts
from netrunner.validate import LabInfo


def test_lab_scripts_keep_lf_line_endings():
    # Windows(core.autocrlf=true)에서 CRLF로 풀리면 컨테이너의 sh가
    # "set: illegal option -"로 바로 종료된다.
    scripts = [*(ROOT / "lab").glob("*.sh"), ROOT / "lab" / "Dockerfile"]
    assert [p.name for p in scripts if b"\r" in p.read_bytes()] == []


def test_config_enables_all_sixteen_faults():
    assert set(DockerLab().lab_info.enabled_faults) == set(FAULTS)


def test_invalid_faults_and_urls_never_run_commands(monkeypatch):
    lab = DockerLab()
    monkeypatch.setattr(lab, "_run", lambda *args, **kwargs: pytest.fail("unexpected command"))
    assert not lab.inject(FaultSpec("D2", {"domain": "shop.lab", "ip": "8.8.8.8"})).ok
    assert not lab.inject(FaultSpec("D2", {"domain": "shop.lab", "ip": "10.20.0.10"})).ok
    assert not lab.inject(FaultSpec("T2", {"port": 443})).ok
    assert not lab.inject(FaultSpec("T1", {"port": True})).ok
    assert not lab.inject(FaultSpec("N1", {"delay_ms": 19})).ok
    assert not lab.inject(FaultSpec("N1", {"delay_ms": True})).ok
    assert not lab.inject(FaultSpec("D4", {"delay_ms": 99})).ok
    assert not lab.inject(FaultSpec("N4", {"rate_kbit": 10000.5})).ok
    assert not lab.inject(FaultSpec("B1", {"domain": "example.com"})).ok
    assert not lab.inject(FaultSpec("B2", {"domain": "shop.lab", "block_ip": "10.20.0.99"})).ok
    assert not lab.health_check("http://example.com/").ok
    assert not lab.health_check("http://shop.lab:0/").ok


def test_d2_allows_other_lab_hosts(monkeypatch):
    lab = DockerLab()
    calls = []
    monkeypatch.setattr(
        lab, "_set_dns", lambda domain, ip: calls.append((domain, ip)) or {"exit_code": 0}
    )
    for ip in (lab.hosts["dns"], lab.hosts["alt_dns"], lab.hosts["router"]):
        assert lab.inject(FaultSpec("D2", {"domain": "shop.lab", "ip": ip})).ok
    assert calls == [("shop.lab", lab.hosts[name]) for name in ("dns", "alt_dns", "router")]


def test_d1_validates_domain_before_injection(monkeypatch):
    lab = DockerLab()
    calls = []
    monkeypatch.setattr(lab, "_set_dns", lambda domain: calls.append(domain) or {"exit_code": 0})
    assert not lab.inject(FaultSpec("D1", {"domain": "example.com"})).ok
    assert lab.inject(FaultSpec("D1", {"domain": "shop.lab"})).ok
    assert calls == ["shop.lab"]


def test_reset_attempts_firewall_even_if_dns_fails(monkeypatch):
    lab = DockerLab()
    calls = []
    monkeypatch.setattr(lab, "_set_dns", lambda: {"exit_code": 1})
    monkeypatch.setattr(lab, "_set_cert", lambda cert: {"exit_code": 0})
    monkeypatch.setattr(lab, "_set_redirect", lambda enabled: {"exit_code": 0})
    monkeypatch.setattr(lab, "_run", lambda *args, **kw: calls.append(args) or {"exit_code": 0})
    assert not lab.reset().ok
    assert calls[0] == ("client", "sh", "/stop-capture.sh")
    assert calls[1] == ("client", "sh", "/stop-load.sh")
    assert calls[2][:3] == ("client", "sh", "-c")
    assert ("router", "iptables", "-F", "NETRUNNER") in calls
    assert ("web", "iptables", "-F", "NETRUNNER") in calls
    assert any(call[:2] == ("backend", "sh") for call in calls)


def test_d3_accepts_no_parameters(monkeypatch):
    lab = DockerLab()
    calls = []
    monkeypatch.setattr(lab, "_run", lambda *args, **kw: calls.append(args) or {"exit_code": 0})
    assert not lab.inject(FaultSpec("D3", {"x": 1})).ok
    assert lab.inject(FaultSpec("D3", {})).ok
    assert calls == [
        (
            "router",
            "iptables",
            "-A",
            "NETRUNNER",
            "-p",
            "udp",
            "-d",
            lab.hosts["dns"],
            "--dport",
            "53",
            "-j",
            "DROP",
        )
    ]


def test_timeout_preserves_partial_output(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0], 1, output=b"partial output", stderr=b"partial error"
        )

    monkeypatch.setattr(subprocess, "run", timeout)
    result = DockerLab()._run("client", "curl")
    assert result["exit_code"] is None
    assert result["stdout"] == "partial output" and result["stderr"] == "partial error"


def test_probe_commands_and_failures(monkeypatch):
    lab = DockerLab()
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[1] == "dig":
            return {
                "exit_code": 0,
                "stdout": "ANSWER: 1\nQuery time: 3 msec",
                "stderr": "",
                "elapsed_ms": 4,
            }
        return {
            "exit_code": 7,
            "stdout": (
                "http_code=000\ntime_namelookup=0\ntime_connect=0\ntime_appconnect=0\n"
                "time_starttransfer=0\ntime_total=0"
            ),
            "stderr": "Connection refused",
            "elapsed_ms": 5,
        }

    monkeypatch.setattr(lab, "_run", run)
    dig = lab.run_probe("dig", {"domain": "shop.lab", "server": "alt_dns"})
    curl = lab.run_probe("curl", {"url": "http://shop.lab/", "resolve_ip": "10.20.0.10"})
    assert dig.ok and not missing_parts("dig", dig.output)
    assert "@10.20.0.54" in calls[0]
    assert curl.ok and not missing_parts("curl", curl.output)
    assert "Connection refused" in curl.output and "-v" in calls[1]
    assert "shop.lab:80:10.20.0.10" in calls[1]
    assert not lab.run_probe("curl", {"url": "http://example.com/"}).ok
    assert not lab.run_probe("unknown", {}).ok
    monkeypatch.setattr(
        lab,
        "_run",
        lambda *a, **kw: {"exit_code": None, "stdout": "", "stderr": "", "elapsed_ms": 1},
    )
    assert not lab.run_probe("curl", {"url": "http://shop.lab/"}).ok


def test_capture_probe_returns_record(monkeypatch):
    lab = DockerLab()
    monkeypatch.setattr(
        lab,
        "_capture",
        lambda url: (
            {"exit_code": 0, "output": "#1 packet", "elapsed_ms": 2, "packets": 1},
            "/private/test.pcap",
        ),
    )
    result = lab.run_probe("capture", {"url": "http://shop.lab/"})
    assert result.ok and result.packets == 1 and result.file == "/private/test.pcap"
    assert not missing_parts("capture", result.output)


def test_dns_waits_for_expected_answers(monkeypatch):
    lab = DockerLab()
    calls = []
    monkeypatch.setattr(
        lab,
        "_run",
        lambda *a, **kw: calls.append(a) or {"exit_code": 0, "stderr": ""},
    )
    answers = iter(["10.20.0.10", "10.20.0.10", "10.20.0.99", "10.20.0.10"])
    monkeypatch.setattr(lab, "_dns", lambda *a: {"exit_code": 0, "stdout": next(answers)})
    assert lab._set_dns("shop.lab", "10.20.0.99")["exit_code"] == 0
    assert 'su-exec dnsmasq kill -HUP "$(cat /tmp/dnsmasq.pid)"' in calls[0][3]


def test_n4_keeps_load_active_after_symptom_verification(monkeypatch, tmp_path):
    lab = DockerLab()
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/first_integration/r1_dns_old_ip.scenario.json")
    )
    scenario.faults = [FaultSpec("N4", {"rate_kbit": 256})]
    events = []
    ping_outputs = iter(
        [
            "\n".join("64 bytes from web: time=1 ms" for _ in range(20)),
            "\n".join("64 bytes from web: time=20 ms" for _ in range(20)),
        ]
    )
    pcap = tmp_path / "n4.pcap"
    pcap.touch()

    monkeypatch.setattr(
        lab,
        "_dns",
        lambda server, domain, timeout=2: {
            "exit_code": 0,
            "stdout": lab.config["web_ip"],
        },
    )
    monkeypatch.setattr(
        lab,
        "_curl",
        lambda *a: {"exit_code": 0, "stdout": "200 0 0 0", "stderr": ""},
    )
    monkeypatch.setattr(
        lab,
        "_run",
        lambda *a, **kw: (
            events.append("ping") or {"exit_code": 0, "stdout": next(ping_outputs), "stderr": ""}
        ),
    )
    monkeypatch.setattr(lab, "_stop_load", lambda: events.append("stop") or {"exit_code": 0})
    monkeypatch.setattr(lab, "_start_load", lambda: events.append("start") or {"exit_code": 0})
    monkeypatch.setattr(
        lab,
        "_capture",
        lambda url: (
            {"exit_code": 0, "packets": 1, "stdout": "#1 packet"},
            str(pcap),
        ),
    )

    result = lab.verify_symptom(scenario)

    assert result.ok, result.observations
    assert events == ["stop", "ping", "start", "ping"]


@pytest.mark.parametrize(
    "blocked_exit,packet,expected",
    [
        (7, "target_reset", True),
        (7, "other_reset", False),
        (7, "target_syn_and_other_reset", False),
        (28, "target_syn", False),
    ],
)
def test_https_b2_requires_refusal_from_blockpage(
    monkeypatch, tmp_path, blocked_exit, packet, expected
):
    lab = DockerLab()
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/first_integration/r1_dns_old_ip.scenario.json")
    )
    scenario.target_url = "https://shop.lab/"
    block_ip = lab.hosts["block_page"]
    scenario.faults = [FaultSpec("B2", {"domain": "shop.lab", "block_ip": block_ip})]
    packet_output = {
        "target_reset": f"IP {block_ip}.443 > 10.21.0.2.12345: Flags [R.]",
        "other_reset": "IP 10.20.0.99.443 > 10.21.0.2.12345: Flags [R.]",
        "target_syn_and_other_reset": (
            f"IP 10.21.0.2.12345 > {block_ip}.443: Flags [S]\n"
            "IP 10.20.0.99.443 > 10.21.0.2.12345: Flags [R.]"
        ),
        "target_syn": f"IP 10.21.0.2.12345 > {block_ip}.443: Flags [S]",
    }[packet]
    pcap = tmp_path / "b2.pcap"
    pcap.touch()

    monkeypatch.setattr(
        lab,
        "_dns",
        lambda server, domain, timeout=2: {
            "exit_code": 0,
            "stdout": block_ip if server == lab.hosts["dns"] else lab.config["web_ip"],
        },
    )
    monkeypatch.setattr(
        lab,
        "_curl",
        lambda *a: {"exit_code": blocked_exit, "stdout": "", "stderr": ""},
    )
    monkeypatch.setattr(
        lab,
        "_run",
        lambda *a, **kw: {
            "exit_code": blocked_exit,
            "stdout": "",
            "stderr": "Connection refused",
        },
    )
    monkeypatch.setattr(
        lab,
        "_capture",
        lambda url: (
            {"exit_code": 0, "packets": 1, "stdout": packet_output},
            str(pcap),
        ),
    )

    result = lab.verify_symptom(scenario)

    assert result.ok is expected, result.observations


def test_capture_numbers_packets_and_always_cleans_up(monkeypatch, tmp_path):
    lab = DockerLab()
    calls = []
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def copy(_cmd, **kwargs):
        kwargs["stdout"].write(b"pcap")
        return subprocess.CompletedProcess(_cmd, 0)

    monkeypatch.setattr(subprocess, "run", copy)

    def run(service, *args, **kwargs):
        calls.append(args)
        if args[:2] == ("sh", "-c"):
            return {"exit_code": 0, "stdout": "    1 packet one\n    2 packet two\n", "stderr": ""}
        return {"exit_code": 0}

    monkeypatch.setattr(lab, "_run", run)
    result, path = lab._capture("http://shop.lab/")
    assert result["packets"] == 2
    assert result["output"] == "#1 packet one\n#2 packet two\n"
    assert Path(path).read_bytes() == b"pcap"
    if os.name == "posix":
        assert Path(path).stat().st_mode & 0o777 == 0o600
    assert ("sh", "/stop-capture.sh") in calls
    assert calls[-1][0:2] == ("rm", "-f")


def test_capture_copy_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    lab = DockerLab()
    monkeypatch.setattr(lab, "_run", lambda *a, **kw: {"exit_code": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 1))
    result, path = lab._capture("http://shop.lab/")
    assert result["exit_code"] == 1
    assert not Path(path).exists()


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
def test_real_probe_outputs():
    lab = DockerLab()
    for tool, args in (
        ("dig", {"domain": "shop.lab", "server": "dns"}),
        ("dig", {"domain": "shop.lab", "server": "alt_dns"}),
        ("curl", {"url": "http://shop.lab/"}),
        ("capture", {"url": "http://shop.lab/"}),
        ("openssl", {"host": "shop.lab"}),
        ("ping", {"host": "10.20.0.10", "count": 2}),
        ("tracepath", {"host": "10.20.0.10"}),
    ):
        result = lab.run_probe(tool, args)
        assert result.ok, result.error
        assert not missing_parts(tool, result.output)


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
def test_web_server_runs_without_root():
    result = DockerLab()._run("web", "sh", "-c", "awk '/^Uid:/ {print $2}' /proc/1/status")
    assert result["exit_code"] == 0 and result["stdout"].strip() not in ("", "0")


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
@pytest.mark.parametrize(
    "filename",
    [
        "fs01_d1_shop",
        "fs02_d1_news",
        "fs05_d3_shop",
        "fs06_d3_news",
        "fs12_n1_shop",
        "fs13_n1_news",
    ],
)
def test_real_dns_fault_recovers(filename):
    lab = DockerLab()
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/fixed_set" / f"{filename}.scenario.json")
    )
    try:
        assert lab.health_check(scenario.target_url).ok
        assert lab.inject(scenario.faults[0]).ok
        result = lab.verify_symptom(scenario)
        assert result.ok, result.observations
    finally:
        assert lab.reset().ok
        assert lab.health_check(scenario.target_url).ok


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
@pytest.mark.parametrize(
    "fault",
    [
        FaultSpec("T1", {"port": 80}),
        FaultSpec("N2", {"loss_pct": 30}),
        FaultSpec("N3", {"min_size": 576}),
        FaultSpec("N3", {"min_size": 1500}),
        FaultSpec("H2", {}),
    ],
)
def test_real_extra_fault_recovers(fault):
    lab = DockerLab()
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/first_integration/r2_tcp_refused.scenario.json")
    )
    scenario.faults = [fault]
    try:
        assert lab.health_check(scenario.target_url).ok
        if fault.id == "N3":
            assert lab._curl("http://shop.lab/large")["exit_code"] == 0
        assert lab.inject(scenario.faults[0]).ok
        if fault.id == "N3":
            probe = lab.run_probe(
                "ping", {"host": "shop.lab", "count": 2, "size": fault.params["min_size"] - 28}
            )
            assert probe.ok and "100% packet loss" in probe.output
        result = lab.verify_symptom(scenario)
        assert result.ok, result.observations
    finally:
        assert lab.reset().ok
        assert lab.health_check(scenario.target_url).ok


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
@pytest.mark.parametrize(
    "filename", ["fs19_s1_shop", "fs20_s1_news", "fs23_h1_shop", "fs24_h1_news"]
)
def test_real_web_fault_recovers(filename):
    lab = DockerLab()
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/fixed_set" / f"{filename}.scenario.json")
    )
    try:
        assert lab.health_check(scenario.target_url).ok
        assert lab.inject(scenario.faults[0]).ok
        result = lab.verify_symptom(scenario)
        assert result.ok, result.observations
    finally:
        assert lab.reset().ok
        assert lab.health_check(scenario.target_url).ok


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
@pytest.mark.parametrize(
    "domain,cert_domain", [("shop.lab", "news.lab"), ("news.lab", "shop.lab")]
)
def test_real_s2_recovers(domain, cert_domain):
    lab = DockerLab()
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/fixed_set/fs19_s1_shop.scenario.json")
    )
    scenario.target_url = f"https://{domain}/"
    scenario.faults = [FaultSpec("S2", {"cert_domain": cert_domain})]
    try:
        assert lab.health_check(scenario.target_url).ok
        assert lab.inject(scenario.faults[0]).ok
        result = lab.verify_symptom(scenario)
        assert result.ok, result.observations
    finally:
        assert lab.reset().ok
        assert lab.health_check(scenario.target_url).ok


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
@pytest.mark.parametrize("delay_ms", [100, 3000])
def test_real_d4_recovers(delay_ms):
    lab = DockerLab()
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/first_integration/r1_dns_old_ip.scenario.json")
    )
    scenario.faults = [FaultSpec("D4", {"domain": scenario.domain, "delay_ms": delay_ms})]
    try:
        assert lab.health_check(scenario.target_url).ok
        assert lab.inject(scenario.faults[0]).ok
        result = lab.verify_symptom(scenario)
        assert result.ok, result.observations
        assert (
            int(result.observations["timed_dns"]["stdout"].split("Query time: ")[1].split()[0])
            >= delay_ms * 0.75
        )
    finally:
        assert lab.reset().ok
        assert lab.health_check(scenario.target_url).ok


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
def test_real_n4_recovers():
    lab = DockerLab()
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/first_integration/r1_dns_old_ip.scenario.json")
    )
    scenario.faults = [FaultSpec("N4", {"rate_kbit": 256})]
    try:
        assert lab.health_check(scenario.target_url).ok
        assert lab.inject(scenario.faults[0]).ok
        result = lab.verify_symptom(scenario)
        assert result.ok, result.observations
        running = lab._run(
            "client",
            "sh",
            "-c",
            'pid=$(cat /tmp/netrunner-load.pid); kill -0 "$pid"',
        )
        assert running["exit_code"] == 0
    finally:
        assert lab.reset().ok
        assert lab.health_check(scenario.target_url).ok


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
@pytest.mark.parametrize("domain", ["shop.lab", "news.lab"])
def test_real_b1_recovers(domain):
    lab = DockerLab()
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/fixed_set/fs19_s1_shop.scenario.json")
    )
    scenario.target_url = f"https://{domain}/"
    scenario.faults = [FaultSpec("B1", {"domain": domain})]
    try:
        assert lab.health_check(scenario.target_url).ok
        assert lab.inject(scenario.faults[0]).ok
        result = lab.verify_symptom(scenario)
        assert result.ok, result.observations
    finally:
        assert lab.reset().ok
        assert lab.health_check(scenario.target_url).ok


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
@pytest.mark.parametrize("domain", ["shop.lab", "news.lab"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_real_b2_recovers(domain, scheme):
    lab = DockerLab()
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/first_integration/r1_dns_old_ip.scenario.json")
    )
    scenario.target_url = f"{scheme}://{domain}/"
    scenario.faults = [FaultSpec("B2", {"domain": domain, "block_ip": lab.hosts["block_page"]})]
    try:
        assert lab.health_check(scenario.target_url).ok
        assert lab.inject(scenario.faults[0]).ok
        result = lab.verify_symptom(scenario)
        assert result.ok, result.observations
        if scheme == "https":
            assert "Connection refused" in result.observations["block_page"]["stderr"]
            assert "Flags [R" in result.observations["capture"]["stdout"]
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
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/first_integration/r2_tcp_refused.scenario.json")
    )
    record = Referee(lab, ReplayDiagnoser([]), LabInfo.load(ROOT / "config/lab.json")).run_round(
        scenario
    )
    assert record["status"] == "invalid" and record["reset_verified"]
    assert lab.health_check(scenario.target_url).ok


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
def test_real_lab_d2_alt_dns_refuses_connection_and_recovers():
    lab = DockerLab()
    scenario = Scenario.from_dict(
        read_json(ROOT / "fixtures/first_integration/r1_dns_old_ip.scenario.json")
    )
    scenario.faults[0].params["ip"] = lab.hosts["alt_dns"]
    try:
        assert lab.health_check(scenario.target_url).ok
        assert lab.inject(scenario.faults[0]).ok
        result = lab.verify_symptom(scenario)
        assert result.ok, result.observations
        assert "Connection refused" in result.observations["curl"]["stderr"]
        assert "Flags [R" in result.observations["capture"]["stdout"]
    finally:
        assert lab.reset().ok
        assert lab.health_check(scenario.target_url).ok
