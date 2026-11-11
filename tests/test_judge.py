import json
from pathlib import Path

import pytest

from netrunner.breaker import FixedBreaker
from netrunner.contracts import ContractError, Diagnosis, Limits, Scenario, read_json
from netrunner.diagnoser import ReplayDiagnoser
from netrunner.faults import FAULTS
from netrunner.lab import MockLab
from netrunner.referee import Referee, feedback_for
from netrunner.report import render_html, summarize
from netrunner.scoring import score
from netrunner.validate import LabInfo, find_clues, validate

ROOT = Path(__file__).resolve().parent.parent
FI = ROOT / "fixtures" / "first_integration"
EX = ROOT / "fixtures" / "scoring_examples"
LAB = LabInfo.load(ROOT / "config" / "lab.json")
LAB.enabled_faults = list(FAULTS)  # 심판 규칙은 아직 실험망에 구현되지 않은 장애도 검사한다.

# 자주 쓰는 고정 입력. 테스트에서 바꿀 때는 dict(R2_DIAG, **patch)처럼 복사한다.
R1_SC, R1_DIAG = (
    read_json(FI / "r1_dns_old_ip.scenario.json"),
    read_json(FI / "r1_dns_old_ip.diagnosis.json"),
)
R2_SC, R2_DIAG = (
    read_json(FI / "r2_tcp_refused.scenario.json"),
    read_json(FI / "r2_tcp_refused.diagnosis.json"),
)
X1_SC = read_json(EX / "x1_wrong_cause_and_fake_evidence.scenario.json")
X1_DIAG = read_json(EX / "x1_wrong_cause_and_fake_evidence.diagnosis.json")
X2_SC, X2_DIAG = (
    read_json(EX / "x2_masked_second_fault.scenario.json"),
    read_json(EX / "x2_masked_second_fault.diagnosis.json"),
)
X3_SC = read_json(EX / "x3_rejected_proposal.scenario.json")


def scenario(**kw) -> Scenario:
    d = {
        "scenario_id": "T",
        "target_url": "http://shop.lab/",
        "complaint": "사이트가 열리지 않아요.",
        "faults": [{"id": "T2", "params": {"port": 80}}],
    }
    d.update(kw)
    return Scenario.from_dict(d)


def diagnosis(findings, trace=None) -> Diagnosis:
    return Diagnosis.from_dict(
        {"round_id": "r", "model": "m", "method": "C", "findings": findings, "trace": trace or []}
    )


def score_of(sc: dict, diag: dict, observable=(True,), **kw):
    return score(
        Scenario.from_dict(sc),
        list(observable),
        Diagnosis.from_dict(diag),
        kw.pop("elapsed_s", 1.0),
        **kw,
    )


# ---------------------------------------------------------------- 검증


def test_fixture_scenarios_pass_validation():
    for sc in (R1_SC, R2_SC):
        assert validate(Scenario.from_dict(sc), LAB).ok


def test_rejected_proposal_reports_range_and_clue():
    v = validate(Scenario.from_dict(X3_SC), LAB)
    assert not v.ok
    assert any("loss_pct" in e for e in v.errors)
    assert any("단서" in e for e in v.errors)


@pytest.mark.parametrize(
    "text",
    [
        "IP가 이상해요",
        "인증서 경고가 떠요",
        "mtu 문제 같아요",
        "포트가 막힌 듯",
        "it redirects forever",
        "certificates warning",
        "couldn't resolve it",
        "resolving takes ages",
        "some ports closed",
        "packets vanish",
    ],
)
def test_clue_words_detected_with_particles_and_inflections(text):
    assert find_clues(text)


@pytest.mark.parametrize(
    "text",
    ["사이트가 열리지 않아요.", "한참 있다가 열리기 시작해요", "ZIP 파일 다운로드가 멈춰요"],
)
def test_plain_complaints_have_no_clues(text):
    assert not find_clues(text)


