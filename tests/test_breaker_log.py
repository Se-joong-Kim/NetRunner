"""모델 호출 원문 기록(breaker_log, LLMBreaker.record) 테스트."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from netrunner.breaker import CompletionError, LLMBreaker
from netrunner.breaker_log import CallJournal
from netrunner.validate import LabInfo

ROOT = Path(__file__).resolve().parents[1]
LAB = LabInfo.load(ROOT / "config" / "lab.json")
TARGET = "http://shop.lab/"
GOOD = json.dumps(
    {
        "faults": [{"id": "T2", "params": {"port": 80}}],
        "complaint": "사이트가 열리지 않아요.",
    },
    ensure_ascii=False,
)


def test_each_call_is_recorded_with_prompt_hash_raw_text_and_verdict():
    records: list[dict] = []
    outputs = iter(["아무 말", GOOD])
    sent: list[str] = []

    def complete(prompt: str) -> str:
        sent.append(prompt)
        return next(outputs)

    LLMBreaker(complete, LAB, TARGET, record=records.append).propose([])
    assert [r["attempt"] for r in records] == [1, 2]
    assert [r["call"] for r in records] == [1, 2]
    assert records[0]["raw"] == "아무 말"
    assert records[0]["verdict"].startswith("형식 오류")
    assert records[1]["verdict"] == "채택"
    assert records[0]["prompt_sha256"] == hashlib.sha256(sent[0].encode("utf-8")).hexdigest()
    assert records[1]["target_url"] == TARGET


def test_api_error_is_recorded_without_raw_text():
    records: list[dict] = []
    outputs = iter([CompletionError("연결 실패"), GOOD])

    def complete(prompt: str) -> str:
        item = next(outputs)
        if isinstance(item, Exception):
            raise item
        return item

    LLMBreaker(complete, LAB, TARGET, record=records.append).propose([])
    assert records[0]["raw"] is None
    assert records[0]["verdict"] == "API 오류: 연결 실패"
    assert records[1]["verdict"] == "채택"


def test_call_journal_appends_one_utf8_json_line_per_record(tmp_path):
    path = tmp_path / "nested" / "breaker-test.jsonl"
    journal = CallJournal(path)
    journal({"verdict": "채택", "raw": None})
    journal({"verdict": "형식 오류", "raw": "한글 원문"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["verdict"] for line in lines] == ["채택", "형식 오류"]
    assert "한글 원문" in lines[1]
