"""Breaker 대결 실행 명령(match) 테스트. GLM 호출은 가짜로 바꿔 끼운다.

모든 테스트는 --out을 임시 폴더로 준다.
"""

from __future__ import annotations

import json

from netrunner import __main__ as cli
from netrunner.breaker import CompletionError
from netrunner.contracts import Limits, RoundBrief
from netrunner.lab import MockLab
from netrunner.report import load_records, summarize_breaker


def _d2(i: int) -> str:
    """서로 다른 D2 제안 하나. 대역 안의 주소 번호만 바꾼다."""
    params = {"domain": "shop.lab", "ip": f"10.20.0.{100 + i}"}
    return json.dumps(
        {"faults": [{"id": "D2", "params": params}], "complaint": "사이트가 열리지 않는다"},
        ensure_ascii=False,
    )


def _use_fake(monkeypatch, reply) -> None:
    """OpencodeComplete 자리에 가짜 호출을 넣는다."""
    monkeypatch.setattr(cli, "OpencodeComplete", lambda **_kw: reply)


def _replies(*items):
    queue = list(items)
    return lambda _prompt: queue.pop(0)


def _match(tmp_path, *extra) -> int:
    return cli.main(["match", "--impl", "mock", "--out", str(tmp_path), *extra])


def _rounds(tmp_path) -> list[dict]:
    (path,) = tmp_path.glob("rounds-match-*.jsonl")
    return load_records(path)


def test_match_runs_breaker_proposals_through_referee(monkeypatch, tmp_path, capsys):
    _use_fake(monkeypatch, _replies(_d2(1), _d2(2)))
    assert _match(tmp_path, "--rounds", "2") == 0
    recs = _rounds(tmp_path)
    assert [r["origin"] for r in recs] == ["breaker", "breaker"]
    assert all(r["status"] == "scored" and r["reset_verified"] for r in recs)
    assert {r["model"] for r in recs} == {"none"}  # 자리표시 진단기
    assert list(tmp_path.glob("breaker-*.jsonl"))  # Breaker 호출 기록
    assert list(tmp_path.glob("report-match-*.html"))
    assert "증상 재현 2/2" in capsys.readouterr().out


def test_proposal_failure_is_recorded_and_exits_with_one(monkeypatch, tmp_path, capsys):
    def broken(_prompt):
        raise CompletionError("연결 실패")

    _use_fake(monkeypatch, broken)
    assert _match(tmp_path, "--rounds", "2") == 1
    assert [r["status"] for r in _rounds(tmp_path)] == ["proposal_failed"]
    assert "제안 실패 1" in capsys.readouterr().out


def test_match_stops_when_reset_is_not_verified(monkeypatch, tmp_path):
    monkeypatch.setitem(cli.LAB_IMPLS, "mock", lambda _config: MockLab(broken_reset=True))
    _use_fake(monkeypatch, _replies(_d2(1), _d2(2), _d2(3)))
    assert _match(tmp_path, "--rounds", "3") == 1
    recs = _rounds(tmp_path)
    assert len(recs) == 1 and recs[0]["reset_verified"] is False


def test_none_diagnoser_answers_nothing():
    d = cli.DIAGNOSER_IMPLS["none"](None)
    brief = RoundBrief("r1", "http://shop.lab/", "사이트가 열리지 않는다", Limits())
    out = d.diagnose(brief, None)
    assert (d.model, out.round_id, out.findings) == ("none", "r1", [])


def _rec(status, reproduced=False, reset=True, reason=""):
    log = [{"step": "verify_symptom", "ok": reproduced}] if status != "rejected" else []
    return {
        "origin": "breaker",
        "status": status,
        "lab_log": log,
        "reset_verified": None if status in ("rejected", "proposal_failed") else reset,
        "invalid_reason": reason,
    }


def test_summarize_breaker_counts_each_outcome():
    recs = [
        _rec("scored", reproduced=True),
        _rec("invalid", reason="증상 재현 실패"),
        _rec("invalid", reason="증상 재현 실패", reset=False),
        _rec("rejected"),
        _rec("proposal_failed"),
    ]
    s = summarize_breaker(recs)
    assert s["proposals"] == 4 and s["rejected"] == 1 and s["lab_rounds"] == 3
    assert s["reproduced"] == 1 and s["reset_ok"] == 2 and s["proposal_failed"] == 1
    assert s["invalid_reasons"] == {"증상 재현 실패": 2}
