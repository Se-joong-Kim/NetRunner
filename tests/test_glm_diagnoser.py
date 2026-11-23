import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from netrunner import __main__ as cli
from netrunner.contracts import (
    ContractError,
    Evidence,
    Finding,
    Limits,
    ProbeResult,
    RoundBrief,
    Scenario,
)
from netrunner.diagnoser import GlmDiagnoser
from netrunner.faults import FAULTS
from netrunner.glm_client import OpencodeComplete
from netrunner.lab import MockLab
from netrunner.referee import Referee
from netrunner.toolbox import Toolbox
from netrunner.validate import LabInfo

ROOT = Path(__file__).resolve().parents[1]


def model_with_responses(*actions):
    def reply(argv, **_kwargs):
        action = next(responses)
        text = action if isinstance(action, str) else json.dumps(action)
        event = {"type": "text", "part": {"text": text}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(event), "")

    responses = iter(actions)
    run = Mock(side_effect=reply)
    complete = OpencodeComplete("opencode", ROOT, ROOT / "config/opencode_diagnoser.json", run=run)
    return GlmDiagnoser(complete), run


@pytest.mark.parametrize(
    ("fault_id", "params", "checks", "outputs", "stage", "cause"),
    [
        (
            "D2",
            {"domain": "shop.lab", "ip": "10.20.0.99"},
            ["dns", "alt_dns"],
            ["shop.lab. 60 IN A 10.20.0.99", "shop.lab. 60 IN A 10.20.0.10"],
            "DNS",
            "잘못된 주소",
        ),
        ("T2", {"port": 80}, ["http"], ["curl: Connection refused"], "TCP", "연결 거부"),
    ],
)
def test_referee_scores_glm_diagnosis_using_toolbox_record(
    fault_id, params, checks, outputs, stage, cause
):
    scenario = Scenario.from_dict(
        {
            "scenario_id": "hidden-scenario",
            "target_url": "http://shop.lab/",
            "complaint": "사이트가 열리지 않아요.",
            "faults": [{"id": fault_id, "params": params}],
            "note": "hidden-answer-note",
        }
    )
    result = {"stage": stage, "cause": cause, "evidence": [{"ref": "probe:p1", "claim": "관측"}]}
    diagnoser, model = model_with_responses(
        *({"check": check} for check in checks), {"findings": [result]}
    )
    runner = Mock()
    runner.run_probe.side_effect = [ProbeResult(True, output) for output in outputs]
    referee = Referee(MockLab(), diagnoser, LabInfo.load(ROOT / "config/lab.json"), runner=runner)

    record = referee.run_round(scenario)

    assert record["status"] == "scored", record["invalid_reason"]
    assert record["score"]["faults"][0]["verdict"] == "correct"
    assert record["score"]["evidence"][0]["supported"] is True
    assert record["reset_verified"] is True
    assert record["diagnosis"]["llm_calls"] == len(checks) + 1
    last_prompt = model.call_args.kwargs["input"]
    assert "probe:p1" in last_prompt and outputs[0] in last_prompt
    assert "hidden-answer-note" not in last_prompt and "hidden-scenario" not in last_prompt


@pytest.mark.parametrize(
    "action",
    [
        {"check": "http", "args": {"url": "https://example.org/"}},
        {"findings": [{"stage": "TCP", "cause": "연결 거부", "evidence": ["probe:p9"]}]},
    ],
)
def test_rejects_model_supplied_targets_and_unobserved_evidence(action):
    diagnoser, _ = model_with_responses(action)
    runner = Mock()
    limits = Limits()
    lab = LabInfo.load(ROOT / "config/lab.json")
    toolbox = Toolbox(runner, lab, limits, frozenset({"curl"}))
    with pytest.raises(ContractError):
        diagnoser.diagnose(RoundBrief("round", "http://shop.lab/", "접속 실패", limits), toolbox)
    runner.run_probe.assert_not_called()


def test_zero_model_budget_does_not_call_opencode():
    diagnoser, model = model_with_responses()
    limits = Limits(max_llm_calls=0)
    toolbox = Toolbox(None, LabInfo.load(ROOT / "config/lab.json"), limits, frozenset())
    result = diagnoser.diagnose(
        RoundBrief("round", "http://shop.lab/", "접속 실패", limits), toolbox
    )
    assert not result.findings
    model.assert_not_called()


