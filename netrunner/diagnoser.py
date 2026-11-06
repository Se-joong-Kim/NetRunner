"""심판 → Diagnoser 경계. Diagnoser 담당은 diagnose(brief)를 구현하고 Diagnosis 형식으로 돌려준다.

brief에는 라운드 ID, 대상 URL, 사용자 증상, 검사 한도만 있다. 정답 정보는 넘기지 않는다.
"""
from __future__ import annotations

import copy
from typing import Protocol

from .contracts import Diagnosis, RoundBrief


class Diagnoser(Protocol):
    # 라운드 기록의 모델·방식 이름은 여기서만 가져온다. 진단 실패 라운드도 이 모델의 실패로 집계된다.
    model: str  # 예: "solar-pro2", "glm-4.5"
    method: str  # 설계안 9.2절: "A" | "B" | "C" | "C-1" | "C-2"

    def diagnose(self, brief: RoundBrief) -> Diagnosis: ...


class ReplayDiagnoser:
    """저장된 진단 결과를 순서대로 돌려준다. 실제 Diagnoser가 붙기 전 심판 개발·시연용."""

    def __init__(self, diagnoses: list[dict]):
        self._queue = list(diagnoses)
        first = diagnoses[0] if diagnoses else {}
        self.model = first.get("model", "-")
        self.method = first.get("method", "-")

    def diagnose(self, brief: RoundBrief) -> Diagnosis:
        if not self._queue:
            raise RuntimeError("재생할 진단 결과가 더 없음")
        d = copy.deepcopy(self._queue.pop(0))
        d["round_id"] = brief.round_id
        return Diagnosis.from_dict(d)
