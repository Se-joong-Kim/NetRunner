"""Breaker 증상 문장 규칙과 거부 뒤 재시도 문장 테스트. 모델 호출은 가짜로 바꾼다."""

from __future__ import annotations

import json
from pathlib import Path

from netrunner.breaker import LLMBreaker
from netrunner.validate import LabInfo

ROOT = Path(__file__).resolve().parents[1]
LAB = LabInfo.load(ROOT / "config" / "lab.json")
TARGET = "http://shop.lab/"
T2_WITH_URL = json.dumps(
    {
        "faults": [{"id": "T2", "params": {"port": 80}}],
        "complaint": "http://shop.lab/ 에 접속이 안 된다",
    },
    ensure_ascii=False,
)
T2_GOOD = json.dumps(
    {
        "faults": [{"id": "T2", "params": {"port": 80}}],
        "complaint": "shop.lab 쇼핑몰 사이트가 열리지 않는다",
    },
    ensure_ascii=False,
)


def fake_model(*outputs):
    queue = list(outputs)
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return queue.pop(0)

    complete.prompts = prompts
    return complete


def test_prompt_forbids_protocol_notation_and_shows_a_bad_and_a_good_example():
    complete = fake_model(T2_GOOD)
    LLMBreaker(complete, LAB, TARGET).propose([])
    prompt = complete.prompts[0]
    assert "프로토콜과 URL 표기(http, https, ://)를 쓰지 않는다" in prompt
    assert "나쁜 예" in prompt and "좋은 예" in prompt


def test_clue_rejection_tells_the_model_to_drop_the_word_and_retry():
    complete = fake_model(T2_WITH_URL, T2_GOOD)
    breaker = LLMBreaker(complete, LAB, TARGET)
    breaker.propose([])
    assert breaker.stats.invalid == 1
    assert "그 단어를 빼고 같은 장애로 다시 쓴다" in complete.prompts[1]