@pytest.mark.parametrize(
    ("limit", "corrected", "expected_calls", "causes"),
    [
        (3, "서버 무응답", 3, ["서버 무응답", "지연"]),
        (4, "주 DNS 서버 무응답", 3, ["지연"]),
        (2, "서버 무응답", 2, ["지연"]),
    ],
)
def test_nonstandard_cause_retries_once_and_keeps_known_findings(
    limit, corrected, expected_calls, causes
):
    unknown = {"stage": "DNS", "cause": "주 DNS 서버 무응답", "evidence": ["probe:p1"]}
    known = {"stage": "dns", "cause": "slow", "evidence": ["probe:p1"]}
    diagnoser, model = model_with_responses(
        {"check": "dns"},
        {"findings": [unknown, known]},
        {"findings": [{**unknown, "cause": corrected}, known]},
    )
    runner = Mock()
    runner.run_probe.return_value = ProbeResult(True, "DNS query output")
    limits = Limits(max_llm_calls=limit)
    lab = LabInfo.load(ROOT / "config/lab.json")
    toolbox = Toolbox(runner, lab, limits, frozenset({"dig"}))

    result = diagnoser.diagnose(RoundBrief("r1", "http://shop.lab/", "DNS 오류", limits), toolbox)

    assert result.findings == [Finding("DNS", cause, [Evidence("probe:p1")]) for cause in causes]
    assert model.call_count == expected_calls
    runner.run_probe.assert_called_once_with("dig", {"domain": "shop.lab", "server": "dns"})
    prompt = model.call_args.kwargs["input"]
    assert all(f"- {fault.stage} / {fault.cause}\n" in prompt for fault in FAULTS.values())
    if expected_calls == 3:
        assert "목록에 없는 단계 또는 원인" in prompt
        assert unknown["cause"] not in prompt


def test_glm_can_use_path_capture_and_tls_checks_for_the_brief_target():
    diagnoser, _ = model_with_responses(
        {"check": "ping"},
        {"check": "tracepath"},
        {"check": "capture"},
        {"check": "tls"},
        {"findings": [{"stage": "경로", "cause": "latency", "evidence": ["probe:p1"]}]},
    )
    runner = Mock()
    runner.run_probe.return_value = ProbeResult(True, "test output")
    limits = Limits()
    url = "https://shop.lab:8443/"
    lab = LabInfo.load(ROOT / "config/lab.json")
    toolbox = Toolbox(runner, lab, limits, frozenset({"ping", "tracepath", "capture", "openssl"}))

    result = diagnoser.diagnose(RoundBrief("r1", url, "접속이 느려요", limits), toolbox)

    assert result.findings == [Finding("경로", "지연", [Evidence("probe:p1")])]
    assert [(p.tool, p.args) for p in toolbox.probes()] == [
        ("ping", {"host": "shop.lab"}),
        ("tracepath", {"host": "shop.lab"}),
        ("capture", {"url": url}),
        ("openssl", {"host": "shop.lab", "port": 8443}),
    ]


@pytest.mark.parametrize(("check", "allowed"), [("capture", {"ping"}), ("tls", {"openssl"})])
def test_glm_rejects_checks_not_offered_for_the_round(check, allowed):
    diagnoser, _ = model_with_responses({"check": check})
    runner = Mock()
    limits = Limits()
    lab = LabInfo.load(ROOT / "config/lab.json")
    toolbox = Toolbox(runner, lab, limits, frozenset(allowed))

    with pytest.raises(ContractError, match="허용되지 않은 검사"):
        diagnoser.diagnose(RoundBrief("r1", "http://shop.lab/", "접속 실패", limits), toolbox)

    runner.run_probe.assert_not_called()


def test_normal_observation_is_not_scored_as_an_extra_fault():
    observations = [
        {"stage": "DNS", "cause": " 정상 ", "evidence": []},
        {"stage": "TCP", "cause": "연결 거부", "evidence": ["probe:p1"]},
    ]
    diagnoser, _ = model_with_responses({"check": "http"}, {"findings": observations})
    runner = Mock()
    runner.run_probe.return_value = ProbeResult(True, "curl: Connection refused")
    scenario = Scenario.from_dict(
        {
            "scenario_id": "normal-dns",
            "target_url": "http://shop.lab/",
            "complaint": "접속 실패",
            "faults": [{"id": "T2", "params": {"port": 80}}],
        }
    )
    referee = Referee(MockLab(), diagnoser, LabInfo.load(ROOT / "config/lab.json"), runner=runner)

    record = referee.run_round(scenario)

    assert record["status"] == "scored"
    assert record["score"]["false_positives"] == []
    assert record["score"]["faults"][0]["verdict"] == "correct"