@pytest.mark.parametrize(
    "faults, needle",
    [
        ([{"id": "T2", "params": {"port": 443}}], "포트"),
        ([{"id": "D1", "params": {"domain": "news.lab"}}], "도메인"),
        ([{"id": "D2", "params": {"domain": "shop.lab", "ip": "10.20.0.10"}}], "정상 서버"),
        ([{"id": "D2", "params": {"domain": "shop.lab", "ip": "8.8.8.8"}}], "대역"),
        ([{"id": "T1", "params": {"port": 80}}, {"id": "T2", "params": {"port": 80}}], "함께"),
        ([{"id": "X9"}], "허용 목록"),
        ([{"id": "N1", "params": {"delay_ms": 100, "bogus": 1}}], "정의되지"),
        ([{"id": "N1", "params": {}}], "필수"),
        ([], "1~3"),
    ],
)
def test_validation_rules(faults, needle):
    v = validate(scenario(faults=faults), LAB)
    assert not v.ok and any(needle in e for e in v.errors), v.errors


def test_fault_not_yet_enabled_in_lab_is_rejected():
    lab = LabInfo(LAB.domains, LAB.web_ip, LAB.subnet, enabled_faults=["D2", "T2"])
    v = validate(scenario(faults=[{"id": "S1"}]), lab)
    assert any("구현되지" in e for e in v.errors)


def test_malformed_target_url_is_rejected_not_crash():
    v = validate(scenario(target_url="http://shop.lab:99999/"), LAB)
    assert not v.ok and "형식 오류" in v.errors[0]
    v = validate(scenario(target_url="http://[::1/"), LAB)
    assert not v.ok and "형식 오류" in v.errors[0]
    assert not validate(scenario(target_url="ftp://shop.lab/"), LAB).ok
    assert not validate(scenario(target_url="http://shop.lab:0/"), LAB).ok


def test_integer_ip_param_is_rejected():
    spec = {"id": "D2", "params": {"domain": "shop.lab", "ip": 167772170}}
    v = validate(scenario(faults=[spec]), LAB)
    assert any("IP 형식이 아님" in e for e in v.errors)


def test_tls_and_sni_faults_require_https():
    v = validate(scenario(target_url="http://shop.lab/", faults=[{"id": "S1"}]), LAB)
    assert any("https" in e for e in v.errors)
    v = validate(scenario(target_url="https://shop.lab/", faults=[{"id": "S1"}]), LAB)
    assert v.ok


def test_masked_fault_named_in_finding_is_false_positive():
    # D3가 T1을 가린다. T1을 짚은 진단은 짝이 없어 오탐이어야 한다.
    sc = {
        "scenario_id": "T",
        "target_url": "http://shop.lab/",
        "complaint": "사이트가 열리지 않아요.",
        "faults": [{"id": "D3", "params": {"domain": "shop.lab"}}, {"id": "T1"}],
    }
    s = score_of(
        sc,
        {
            "round_id": "r",
            "model": "m",
            "method": "C",
            "findings": [{"stage": "TCP", "cause": "무응답 차단"}],
        },
        observable=(True, False),
    )
    assert s.faults[1].verdict == "masked"
    assert s.false_positives == ["TCP / 무응답 차단"]


def test_dns_wrong_ip_masks_later_tcp_fault_but_not_path_fault():
    v = validate(
        scenario(
            faults=[
                {"id": "D2", "params": {"domain": "shop.lab", "ip": "10.20.0.99"}},
                {"id": "T2", "params": {"port": 80}},
                {"id": "N1", "params": {"delay_ms": 200}},
            ]
        ),
        LAB,
    )
    assert v.ok
    assert v.observable == [True, False, True]


def test_sni_block_masks_certificate_fault():
    v = validate(
        scenario(
            target_url="https://shop.lab/",
            faults=[{"id": "B1", "params": {"domain": "shop.lab"}}, {"id": "S1"}],
        ),
        LAB,
    )
    assert v.observable == [True, False]


