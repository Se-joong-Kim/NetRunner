import sys
from unittest.mock import Mock, call

from netrunner import http_lab
from netrunner.contracts import FaultSpec, Scenario


def test_h1_injection_and_reset_restore_nginx_config(tmp_path, monkeypatch):
    directory = tmp_path / "labs" / "http"
    directory.mkdir(parents=True)
    baseline = b"proxy_pass http://backend:8000;\n"
    (directory / "nginx.conf").write_bytes(baseline)
    monkeypatch.setattr(http_lab, "ROOT", tmp_path)
    monkeypatch.setattr(http_lab.shutil, "which", lambda _: sys.executable)
    lab = http_lab.HttpDockerLab()
    compose = Mock(return_value={})
    monkeypatch.setattr(lab, "compose", compose)
    probe = Mock(side_effect=[{"http_code": code} for code in (200, 502, 200)])
    monkeypatch.setattr(lab, "probe", probe)
    fault = FaultSpec("H1")
    scenario = Scenario("h1", http_lab.TARGET, [fault], "사이트가 열리지 않아요.")

    assert lab.health_check(scenario.target_url).observations["http_code"] == 200
    assert lab.inject(fault).ok
    assert lab.active.read_bytes() == b"proxy_pass http://backend:8001;\n"
    symptom = lab.verify_symptom(scenario)
    assert symptom.ok and symptom.observations["http_code"] == 502
    assert lab.reset().ok
    assert lab.active.read_bytes() == baseline
    assert lab.health_check(scenario.target_url).observations["http_code"] == 200
    assert compose.call_args_list == [call("restart", "nginx"), call("restart", "nginx")]
