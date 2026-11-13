from unittest.mock import Mock

import pytest

from netrunner.contracts import Limits, RoundBrief
from netrunner.diagnoser import HttpDiagnoser
from netrunner.toolbox import Toolbox
from netrunner.validate import LabInfo

# HttpDiagnoser는 아직 창구를 쓰지 않는다(tools.call 전환은 이현섭 작업).
NO_TOOLS = Toolbox(None, LabInfo(["nginx"], "127.0.0.1", "127.0.0.0/8"), Limits(), frozenset())


@pytest.mark.parametrize(
    ("code", "loop", "output", "cause"),
    [
        (502, False, "HTTP 502 Bad Gateway", "서버 오류"),
        (301, True, "HTTP 301 /a -> /b\nHTTP 301 /b -> /a", "리다이렉트 순환"),
        (200, False, "HTTP 200 OK", None),
    ],
)
def test_http_diagnosis_from_observation(code, loop, output, cause):
    probe = Mock(
        return_value={
            "http_code": code,
            "redirect_loop": loop,
            "raw_http": output,
            "elapsed_ms": 10,
            "location": "client container",
        }
    )
    brief = RoundBrief("r1", "http://nginx/", "사이트가 열리지 않아요.")

    result = HttpDiagnoser(probe).diagnose(brief, NO_TOOLS)

    probe.assert_called_once_with(brief.target_url, timeout=brief.limits.max_seconds)
    assert result.round_id == brief.round_id
    assert len(result.trace) == 1
    assert result.trace[0].output == output
    if cause is None:
        assert result.findings == []
    else:
        (finding,) = result.findings
        (evidence,) = finding.evidence
        assert (finding.stage, finding.cause) == ("HTTP", cause)
        assert evidence.ref == f"probe:{result.trace[0].probe_id}"


@pytest.mark.parametrize("limits", [Limits(max_tool_calls=0), Limits(max_seconds=0)])
def test_http_diagnoser_skips_probe_when_budget_is_zero(limits):
    probe = Mock()
    brief = RoundBrief("r1", "http://nginx/", "사이트가 열리지 않아요.", limits)

    result = HttpDiagnoser(probe).diagnose(brief, NO_TOOLS)

    probe.assert_not_called()
    assert result.trace == []
    assert result.findings == []