# ---------------------------------------------------------------- 채점


def test_first_integration_round1_scores_correct():
    s = score_of(R1_SC, R1_DIAG)
    assert [f.verdict for f in s.faults] == ["correct"]
    assert s.cause_correct == 1 and not s.false_positives
    # 예전 IP가 보이는 dig 출력은 자동으로 지지, 다른 DNS·직접 요청은 사람이 확인
    assert [c.supported for c in s.evidence] == [True, None, None]
    assert [c.probe_id for c in s.evidence] == ["p2", "p3", "p4"]


def test_wrong_cause_fake_packet_and_unsupported_false_positive():
    s = score_of(X1_SC, X1_DIAG)
    assert s.faults[0].verdict == "cause_wrong"
    assert (s.stage_correct, s.cause_correct) == (1, 0)
    assert s.false_positives == ["DNS / 지연"]
    by_ref = {c.ref: c for c in s.evidence}
    assert not by_ref["pcap:p3#40"].exists  # 캡처에는 패킷이 2개뿐
    # "Connection refused"는 무응답 차단(DROP)이라는 주장을 지지하지 않는다
    assert by_ref["probe:p1"].supported is not True
    assert by_ref[""].reason == "근거 없음"
    assert s.evidence_valid == 0


def test_stage_aliases_and_wrong_stage():
    sc = scenario(faults=[{"id": "N2", "params": {"loss_pct": 10}}])
    ok = score(sc, [True], diagnosis([{"stage": "path", "cause": "packet loss"}]), 1.0)
    assert ok.faults[0].verdict == "correct"
    wrong = score(sc, [True], diagnosis([{"stage": "TCP", "cause": "연결 거부"}]), 1.0)
    assert wrong.faults[0].verdict == "stage_wrong" and wrong.faults[0].finding == 0
    assert wrong.false_positives == ["TCP / 연결 거부"]
    nothing = score(sc, [True], diagnosis([]), 1.0)
    assert nothing.faults[0].verdict == "missed" and nothing.scored_faults == 1


def test_one_wrong_stage_finding_marks_only_one_fault_stage_wrong():
    sc = scenario(
        faults=[
            {"id": "D4", "params": {"delay_ms": 800}},
            {"id": "N1", "params": {"delay_ms": 200}},
        ]
    )
    s = score(sc, [True, True], diagnosis([{"stage": "TCP", "cause": "연결 거부"}]), 1.0)
    assert sorted(f.verdict for f in s.faults) == ["missed", "stage_wrong"]


def test_masked_fault_is_excluded_and_not_counted_as_false_positive():
    s = score_of(X2_SC, X2_DIAG, observable=(True, False))
    assert [f.verdict for f in s.faults] == ["correct", "masked"]
    assert s.scored_faults == 1 and s.cause_correct == 1 and not s.false_positives


def test_time_limit_uses_judge_measurement_not_self_report():
    s = score_of(R2_SC, dict(R2_DIAG, elapsed_s=0), elapsed_s=900, limits=Limits(max_seconds=300))
    assert any("900s" in v for v in s.limit_violations) and s.elapsed_s == 900


@pytest.mark.parametrize(
    "patch",
    [
        {"trace": [{"probe_id": "p1", "tool": "tcpdump", "output": "x", "packets": "12"}]},
        {"findings": [{"stage": None, "cause": "x"}]},
        {"findings": [{"stage": "TCP", "cause": "x", "evidence": [{"ref": 3}]}]},
    ],
)
def test_malformed_diagnosis_is_contract_error(patch):
    with pytest.raises(ContractError):
        Diagnosis.from_dict(dict(R2_DIAG, **patch))
    rec, _ = run_one(diag=ReplayDiagnoser([dict(R2_DIAG, **patch)]))
    assert rec["status"] == "invalid" and "형식 오류" in rec["invalid_reason"]


