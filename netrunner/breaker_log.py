"""Breaker 모델 호출의 원문과 판정을 JSON Lines 파일에 덧붙여 남긴다.

라운드 기록(심판 계약)과 섞지 않는 별도 파일이다. 기록 한 건은 호출 한 번의
프롬프트 해시, 원문 응답, 판정이다. 파일 위치는 진입점이 정한다.
"""

from __future__ import annotations

import json
from pathlib import Path


class CallJournal:
    """기록 한 건을 한 줄 JSON으로 파일 끝에 덧붙인다."""

    def __init__(self, path: str | Path):
        self._path = Path(path)

    def __call__(self, record: dict) -> None:
        """기록 한 건을 덧붙인다. 상위 폴더가 없으면 만든다."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