def test_empty_result_after_normal_dns_prompts_for_remaining_http_check():
    finding = {"stage": "TCP", "cause": "연결 거부", "evidence": ["probe:p2"]}
    diagnoser, model = model_with_responses(
        {"check": "dns"}, {"findings": []}, {"check": "http"}, {"findings": [finding]}
    )
    runner = Mock()
    runner.run_probe.side_effect = [
        ProbeResult(True, "shop.lab. 60 IN A 10.20.0.10"),
        ProbeResult(True, "curl: Connection refused"),
    ]
    limits = Limits()
    lab = LabInfo.load(ROOT / "config/lab.json")
    toolbox = Toolbox(runner, lab, limits, frozenset({"dig", "curl"}))

    result = diagnoser.diagnose(RoundBrief("r1", "http://shop.lab/", "접속 실패", limits), toolbox)

    assert result.findings[0].evidence[0].ref == "probe:p2"
    assert [call.args[0] for call in runner.run_probe.call_args_list] == ["dig", "curl"]
    assert "아직 실행하지 않은 검사" in model.call_args_list[2].kwargs["input"]


@pytest.mark.parametrize(("tool_limit", "expected_calls"), [(0, 1), (1, 2)])
def test_empty_result_retries_at_most_once_and_only_when_checks_are_available(
    tool_limit, expected_calls
):
    diagnoser, model = model_with_responses({"findings": []}, {"findings": []})
    runner = Mock()
    limits = Limits(max_tool_calls=tool_limit)
    lab = LabInfo.load(ROOT / "config/lab.json")
    toolbox = Toolbox(runner, lab, limits, frozenset({"curl"}))

    result = diagnoser.diagnose(RoundBrief("r1", "http://shop.lab/", "접속 실패", limits), toolbox)

    assert result.findings == []
    assert model.call_count == expected_calls
    runner.run_probe.assert_not_called()


def test_retries_invalid_json_without_repeating_probes():
    invalid = "이 응답은 올바른 JSON이 아닙니다"
    finding = {"stage": "TCP", "cause": "연결 거부", "evidence": ["probe:p1"]}
    diagnoser, model = model_with_responses({"check": "http"}, invalid, {"findings": [finding]})
    runner = Mock()
    runner.run_probe.return_value = ProbeResult(True, "curl: Connection refused")
    limits = Limits(max_llm_calls=3)
    lab = LabInfo.load(ROOT / "config/lab.json")
    toolbox = Toolbox(runner, lab, limits, frozenset({"curl"}))

    result = diagnoser.diagnose(RoundBrief("r1", "http://shop.lab/", "접속 실패", limits), toolbox)

    assert result.llm_calls == 3
    assert result.findings[0].evidence[0].ref == "probe:p1"
    runner.run_probe.assert_called_once_with("curl", {"url": "http://shop.lab/"})
    retry_prompt = model.call_args.kwargs["input"]
    assert "직전 응답의 형식이 잘못됐다" in retry_prompt
    assert "curl: Connection refused" in retry_prompt
    assert invalid not in retry_prompt


@pytest.mark.parametrize(("limit", "calls"), [(1, 1), (3, 2)])
def test_invalid_json_retry_stops_at_model_call_limit_or_one_retry(limit, calls):
    diagnoser, model = model_with_responses("잘못된 응답", "[]")
    limits = Limits(max_llm_calls=limit)
    lab = LabInfo.load(ROOT / "config/lab.json")
    toolbox = Toolbox(None, lab, limits, frozenset())

    with pytest.raises(ContractError, match="GLM 응답이 JSON 객체 형식이 아님"):
        diagnoser.diagnose(RoundBrief("r1", "http://shop.lab/", "접속 실패", limits), toolbox)

    assert model.call_count == calls


def test_opencode_factory_uses_deny_all_agent():
    diagnoser = GlmDiagnoser.from_opencode("opencode")
    adapter = diagnoser._complete
    agents = json.loads(adapter.config.read_text(encoding="utf-8"))["agent"]
    assert adapter.agent == "diagnoser"
    assert agents[adapter.agent]["permission"]["*"] == "deny"


def test_run_registers_glm_c_with_selected_model_and_executable():
    args = cli.build_parser().parse_args(
        ["run", "--diagnoser", "glm-c", "--opencode", "test-opencode", "--model", "test/model"]
    )
    (name,) = args.diagnoser
    diagnoser = cli.DIAGNOSER_IMPLS[name](args)
    assert diagnoser.method == "C"
    assert diagnoser.model == "test/model"
    assert diagnoser._complete.binary == "test-opencode"


@pytest.mark.parametrize("command", ["run", "match"])
def test_glm_c_requires_a_connected_probe_runner(command, tmp_path, capsys):
    assert (
        cli.main([command, "--impl", "mock", "--diagnoser", "glm-c", "--out", str(tmp_path)]) == 1
    )
    assert "검사 실행기가 연결되지 않았습니다" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
