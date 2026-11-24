"""고정 세트를 진단기(방식)별로 반복 실행하는 run --diagnoser 테스트."""

from __future__ import annotations

import pytest

from netrunner import __main__ as cli
from netrunner.contracts import Diagnosis
from netrunner.lab import MockLab
from netrunner.report import load_records, summarize


class _Silent:
    """원인을 답하지 않는 가짜 진단기. 모델과 방식 이름만 다르다."""

    def __init__(self, model: str, method: str):
        self.model, self.method = model, method
        self.tool_names: set[str] = set()

    def diagnose(self, brief, tools):
        self.tool_names = {s["function"]["name"] for s in tools.specs()}
        return Diagnosis(brief.round_id, self.model, self.method, [])


def _register(monkeypatch, **diagnosers) -> None:
    for name, d in diagnosers.items():
        monkeypatch.setitem(cli.DIAGNOSER_IMPLS, name, lambda _args, d=d: d)


def _run(tmp_path, *extra) -> int:
    return cli.main(["run", "--impl", "mock", "--out", str(tmp_path), *extra])


def _rounds(tmp_path) -> list[dict]:
    (path,) = tmp_path.glob("rounds-*.jsonl")
    return load_records(path)


def test_each_scenario_runs_once_per_diagnoser_and_repeat(monkeypatch, tmp_path):
    _register(monkeypatch, a=_Silent("stub", "A"), glm=_Silent("glm", "C"))
    assert _run(tmp_path, "--repeat", "2", "--diagnoser", "a", "--diagnoser", "glm") == 0
    recs = _rounds(tmp_path)
    # 반복 → 시나리오 → 진단기 순서로 섞어 실험망 상태 변화가 한 방식에 몰리지 않게 한다.
    once = [("FI-01", "A"), ("FI-01", "C"), ("FI-02", "A"), ("FI-02", "C")]
    assert [(r["scenario_id"], r["method"]) for r in recs] == once * 2
    rows = {(r["model"], r["method"]): r["rounds"] for r in summarize(recs)}
    assert rows == {("stub", "A"): "4/4 (100%)", ("glm", "C"): "4/4 (100%)"}


def test_each_method_gets_its_own_tools(monkeypatch, tmp_path):
    # 방식별 허용 도구는 심판이 정한다. C-1은 캡처를 쓰지 못한다.
    c, c1 = _Silent("glm", "C"), _Silent("glm", "C-1")
    _register(monkeypatch, c=c, c1=c1)
    assert _run(tmp_path, "--diagnoser", "c", "--diagnoser", "c1") == 0
    assert "capture" in c.tool_names and "capture" not in c1.tool_names


def test_run_with_diagnosers_stops_after_unverified_reset(monkeypatch, tmp_path):
    monkeypatch.setitem(cli.LAB_IMPLS, "mock", lambda _config: MockLab(broken_reset=True))
    _register(monkeypatch, a=_Silent("stub", "A"))
    assert _run(tmp_path, "--repeat", "3", "--diagnoser", "a") == 1
    assert len(_rounds(tmp_path)) == 1


def test_replay_cannot_be_mixed_with_other_diagnosers(monkeypatch, tmp_path, capsys):
    _register(monkeypatch, a=_Silent("stub", "A"))
    assert _run(tmp_path, "--diagnoser", "replay", "--diagnoser", "a") == 1
    assert "replay" in capsys.readouterr().err
    assert not list(tmp_path.glob("rounds-*.jsonl"))


def test_default_run_still_replays_saved_diagnoses(tmp_path):
    assert _run(tmp_path) == 0
    recs = _rounds(tmp_path)
    assert [r["scenario_id"] for r in recs] == ["FI-01", "FI-02"]
    assert all(r["probes"] for r in recs)  # 저장된 검사 출력을 재생했다


@pytest.mark.parametrize(
    ("name", "method"),
    [("glm-b", "B"), ("glm-c", "C"), ("glm-c1", "C-1"), ("glm-c2", "C-2")],
)
def test_run_registers_each_glm_method(name, method):
    args = cli.build_parser().parse_args(
        ["run", "--diagnoser", name, "--opencode", "test-opencode", "--model", "test/model"]
    )
    diagnoser = cli.DIAGNOSER_IMPLS[name](args)
    assert (diagnoser.method, diagnoser.model) == (method, "test/model")
    assert diagnoser._complete.binary == "test-opencode"


@pytest.mark.parametrize("name", ["glm-b", "glm-c1", "glm-c2"])
@pytest.mark.parametrize("command", ["run", "match"])
def test_glm_methods_require_a_connected_probe_runner(command, name, tmp_path, capsys):
    assert cli.main([command, "--impl", "mock", "--diagnoser", name, "--out", str(tmp_path)]) == 1
    assert "검사 실행기가 연결되지 않았습니다" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
