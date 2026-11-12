import json
from pathlib import Path

import pytest

from netrunner.breaker import BreakerError, LLMBreaker, parse_proposal
from netrunner.breaker_prompt import build_prompt
from netrunner.contracts import BreakerFeedback, FaultOutcome, FaultSpec, read_json
from netrunner.diagnoser import ReplayDiagnoser
from netrunner.lab import MockLab
from netrunner.referee import Referee
from netrunner.validate import CLUE_WORDS, LabInfo

ROOT = Path(__file__).resolve().parent.parent
FI = ROOT / "fixtures" / "first_integration"
LAB = LabInfo.load(ROOT / "config" / "lab.json")
TARGET = "http://shop.lab/"

GOOD = {
    "faults": [{"id": "D2", "params": {"domain": "shop.lab", "ip": "10.20.0.99"}}],
    "complaint": "사이트가 열리지 않아요.",
    "note": "테스트",
}


def fake_model(*outputs):
    """준비한 응답을 차례로 돌려주고, 받은 프롬프트를 prompts에 남긴다."""
    queue = list(outputs)
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if not queue:
            raise AssertionError("준비한 응답이 더 없음")
        return queue.pop(0)

    complete.prompts = prompts
    return complete


def as_text(d) -> str:
    return json.dumps(d, ensure_ascii=False)


def test_proposal_gets_breaker_origin_fixed_target_and_own_id():
    b = LLMBreaker(fake_model(as_text({**GOOD, "target_url": "http://news.lab/"})), LAB, TARGET)
    sc = b.propose([])
    assert sc.origin == "breaker"
    assert sc.target_url == TARGET  # 모델이 내도 대상은 바꾸지 못한다
    assert sc.scenario_id == "BR-001"
    assert sc.faults[0].id == "D2"
    assert b.stats.accepted == 1 and b.stats.calls == 1


def test_prompt_lists_enabled_faults_and_clue_words_without_inject_text():
    prompt = build_prompt([], TARGET, LAB)
    for fid in LAB.enabled_faults:
        assert f"- {fid}:" in prompt
    for word in CLUE_WORDS:
        assert word in prompt
    assert "dnsmasq" not in prompt  # 주입 방법은 Breaker 프롬프트에 넣지 않는다
    assert "지난 라운드 없음" in prompt


def test_prompt_summarizes_history_verdicts():
    fb = BreakerFeedback(
        scenario_id="BR-007",
        faults=[FaultSpec("T2", {"port": 80})],
        complaint="접속이 안 돼요.",
        valid=True,
        outcomes=[FaultOutcome("T2", "TCP / 연결 거부", "cause_wrong")],
        false_positives=["DNS / 지연"],
    )
    prompt = build_prompt([fb], TARGET, LAB)
    assert "BR-007" in prompt
    assert "T2 TCP / 연결 거부=cause_wrong" in prompt
    assert "오탐: DNS / 지연" in prompt


def test_unparsable_output_is_retried_with_reason():
    model = fake_model("이건 JSON이 아님", as_text(GOOD))
    b = LLMBreaker(model, LAB, TARGET)
    assert b.propose([]).faults[0].id == "D2"
    assert b.stats.unparsable == 1 and b.stats.calls == 2
    assert "직전 제안이 거부됨: 형식 오류" in model.prompts[1]


def test_clue_word_in_complaint_is_rejected_then_retried():
    bad = {**GOOD, "complaint": "DNS가 이상한지 사이트가 안 열려요."}
    model = fake_model(as_text(bad), as_text(GOOD))
    b = LLMBreaker(model, LAB, TARGET)
    b.propose([])
    assert b.stats.invalid == 1
    assert "원인 단서" in model.prompts[1]


def test_duplicate_proposal_is_skipped_and_counted():
    other = {
        **GOOD,
        "complaint": "사이트가 느려요.",
        "faults": [{"id": "T2", "params": {"port": 80}}],
    }
    b = LLMBreaker(fake_model(as_text(GOOD), as_text(GOOD), as_text(other)), LAB, TARGET)
    assert b.propose([]).faults[0].id == "D2"
    assert b.propose([]).faults[0].id == "T2"
    assert b.stats.duplicate == 1 and b.stats.accepted == 2
    assert b.stats.rejected_ratio == pytest.approx(1 / 3)


def test_gives_up_after_max_attempts():
    bad = as_text({**GOOD, "complaint": "IP가 잘못된 것 같아요."})
    b = LLMBreaker(fake_model(bad, bad, bad), LAB, TARGET, max_attempts=3)
    with pytest.raises(BreakerError):
        b.propose([])
    assert b.stats.calls == 3 and b.stats.accepted == 0


@pytest.mark.parametrize(
    "raw",
    [
        f"```json\n{as_text(GOOD)}\n```",
        f"좋은 제안입니다.\n{as_text(GOOD)}\n이상입니다.",
    ],
)
def test_fenced_or_wrapped_json_is_parsed(raw):
    assert parse_proposal(raw) == GOOD


@pytest.mark.parametrize("raw", ["", "[1, 2]", "{not json}", "```json\n```"])
def test_parse_proposal_rejects_non_objects(raw):
    with pytest.raises(ValueError):
        parse_proposal(raw)


def test_referee_runs_llm_breaker_round_and_marks_origin_breaker():
    r1 = read_json(FI / "r1_dns_old_ip.diagnosis.json")
    referee = Referee(MockLab(), ReplayDiagnoser([r1]), LAB)
    breaker = LLMBreaker(fake_model(as_text(GOOD)), LAB, TARGET)
    records = referee.run_match(breaker, rounds=1)
    assert len(records) == 1
    assert records[0]["origin"] == "breaker"
    assert records[0]["status"] == "scored"
    assert records[0]["reset_verified"] is True


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "complaint": 5},
        {**GOOD, "complaint": None},
        {**GOOD, "faults": [{"id": ["D2"], "params": {}}]},
        {**GOOD, "faults": [{"id": "D2", "params": ["x"]}]},
        {**GOOD, "faults": "D2"},
    ],
)
def test_wrong_types_are_counted_and_retried_not_crashed(bad):
    b = LLMBreaker(fake_model(as_text(bad), as_text(GOOD)), LAB, TARGET)
    assert b.propose([]).faults[0].id == "D2"
    assert b.stats.unparsable == 1 and b.stats.calls == 2


def test_braces_in_prose_do_not_break_parsing():
    raw = f"장애는 {{D2}} 입니다.\n{as_text(GOOD)}\n참고 {{끝}}"
    assert parse_proposal(raw) == GOOD


def test_omitted_optional_param_counts_as_duplicate():
    # D4의 domain은 생략하면 대상 도메인(shop.lab)과 같다.
    a = {**GOOD, "faults": [{"id": "D4", "params": {"delay_ms": 500}}]}
    b_ = {**GOOD, "faults": [{"id": "D4", "params": {"domain": "shop.lab", "delay_ms": 500}}]}
    b = LLMBreaker(fake_model(as_text(a), as_text(b_), as_text(GOOD)), LAB, TARGET)
    b.propose([])
    assert b.propose([]).faults[0].id == "D2"
    assert b.stats.duplicate == 1
