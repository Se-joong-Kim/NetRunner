"""검사 도구 목록(tools.py)과 실험망 정보의 도구 관련 필드 테스트."""

import json
from pathlib import Path

from netrunner.validate import LabInfo

ROOT = Path(__file__).resolve().parent.parent
LAB = LabInfo.load(ROOT / "config" / "lab.json")


def test_lab_info_reads_hosts_and_enabled_tools():
    assert LAB.hosts["alt_dns"] == "10.20.0.54"
    assert LAB.enabled_tools == ["dig", "curl", "capture"]


def test_lab_info_defaults_to_no_tools(tmp_path):
    path = tmp_path / "lab.json"
    path.write_text(
        json.dumps({"domains": ["nginx"], "web_ip": "127.0.0.1", "subnet": "127.0.0.0/8"}),
        encoding="utf-8",
    )
    info = LabInfo.load(path)
    assert info.enabled_tools == [] and info.hosts == {}
