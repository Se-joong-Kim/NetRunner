"""심판 쪽 도구 창구(Toolbox)와 재생 실행기·진단기 테스트."""

from dataclasses import replace
from pathlib import Path

import pytest

from netrunner.contracts import Limits, ProbeResult
from netrunner.toolbox import MAX_OUTPUT_CHARS, Toolbox
from netrunner.tools import TOOLS
from netrunner.validate import LabInfo

ROOT = Path(__file__).resolve().parent.parent
LAB = LabInfo.load(ROOT / "config" / "lab.json")  # enabled_tools: dig, curl, capture
ALL = frozenset(TOOLS)
DIG = {"domain": "shop.lab"}


class FakeRunner:
    """받은 호출을 쌓고 정해진 결과를 돌려주는 실험망 대역."""

    def __init__(self, result=None, error: Exception | None = None):
        self.calls = []
        self.result = result or ProbeResult(True, "ANSWER: 1\n;; Query time: 2 msec", 5)
        self.error = error

    def run_probe(self, tool, args):
        self.calls.append((tool, args))
        if self.error:
            raise self.error
        return self.result


def box(runner=None, limits=None, allowed=ALL, lab=LAB, clock=None):
    kw = {"clock": clock} if clock else {}
    return Toolbox(runner, lab, limits or Limits(), allowed, **kw)


def test_call_runs_and_records_with_ids():
    runner = FakeRunner()
    tb = box(runner)
    views = [tb.call("dig", DIG), tb.call("dig", dict(DIG, server="alt_dns"))]
    assert [v.probe_id for v in views] == ["p1", "p2"]
    assert [p.probe_id for p in tb.record] == ["p1", "p2"]
    assert tb.record[0].output == runner.result.output and tb.record[0].tool == "dig"
    assert views[0].output == runner.result.output


@pytest.mark.parametrize(
    "name, args, allowed",
    [
        ("rm", {}, ALL),
        ("dig", DIG, frozenset()),
        ("openssl", {"host": "shop.lab"}, ALL),
        ("dig", {"domain": "example.com"}, ALL),
    ],
)
def test_rejections_never_reach_runner(name, args, allowed):
    runner = FakeRunner()
    tb = box(runner, allowed=allowed)
    view = tb.call(name, args)
    assert view.probe_id is None and view.error
    assert runner.calls == [] and len(tb.rejected) == 1 and tb.record == []


def test_garbage_call_is_rejected_not_raised():
    runner = FakeRunner()
    tb = box(runner)
    assert tb.call(None, None).probe_id is None
    assert tb.call("dig", '{"domain":"shop.lab"}').probe_id is None
    assert runner.calls == [] and len(tb.rejected) == 2


def test_tool_limit():
    tb = box(FakeRunner(), limits=Limits(max_tool_calls=1))
    assert tb.call("dig", DIG).probe_id == "p1"
    assert tb.call("dig", DIG).probe_id is None
    assert tb.remaining() == 0


def test_time_limit():
    times = iter([0.0, 301.0])
    runner = FakeRunner()
    tb = box(runner, limits=Limits(max_seconds=300), clock=lambda: next(times))
    assert tb.call("dig", DIG).probe_id is None and runner.calls == []


@pytest.mark.parametrize(
    "runner",
    [
        FakeRunner(ProbeResult(False, error="지원하지 않는 도구")),
        FakeRunner(error=RuntimeError("x")),
    ],
)
def test_runner_failure_and_exception_are_rejections(runner):
    tb = box(runner)
    view = tb.call("dig", DIG)
    assert view.probe_id is None and view.error and tb.record == []


def test_no_runner_rejects():
    tb = box(None)
    assert tb.call("dig", DIG).probe_id is None and tb.record == []


def test_output_truncated():
    tb = box(FakeRunner(ProbeResult(True, "x" * 25_000, packets=7)))
    view = tb.call("capture", {"url": "http://shop.lab/"})
    out = tb.record[0].output
    assert out.startswith("x" * MAX_OUTPUT_CHARS) and "25000" in out
    assert len(out) < 25_000 and view.output == out and tb.record[0].packets == 7


def test_closed_toolbox_rejects_calls():
    runner = FakeRunner()
    tb = box(runner)
    tb.close()
    assert tb.call("dig", DIG).probe_id is None and runner.calls == []


def test_specs_lists_only_usable_tools():
    tb = box(FakeRunner(), lab=replace(LAB, enabled_tools=["dig"]))
    assert [s["function"]["name"] for s in tb.specs()] == ["dig"]
