"""결과 화면의 오진 유형 요약과 반복 측정 그래프 테스트."""

from __future__ import annotations

from netrunner import __main__ as cli
from netrunner.contracts import Diagnosis, Status
from netrunner.report import (
    false_positive_rows,
    misdiagnosis_rows,
    misdiagnosis_section,
    repeat_rows,
    repeat_section,
)

D2 = ("D2", "DNS / 잘못된 주소")
T2 = ("T2", "TCP / 연결 거부")


def _rec(method, scenario, verdicts, fps=(), status=Status.SCORED, origin="fixed"):
    """채점 결과만 필요한 만큼 채운 라운드 기록.

    verdicts는 (장애, 판정) 짝 목록이다. 정답이면 단계·원인을 모두
    맞힌 것으로, 원인 틀림이면 단계만 맞힌 것으로 센다.
    """
    faults = [{"id": f[0], "label": f[1], "verdict": v, "finding": None} for f, v in verdicts]
    score = {
        "faults": faults,
        "false_positives": list(fps),
        "stage_correct": sum(v in ("correct", "cause_wrong") for _, v in verdicts),
        "cause_correct": sum(v == "correct" for _, v in verdicts),
        "scored_faults": sum(v != "masked" for _, v in verdicts),
    }
    return {
        "status": status,
        "model": "glm" if method != "A" else "rules",
        "method": method,
        "origin": origin,
        "scenario_id": scenario,
        "score": score if status == Status.SCORED else None,
    }


def test_misdiagnosis_rows_count_verdicts_per_method_and_fault():
    recs = [
        _rec("A", "s1", [(D2, "correct")]),
        _rec("A", "s1", [(D2, "cause_wrong")]),
        _rec("C", "s1", [(D2, "missed")]),
        _rec("C", "s2", [(T2, "correct")]),
        # 거부·무효 라운드는 채점이 없으므로 세지 않는다.
        _rec("C", "s3", [], status=Status.REJECTED),
        _rec("C", "s1", [], status=Status.INVALID),
    ]
    rows = {(r["method"], r["fault"]): r for r in misdiagnosis_rows(recs)}
    assert set(rows) == {("A", "D2 DNS / 잘못된 주소"), ("C", "D2 DNS / 잘못된 주소"),
                         ("C", "T2 TCP / 연결 거부")}  # fmt: skip
    a_d2 = rows[("A", "D2 DNS / 잘못된 주소")]
    assert (a_d2["correct"], a_d2["cause_wrong"], a_d2["missed"]) == (1, 1, 0)
    assert a_d2["model"] == "rules" and a_d2["origin"] == "fixed"
    assert rows[("C", "D2 DNS / 잘못된 주소")]["missed"] == 1


def test_false_positive_rows_count_labels_per_method():
    recs = [
        _rec("C", "s1", [(D2, "correct")], fps=["DNS / 지연"]),
        _rec("C", "s2", [(T2, "correct")], fps=["DNS / 지연", "TCP / 연결 거부"]),
        _rec("A", "s1", [(D2, "correct")]),
    ]
    rows = [(r["method"], r["label"], r["count"]) for r in false_positive_rows(recs)]
    assert rows == [("C", "DNS / 지연", 2), ("C", "TCP / 연결 거부", 1)]


def test_repeat_index_is_the_occurrence_of_each_scenario_per_method():
    # run --repeat 2의 기록 순서: 반복 → 시나리오 → 진단기
    recs = [
        _rec("A", "s1", [(D2, "correct")]),
        _rec("C", "s1", [(D2, "missed")]),
        _rec("A", "s2", [(T2, "cause_wrong")]),
        _rec("C", "s2", [(T2, "correct")]),
        _rec("A", "s1", [(D2, "correct")]),
        _rec("C", "s1", [(D2, "correct")]),
        _rec("A", "s2", [(T2, "correct")]),
        # 2회차 s2의 C 라운드는 무효라 채점에서 빠지지만 회차는 센다.
        _rec("C", "s2", [], status=Status.INVALID),
    ]
    rows = {(r["method"], r["repeat"]): r for r in repeat_rows(recs)}
    assert set(rows) == {("A", 1), ("A", 2), ("C", 1), ("C", 2)}
    assert (rows[("A", 1)]["cause"], rows[("A", 1)]["stage"]) == ((1, 2), (2, 2))
    assert rows[("A", 2)]["cause"] == (2, 2)
    assert rows[("C", 1)]["cause"] == (1, 2)
    assert rows[("C", 2)]["cause"] == (1, 1)


def test_rows_are_ordered_by_method_first():
    # 방식 A의 모델 이름(rules)이 GLM보다 뒤에 와도 A, B, C 순서로 읽혀야 한다.
    recs = [_rec("C", "s1", [(D2, "correct")]), _rec("A", "s1", [(D2, "correct")])]
    assert [r["method"] for r in misdiagnosis_rows(recs)] == ["A", "C"]
    assert [r["method"] for r in repeat_rows(recs)] == ["A", "C"]


def test_misdiagnosis_section_lists_faults_and_false_positives():
    recs = [
        _rec("C", "s1", [(D2, "missed")], fps=["DNS / 지연"]),
        _rec("C", "s2", [(T2, "correct")]),
    ]
    html = misdiagnosis_section(recs)
    assert "오진 유형" in html
    assert "D2 DNS / 잘못된 주소" in html and "DNS / 지연" in html


def test_misdiagnosis_section_is_empty_without_scored_rounds():
    assert misdiagnosis_section([_rec("C", "s1", [], status=Status.REJECTED)]) == ""


def test_repeat_chart_appears_only_with_two_or_more_repeats():
    once = [_rec("C", "s1", [(D2, "correct")])]
    twice = once + [_rec("C", "s1", [(D2, "missed")])]
    assert repeat_section(once) == ""
    html = repeat_section(twice)
    assert "<svg" in html and "반복 측정" in html
    # 막대마다 개수와 비율을 같이 적는다.
    assert "1/1 (100%)" in html and "0/1 (0%)" in html


class _Silent:
    """원인을 답하지 않는 가짜 진단기(모든 장애를 놓친다)."""

    model, method = "silent", "C"

    def diagnose(self, brief, tools):
        return Diagnosis(brief.round_id, self.model, self.method, [])


def test_run_report_page_has_misdiagnosis_table_and_repeat_chart(monkeypatch, tmp_path):
    monkeypatch.setitem(cli.DIAGNOSER_IMPLS, "silent", lambda _args: _Silent())
    argv = ["run", "--impl", "mock", "--out", str(tmp_path), "--repeat", "2"]
    assert cli.main([*argv, "--diagnoser", "silent"]) == 0
    (page,) = tmp_path.glob("report-*.html")
    text = page.read_text(encoding="utf-8")
    assert "오진 유형" in text and "<svg" in text
