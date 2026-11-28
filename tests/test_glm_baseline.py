import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from netrunner.breaker import CompletionError
from netrunner.contracts import (
    BaselineBrief,
    ContractError,
    Finding,
    Limits,
    ProbeResult,
    RoundBrief,
    Scenario,
)
from netrunner.glm_baseline import GlmBaselineDiagnoser
from netrunner.glm_client import OpencodeComplete
from netrunner.lab import MockLab
from netrunner.referee import Referee
from netrunner.validate import LabInfo

ROOT = Path(__file__).resolve().parents[1]
TARGET = "http://shop.lab/"


def baseline_with_responses(*actions):
    def reply(argv, **kwargs):
        action = next(responses)
        event = {
            "type": "text",
            "part": {"text": action if isinstance(action, str) else json.dumps(action)},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(event), "")

    responses = iter(actions)
    run = Mock(side_effect=reply)
    return GlmBaselineDiagnoser(OpencodeComplete("opencode", ROOT, ROOT / "config", run=run)), run


@pytest.mark.parametrize(
    ("output", "error"),
    [
        (
            "http_code=000\nverbose\ncurl: (7) Connection refused\nsecret",
            "curl: (7) Connection refused",
        ),
        ("http_code=502\nverbose", "HTTP 502"),
        ("http_code=200\nverbose", None),
    ],
)
def test_referee_provides_only_optional_error_line_without_diagnoser_probes(output, error):
    diagnoser, model = baseline_with_responses(
        {"findings": [{"stage": "tcp", "cause": "connection refused"}]}
    )
    scenario = Scenario.from_dict(
        {
            "scenario_id": "hidden-case",
            "target_url": TARGET,
            "complaint": "접속이 안 돼요.",
            "faults": [{"id": "T2", "params": {"port": 80}}],
            "note": "hidden-answer",
        }
    )
    runner = Mock()
    runner.run_probe.return_value = ProbeResult(True, output)
    referee = Referee(MockLab(), diagnoser, LabInfo.load(ROOT / "config/lab.json"), runner=runner)

    record = referee.run_round(scenario)

    assert record["status"] == "scored", record["invalid_reason"]
    assert record["reset_verified"]
    assert record["method"] == "B"
    assert record["brief"].get("curl_error") == error
    assert record["score"]["faults"][0]["verdict"] == "correct"
    assert record["diagnosis"]["findings"][0]["evidence"] == []
    assert record["probes"] == record["rejected_calls"] == []
    assert record["score"]["tool_calls"] == 0
    runner.run_probe.assert_called_once_with("curl", {"url": TARGET})
    prompt = model.call_args.kwargs["input"]
    payload = {"url": TARGET, "symptom": scenario.complaint}
    if error is not None:
        payload["curl_error"] = error
    assert json.loads(prompt.splitlines()[-1]) == payload
    assert all(
        word not in prompt for word in ("hidden-case", "hidden-answer", "verbose", "secret")
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"check": "curl", "args": {"url": TARGET}},
        {"findings": [{"stage": "TCP", "cause": "RST", "evidence": ["probe:p1"]}]},
        {"findings": [{"stage": "TCP", "cause": "임의 장애"}]},
    ],
)
def test_baseline_retries_invalid_output_without_accessing_toolbox(invalid):
    diagnoser, model = baseline_with_responses(
        invalid, {"findings": [{"stage": "tcp", "cause": "RST"}]}
    )
    tools = Mock()

    result = diagnoser.diagnose(RoundBrief("r1", TARGET, "접속 실패"), tools)

    assert result.findings == [Finding("TCP", "연결 거부")]
    assert result.llm_calls == model.call_count == 2
    assert not tools.mock_calls


@pytest.mark.parametrize(
    "wrapper", ["```json\n{}\n```", "```\n{}\n```\n설명입니다.", "{}\n`findings` 설명입니다."]
)
def test_baseline_reads_one_wrapped_diagnosis_without_retrying(wrapper):
    payload = '{"findings":[{"stage":"TCP","cause":"연결 거부"}]}'
    diagnoser, model = baseline_with_responses(wrapper.format(payload))

    result = diagnoser.diagnose(RoundBrief("r1", TARGET, "접속 실패"), Mock())

    assert result.findings == [Finding("TCP", "연결 거부")]
    assert result.llm_calls == model.call_count == 1


@pytest.mark.parametrize(
    "invalid", ['{"findings":[', '{"findings":[]}\n{"findings":[]}', '```json\n{"findings":[]}']
)
def test_baseline_rejects_ambiguous_or_incomplete_wrappers(invalid):
    diagnoser, model = baseline_with_responses(invalid, invalid)

    with pytest.raises(ContractError, match="JSON 객체"):
        diagnoser.diagnose(RoundBrief("r1", TARGET, "접속 실패"), Mock())

    assert model.call_count == 2


def test_baseline_distinguishes_no_findings_from_empty_model_text():
    diagnoser, _ = baseline_with_responses({"findings": []}, "")
    brief = RoundBrief("r1", TARGET, "접속 실패")

    assert diagnoser.diagnose(brief, Mock()).findings == []
    with pytest.raises(CompletionError, match="모델 응답이 비어 있음"):
        diagnoser.diagnose(brief, Mock())


@pytest.mark.parametrize(
    ("curl_error", "symptom"),
    [(None, "DNS 조회 결과에 NXDOMAIN이 표시돼요."), ("HTTP 502", "사이트 접속이 안 돼요.")],
)
def test_baseline_prompt_handles_ambiguity_without_requiring_a_curl_error(curl_error, symptom):
    diagnoser, model = baseline_with_responses({"findings": []})
    brief = BaselineBrief("r1", TARGET, symptom, curl_error=curl_error)

    diagnoser.diagnose(brief, Mock())

    prompt = model.call_args.kwargs["input"]
    assert "특정 원인을 추측하지 않는다" in prompt
    assert "증상 자체에 뚜렷한 단서가 있으면 사용한다" in prompt
    assert "구분할 수 없으면" in prompt
    assert json.loads(prompt.splitlines()[-1]).get("curl_error") == curl_error


@pytest.mark.parametrize("limits", [Limits(max_llm_calls=0), Limits(max_seconds=0)])
def test_baseline_honors_limits_without_contacting_model(limits):
    diagnoser, model = baseline_with_responses()
    tools = Mock()

    result = diagnoser.diagnose(RoundBrief("r1", TARGET, "접속 실패", limits), tools)

    assert result.findings == []
    assert result.llm_calls == 0
    model.assert_not_called()
    assert not tools.mock_calls


@pytest.mark.parametrize("limit", [1, 5])
def test_baseline_stops_retrying_at_its_limit(limit):
    diagnoser, model = baseline_with_responses({"check": "curl"}, {"check": "curl"})
    brief = RoundBrief("r1", TARGET, "접속 실패", Limits(max_llm_calls=limit))

    with pytest.raises(ContractError, match="findings만"):
        diagnoser.diagnose(brief, Mock())

    assert model.call_count == min(limit, 2)
