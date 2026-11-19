"""검사 도구 목록(tools.py)과 실험망 정보의 도구 관련 필드 테스트."""

import json
import re
from pathlib import Path

import pytest

from netrunner.faults import FAULTS
from netrunner.tools import TOOLS, check_args, missing_parts, specs, tools_for_method
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


# ---------------------------------------------------------------- 도구 목록


def test_method_tool_sets():
    assert tools_for_method("B") == frozenset()
    assert "capture" not in tools_for_method("C-1")
    assert tools_for_method("C") == frozenset(TOOLS)
    assert tools_for_method("Z") is None


@pytest.mark.parametrize(
    "tool, args",
    [
        ("dig", {"domain": "shop.lab", "server": "alt_dns"}),
        ("dig", {"domain": "shop.lab", "server": "10.20.0.54"}),
        ("curl", {"url": "http://shop.lab/", "resolve_ip": "10.20.0.10"}),
        ("capture", {"url": "https://news.lab:8443/a?b=1"}),
        ("ping", {"host": "10.20.0.10", "count": 3}),
    ],
)
def test_check_args_accepts_lab_values(tool, args):
    assert check_args(tool, args, LAB) is None


@pytest.mark.parametrize(
    "tool, args",
    [
        ("rm", {}),
        ("dig", {"domain": "example.com"}),
        ("dig", {"domain": "shop.lab", "flags": "+trace"}),
        ("curl", {}),
        ("curl", {"url": "http://shop.lab/ ; id"}),
        ("curl", {"url": "ftp://shop.lab/"}),
        ("curl", {"url": "http://shop.lab/", "resolve_ip": "8.8.8.8"}),
        ("dig", {"domain": "shop.lab", "server": "10.20.0.10"}),
        ("dig", '{"domain": "shop.lab"}'),
    ],
)
def test_check_args_rejects(tool, args):
    err = check_args(tool, args, LAB)
    assert isinstance(err, str) and err


def test_bool_is_not_an_int():
    assert check_args("openssl", {"host": "shop.lab", "port": True}, LAB)
    assert check_args("ping", {"host": "shop.lab", "count": False}, LAB)


def _cause_words_in(text: str) -> list[str]:
    hits = []
    for f in FAULTS.values():
        for word in (f.cause, *f.cause_aliases):
            if word.isascii():
                pattern = rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])"
                if re.search(pattern, text.lower()):
                    hits.append(word)
            elif word in text:
                hits.append(word)
    return hits


def test_specs_shape_and_no_cause_words():
    out = specs(["dig", "curl"])
    assert [s["function"]["name"] for s in out] == ["curl", "dig"]
    dig = out[1]["function"]
    assert dig["parameters"]["required"] == ["domain"]
    assert dig["parameters"]["additionalProperties"] is False
    for s in specs(TOOLS):
        fn = s["function"]
        texts = [fn["description"]] + [
            p.get("description", "") for p in fn["parameters"]["properties"].values()
        ]
        assert all(_cause_words_in(t) == [] for t in texts), fn["name"]


def test_missing_parts():
    assert missing_parts("dig", "ANSWER: 1 ...\n;; Query time: 3 msec") == []
    assert missing_parts("capture", "1 IP 10.21.0.2 > 10.20.0.10") != []


def test_curl_output_needs_every_phase_time():
    # 설계 문서 3장: 단계별 시간(namelookup, connect, appconnect, starttransfer, total)
    out = "exit_code=0\nhttp_code=200\ntime_namelookup=0.001\ntime_connect=0.002\ntime_total=0.010"
    assert set(missing_parts("curl", out)) == {"time_appconnect=", "time_starttransfer="}
    full = out + "\ntime_appconnect=0.000\ntime_starttransfer=0.009"
    assert missing_parts("curl", full) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://shop.lab/;id",
        "http://shop.lab/$(reboot)",
        "http://shop.lab/`id`",
        "http://shop.lab/a|b",
        "http://shop.lab/[1-100000]",
        "http://shop.lab/{a,b,c}",
    ],
)
def test_url_rejects_shell_and_glob_chars(url):
    assert check_args("curl", {"url": url}, LAB)