# ---------------------------------------------------------------- 심판 라운드


class Boom:
    model, method = "glm-4.5", "C"

    def diagnose(self, brief):
        raise TimeoutError("GLM 응답 없음")


def run_one(lab=None, diag=None, sc=None):
    lab = lab or MockLab()
    diag = diag if diag is not None else ReplayDiagnoser([R2_DIAG])
    return Referee(lab, diag, LAB).run_round(Scenario.from_dict(sc or R2_SC)), lab


def test_round_hides_answer_from_diagnoser():
    seen = []

    class Spy:
        model, method = "fixture", "C"

        def diagnose(self, brief):
            seen.append(brief)
            return ReplayDiagnoser([R2_DIAG]).diagnose(brief)

    rec, lab = run_one(diag=Spy())
    assert rec["status"] == "scored" and rec["reset_verified"]
    assert set(vars(seen[0])) == {"round_id", "target_url", "complaint", "limits"}
    assert lab.calls[-2:] == ["reset", "health_check"]


@pytest.mark.parametrize(
    "lab, reason",
    [
        (MockLab(fail_inject={"T2"}), "주입 실패"),
        (MockLab(no_symptom=True), "증상 재현 실패"),
    ],
)
def test_invalid_rounds_still_reset_lab(lab, reason):
    rec, lab = run_one(lab=lab)
    assert rec["status"] == "invalid" and reason in rec["invalid_reason"]
    assert "reset" in lab.calls and not lab.active


@pytest.mark.parametrize("where", ["inject", "verify_symptom", "health_check"])
def test_lab_exceptions_become_invalid_rounds(where):
    def boom(*a):
        raise RuntimeError("docker exec failed")

    lab = MockLab()
    setattr(lab, where, boom)
    rec, _ = run_one(lab=lab)
    assert rec["status"] == "invalid" and rec["finished_at"]
    assert any("RuntimeError" in s["detail"] for s in rec["lab_log"])


def test_diagnoser_crash_is_invalid_round_and_lab_is_reset():
    rec, _ = run_one(diag=Boom())
    assert rec["status"] == "invalid" and "TimeoutError" in rec["invalid_reason"]
    assert rec["reset_verified"]


def test_rejected_proposal_never_touches_lab():
    rec, lab = run_one(sc=X3_SC)
    assert rec["status"] == "rejected" and lab.calls == []
    assert rec["reset_verified"] is None and rec["finished_at"]


def test_match_retries_rejected_proposals_and_stops_on_dirty_lab():
    good = Scenario.from_dict(X1_SC)
    recs = Referee(MockLab(), ReplayDiagnoser([X1_DIAG]), LAB).run_match(
        FixedBreaker([Scenario.from_dict(X3_SC), good]), rounds=1
    )
    assert [r["status"] for r in recs] == ["rejected", "scored"]

    dirty = Referee(MockLab(broken_reset=True), ReplayDiagnoser([]), LAB)
    recs = dirty.run_match(FixedBreaker([good, good]), rounds=2)
    assert len(recs) == 1 and recs[0]["reset_verified"] is False


def test_breaker_cannot_label_its_rounds_as_fixed_set():
    sc = Scenario.from_dict(dict(X1_SC, origin="fixed"))
    recs = Referee(MockLab(), ReplayDiagnoser([X1_DIAG]), LAB).run_match(
        FixedBreaker([sc]), rounds=1
    )
    assert recs[0]["origin"] == "breaker" and feedback_for(recs[0]) is not None


def test_feedback_only_for_breaker_rounds():
    rec, _ = run_one()
    assert feedback_for(rec) is None  # 고정 세트는 Breaker 입력에서 제외
    rec, _ = run_one(diag=ReplayDiagnoser([X1_DIAG]), sc=X1_SC)
    fb = feedback_for(rec)
    assert (
        fb.valid
        and fb.outcomes[0].verdict == "cause_wrong"
        and fb.false_positives == ["DNS / 지연"]
    )
    rec, _ = run_one(sc=X3_SC)
    fb = feedback_for(rec)
    assert not fb.valid and "loss_pct" in fb.invalid_reason


