"""출제 전략(breaker_strategy)과 힌트가 Breaker 프롬프트에 들어가는 경로 테스트.

모델 호출은 가짜 complete로 바꿔 끼워 결정적으로 돌린다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from netrunner.breaker import BreakerError, CompletionError, LLMBreaker
from netrunner.breaker_log import CallJournal
from netrunner.breaker_strategy import (
    exhausted_faults,
    plan,
    refused_by_lab,
    refused_values,
    unit_of,
    units,
)
from netrunner.contracts import BreakerFeedback, FaultOutcome, FaultSpec, Scenario
from netrunner.faults import FAULTS
from netrunner.validate import LabInfo

ROOT = Path(__file__).resolve().parents[1]
LAB = LabInfo.load(ROOT / "config" / "lab.json")
TARGET = "http://shop.lab/"
D2_LABEL = FAULTS["D2"].label
T2_LABEL = FAULTS["T2"].label
D2_EMPTY = FaultSpec("D2", {"domain": "shop.lab", "ip": "10.20.0.99"})
D2_HOST = FaultSpec("D2", {"domain": "shop.lab", "ip": "10.20.0.54"})
D2_WEB = FaultSpec("D2", {"domain": "shop.lab", "ip": "10.20.0.10"})
T2 = FaultSpec("T2", {"port": 80})
GOOD_D2 = json.dumps(
    {
        "faults": [{"id": "D2", "params": {"domain": "shop.lab", "ip": "10.20.0.99"}}],
        "complaint": "사이트가 열리지 않아요.",
    },
    ensure_ascii=False,
)


def fb(faults, outcomes, valid=True, false_positives=()):
    """채점이 끝난 라운드 기록 하나. outcomes는 (장애 ID, 라벨, 판정) 목록이다."""
    return BreakerFeedback(
        scenario_id="R",
        faults=list(faults),
        complaint="사이트가 열리지 않아요.",
        valid=valid,
        invalid_reason="" if valid else "무효 사유",
        outcomes=[FaultOutcome(id=i, label=label, verdict=v) for i, label, v in outcomes],
        false_positives=list(false_positives),
    )


def rate(hints, unit):
    return dict(hints.ranked)[unit]


def fake_model(*outputs):
    """준비한 응답을 차례로 돌려주고, 받은 프롬프트를 prompts에 남긴다."""
    queue = list(outputs)
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return queue.pop(0)

    complete.prompts = prompts
    return complete


def test_units_are_enabled_faults_and_only_the_empty_address_class():
    assert units(LAB) == ["D2/ip=empty", "T2"]


@pytest.mark.parametrize(
    ("fault", "refused"),
    [(D2_EMPTY, False), (D2_HOST, True), (D2_WEB, True), (T2, False)],
)
def test_lab_refuses_host_and_web_addresses(fault, refused):
    assert refused_by_lab(fault, LAB) is refused


def test_host_address_belongs_to_its_own_class_outside_the_units():
    assert unit_of(D2_HOST, LAB) == "D2/ip=host"
    assert unit_of(D2_EMPTY, LAB) == "D2/ip=empty"
    assert unit_of(T2, LAB) == "T2"
    assert "D2/ip=host" not in units(LAB)


def test_no_history_gives_every_unit_the_neutral_rate_and_all_untried():
    hints = plan([], LAB, TARGET)
    assert hints.ranked == (("D2/ip=empty", 0.5), ("T2", 0.5))
    assert hints.untried == ("D2/ip=empty", "T2")
    assert hints.exhausted == ()
    assert hints.over_predicted == ()


def test_one_wrong_attempt_moves_the_smoothed_rate_to_two_thirds():
    history = [fb([D2_EMPTY], [("D2", D2_LABEL, "cause_wrong")])]
    hints = plan(history, LAB, TARGET)
    assert rate(hints, "D2/ip=empty") == pytest.approx(2 / 3)
    assert hints.ranked[0][0] == "D2/ip=empty"
    assert hints.untried == ("T2",)


def test_correct_verdict_is_an_attempt_but_not_a_wrong_one():
    history = [fb([D2_EMPTY], [("D2", D2_LABEL, "correct")])]
    hints = plan(history, LAB, TARGET)
    assert rate(hints, "D2/ip=empty") == pytest.approx(1 / 3)
    assert hints.untried == ("T2",)


def test_masked_verdict_is_not_counted_as_an_attempt():
    history = [fb([D2_EMPTY], [("D2", D2_LABEL, "masked")])]
    hints = plan(history, LAB, TARGET)
    assert rate(hints, "D2/ip=empty") == pytest.approx(0.5)
    assert "D2/ip=empty" in hints.untried


def test_invalid_rounds_do_not_feed_the_strategy():
    history = [fb([D2_EMPTY], [("D2", D2_LABEL, "cause_wrong")], valid=False)]
    hints = plan(history, LAB, TARGET)
    assert rate(hints, "D2/ip=empty") == pytest.approx(0.5)
    assert "D2/ip=empty" in hints.untried


def test_outcomes_of_a_multi_fault_round_go_to_each_fault_unit():
    history = [
        fb(
            [D2_EMPTY, T2],
            [("D2", D2_LABEL, "correct"), ("T2", T2_LABEL, "stage_wrong")],
        )
    ]
    hints = plan(history, LAB, TARGET, max_faults=2)
    assert rate(hints, "T2") == pytest.approx(2 / 3)
    assert rate(hints, "D2/ip=empty") == pytest.approx(1 / 3)
    assert hints.ranked[0][0] == "T2"


def test_fixed_fault_is_exhausted_after_a_valid_round_with_one_fault_limit():
    history = [fb([T2], [("T2", T2_LABEL, "cause_wrong")])]
    hints = plan(history, LAB, TARGET, max_faults=1)
    assert hints.exhausted == ("T2",)
    assert [unit for unit, _ in hints.ranked] == ["D2/ip=empty"]


def test_fixed_fault_is_not_exhausted_when_faults_can_be_combined():
    history = [fb([T2], [("T2", T2_LABEL, "cause_wrong")])]
    hints = plan(history, LAB, TARGET, max_faults=2)
    assert hints.exhausted == ()
    assert "T2" in dict(hints.ranked)


def test_fault_with_a_free_ip_parameter_is_never_exhausted():
    history = [fb([D2_EMPTY], [("D2", D2_LABEL, "cause_wrong")])]
    assert exhausted_faults(history, Scenario("", TARGET, [], ""), 1) == frozenset()


def test_over_predicted_keeps_only_known_labels_ordered_by_count():
    history = [
        fb(
            [T2],
            [("T2", T2_LABEL, "cause_wrong")],
            false_positives=[D2_LABEL, D2_LABEL, T2_LABEL, "임의 텍스트", 5],
        )
    ]
    hints = plan(history, LAB, TARGET)
    assert hints.over_predicted == (D2_LABEL, T2_LABEL)


def test_prompt_shows_strategy_after_an_exhausting_round():
    history = [fb([T2], [("T2", T2_LABEL, "cause_wrong")])]
    complete = fake_model(GOOD_D2)
    LLMBreaker(complete, LAB, TARGET, max_faults=1).propose(history)
    prompt = complete.prompts[0]
    assert "## 출제 전략" in prompt
    assert "더 낼 수 없는 장애: T2" in prompt
    assert "아직 안 낸 단위: D2 (빈 주소)" in prompt
    assert "ip=empty" not in prompt  # 내부 표기는 모델에게 보이지 않는다
    assert "- T2:" not in prompt  # 고갈된 장애는 장애 목록에서 빠진다


def test_prompt_forbids_host_addresses_as_fault_ips():
    complete = fake_model(GOOD_D2)
    LLMBreaker(complete, LAB, TARGET).propose([])
    prompt = complete.prompts[0]
    assert "주입할 수 없는 주소(실험망 호스트와 정상 서버)는 ip로 쓰지 않는다" in prompt
    assert "10.20.0.54" in prompt  # alt_dns 호스트 주소가 목록에 적혀 있다
    assert "10.20.0.10" in prompt  # 정상 서버 주소도 적혀 있다


def test_host_address_proposal_is_refused_and_the_reason_goes_back_to_the_model():
    complete = fake_model(
        json.dumps(
            {
                "faults": [{"id": "D2", "params": {"domain": "shop.lab", "ip": "10.20.0.54"}}],
                "complaint": "사이트가 열리지 않아요.",
            },
            ensure_ascii=False,
        ),
        GOOD_D2,
    )
    breaker = LLMBreaker(complete, LAB, TARGET)
    sc = breaker.propose([])
    assert sc.faults[0].params["ip"] == "10.20.0.99"
    assert breaker.stats.lab_refused == 1
    assert "실험망이 주입하지 않는 주소를 씀: 10.20.0.54" in complete.prompts[1]


def test_params_given_as_a_list_are_rejected_not_silently_converted():
    as_pairs = json.dumps(
        {"faults": [{"id": "T2", "params": [["port", 80]]}], "complaint": "열리지 않음"}
    )
    complete = fake_model(as_pairs, as_pairs, as_pairs)
    breaker = LLMBreaker(complete, LAB, TARGET)
    with pytest.raises(BreakerError):
        breaker.propose([])
    assert breaker.stats.unparsable == 3
    assert breaker.stats.accepted == 0


def test_lone_surrogate_in_a_rejected_value_does_not_break_the_journal(tmp_path):
    bad = json.dumps(
        {
            "faults": [{"id": "D2", "params": {"domain": "shop.lab", "ip": "\ud800"}}],
            "complaint": "사이트가 열리지 않아요.",
        }
    )
    complete = fake_model(bad, GOOD_D2)
    journal = CallJournal(tmp_path / "breaker-test.jsonl")
    LLMBreaker(complete, LAB, TARGET, record=journal).propose([])
    lines = (tmp_path / "breaker-test.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "\ud800" not in json.loads(lines[0])["verdict"]


def test_api_error_keeps_the_last_rejection_reason_for_the_next_attempt():
    outputs = iter(
        [
            json.dumps(
                {
                    "faults": [{"id": "D2", "params": {"domain": "shop.lab", "ip": "10.20.0.54"}}],
                    "complaint": "사이트가 열리지 않아요.",
                },
                ensure_ascii=False,
            ),
            CompletionError("연결 실패"),
            GOOD_D2,
        ]
    )
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        item = next(outputs)
        if isinstance(item, Exception):
            raise item
        return item

    LLMBreaker(complete, LAB, TARGET).propose([])
    assert "실험망이 주입하지 않는 주소를 씀: 10.20.0.54" in prompts[2]


def test_alias_false_positive_is_counted_under_its_catalog_label():
    history = [
        fb(
            [T2],
            [("T2", T2_LABEL, "cause_wrong")],
            false_positives=["DNS / wrong ip", "DNS / 잘못된 주소", "모르는 단계 / 무엇"],
        )
    ]
    hints = plan(history, LAB, TARGET)
    assert hints.over_predicted == ("DNS / 잘못된 주소",)


def test_refused_values_names_the_address_the_lab_cannot_inject():
    assert refused_values(D2_HOST, LAB) == ["10.20.0.54"]
    assert refused_values(D2_EMPTY, LAB) == []
