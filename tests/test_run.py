"""고정 세트를 심판으로 반복 실행하는 run 명령 테스트."""

import os
from pathlib import Path

import pytest

from netrunner.__main__ import DEFAULT_LAB, main, make_lab, run_fixed_rounds
from netrunner.diagnoser import ReplayDiagnoser
from netrunner.docker_lab import DockerLab
from netrunner.lab import MockLab
from netrunner.referee import Referee
from netrunner.report import load_records, summarize
from netrunner.validate import LabInfo

ROOT = Path(__file__).resolve().parent.parent
FI_FILES = sorted((ROOT / "fixtures" / "first_integration").glob("*.scenario.json"))


def records_in(out: Path) -> list[dict]:
    (path,) = out.glob("rounds-*.jsonl")
    return load_records(path)


def test_run_repeats_fixed_set_through_referee(tmp_path, capsys):
    assert main(["run", "--impl", "mock", "--repeat", "2", "--out", str(tmp_path)]) == 0
    recs = records_in(tmp_path)
    assert [r["scenario_id"] for r in recs] == ["FI-01", "FI-02"] * 2
    assert all(r["status"] == "scored" and r["reset_verified"] for r in recs)
    assert "증상 재현" in capsys.readouterr().out


def test_demo_is_one_mock_pass(tmp_path):
    assert main(["demo", "--out", str(tmp_path)]) == 0
    assert [r["scenario_id"] for r in records_in(tmp_path)] == ["FI-01", "FI-02"]


def test_run_stops_after_unverified_reset():
    referee = Referee(MockLab(broken_reset=True), ReplayDiagnoser([]), LabInfo.load(DEFAULT_LAB))
    recs = run_fixed_rounds(referee, FI_FILES, repeat=3)
    assert len(recs) == 1 and recs[0]["reset_verified"] is False


def test_docker_impl_builds_docker_lab_from_lab_config():
    assert isinstance(make_lab("docker", DEFAULT_LAB), DockerLab)


@pytest.mark.skipif(os.environ.get("NETRUNNER_INTEGRATION") != "1", reason="Docker 실험망 필요")
def test_real_lab_reproduces_and_resets_every_round(tmp_path):
    # 11주차 완료 기준: D2·T2를 반복해도 매번 재현되고 정상화된다.
    assert main(["run", "--impl", "docker", "--repeat", "3", "--out", str(tmp_path)]) == 0
    recs = records_in(tmp_path)
    assert len(recs) == 6
    assert all(r["status"] == "scored" and r["reset_verified"] for r in recs), [
        (r["scenario_id"], r["invalid_reason"]) for r in recs
    ]


def test_mock_run_keeps_metrics(tmp_path):
    assert main(["run", "--impl", "mock", "--repeat", "3", "--out", str(tmp_path)]) == 0
    recs = records_in(tmp_path)
    (row,) = summarize(recs)
    assert row["rounds"] == "6/6 (100%)" and row["cause_acc"] == "6/6 (100%)"
    assert row["evidence"] == "9/15 (60%)" and row["evidence_unverified"] == 6
    assert row["tool_calls_med"] == 3.5
    assert row["repro"] == "6/6 (100%)" and row["reset_ok"] == "6/6 (100%)"
    assert all(r["probes"] for r in recs)