def test_records_are_appended_as_jsonl(tmp_path):
    out = tmp_path / "rounds.jsonl"
    Referee(MockLab(), ReplayDiagnoser([R2_DIAG]), LAB, records_path=out).run_round(
        Scenario.from_dict(R2_SC)
    )
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["score"]["cause_correct"] == 1


# ---------------------------------------------------------------- 결과


def test_summary_and_html():
    recs = [run_one()[0], run_one(lab=MockLab(no_symptom=True))[0], run_one(sc=X3_SC)[0]]
    rows = summarize(recs)
    assert len(rows) == 1  # 거부된 제안은 모델 행에 들어가지 않는다
    assert rows[0]["rounds"] == "1/2 (50%)" and rows[0]["cause_acc"] == "1/1 (100%)"
    page = render_html(recs, rows)
    assert (
        "Connection refused" in page and "증상 재현 실패" in page and "실험망 사용 안 함" in page
    )


def test_summary_counts_symptom_reproduction():
    # 주입 실패와 증상 미재현은 재현 실패로, 거부된 제안은 시도에서 뺀다.
    recs = [
        run_one()[0],
        run_one(lab=MockLab(no_symptom=True))[0],
        run_one(lab=MockLab(fail_inject={"T2"}))[0],
        run_one(sc=X3_SC)[0],
    ]
    assert summarize(recs)[0]["repro"] == "1/3 (33%)"


def test_cited_probe_output_embedded_once():
    page = render_html(
        [
            run_one(
                diag=ReplayDiagnoser(
                    [
                        dict(
                            R2_DIAG,
                            findings=[
                                {
                                    "stage": "TCP",
                                    "cause": "연결 거부",
                                    "evidence": ["probe:p1", "probe:p1", "pcap:p3#1", "pcap:p3#2"],
                                }
                            ],
                        )
                    ]
                )
            )[0]
        ]
    )
    assert page.count("after 2 ms: Connection refused") == 1


def test_diagnoser_failures_count_against_that_model():
    good = ReplayDiagnoser([dict(R2_DIAG, model="glm-4.5")])
    rows = summarize([run_one(diag=good)[0], run_one(diag=Boom())[0]])
    assert len(rows) == 1 and rows[0]["rounds"] == "1/2 (50%)"


# ------------------------------------------------- 근거 자동 판정(신호)


@pytest.mark.parametrize(
    "fault_id, params, output, expected",
    [
        # 주입 지연 이상의 ping 값은 자동 지지, 정상 ping은 지지 안 함
        ("N1", {"delay_ms": 20}, "64 bytes: icmp_seq=1 time=25.3 ms", True),
        ("N1", {"delay_ms": 200}, "64 bytes: icmp_seq=1 time=412 ms", True),
        ("N1", {"delay_ms": 200}, "64 bytes: icmp_seq=1 time=0.42 ms", False),
        # 0이 아닌 손실률만 지지
        ("N2", {"loss_pct": 10}, "10% packet loss", True),
        ("N2", {"loss_pct": 1}, "0% packet loss", False),
        # 만료 판정: -checkend 출력은 "will not expire"와 구분된다
        ("S1", {}, "Certificate will expire", True),
        ("S1", {}, "Certificate will not expire", False),
        ("S1", {}, "SSL certificate problem: certificate has expired", True),
    ],
)
def test_signal_auto_judgement(fault_id, params, output, expected):
    from netrunner.faults import FAULTS
    from netrunner.scoring import _signal_hit, _signal_patterns

    patterns = _signal_patterns(FAULTS[fault_id], params)
    assert any(_signal_hit(p, floor, output) for p, floor in patterns) is expected
