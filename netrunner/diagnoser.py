"""심판 → Diagnoser 경계.

Diagnoser 담당은 diagnose(brief)를 구현하고 Diagnosis 형식으로
돌려준다. brief에는 라운드 ID, 대상 URL, 사용자 증상, 검사 한도만
있다. 정답 정보는 넘기지 않는다.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Protocol

from .contracts import Diagnosis, Evidence, Finding, Probe, RoundBrief


class Diagnoser(Protocol):
    """Diagnoser가 따를 형태. 증상만 보고 원인을 진단."""

    # 라운드 기록의 모델·방식 이름은 여기서만 가져온다.
    # 진단 실패 라운드도 이 모델의 실패로 집계된다.
    model: str  # 예: "solar-pro2", "glm-4.5"
    method: str  # 설계안 9.2절: "A" | "B" | "C" | "C-1" | "C-2"

    def diagnose(self, brief: RoundBrief) -> Diagnosis:
        """brief만 보고 진단 결과를 돌려준다."""


class HttpDiagnoser:
    """HTTP 검사 결과를 근거로 원인을 판별하는 규칙 기반 진단기."""

    model = "rules-http"
    method = "A"

    def __init__(self, probe: Callable[..., dict]):
        self._probe = probe

    def diagnose(self, brief: RoundBrief) -> Diagnosis:
        """한도 내에서 HTTP를 검사하고 확인된 원인만 반환."""
        result = Diagnosis(brief.round_id, self.model, self.method, [])
        if brief.limits.max_tool_calls <= 0 or brief.limits.max_seconds <= 0:
            return result

        observation = self._probe(brief.target_url, timeout=brief.limits.max_seconds)
        probe = Probe(
            "http-1",
            "python-http",
            {"url": brief.target_url, "location": observation["location"]},
            observation["raw_http"],
            observation["elapsed_ms"],
        )
        result.trace.append(probe)
        if observation["http_code"] == 502:
            cause, claim = "서버 오류", "HTTP 502 응답 관측"
        elif observation.get("redirect_loop"):
            cause, claim = "리다이렉트 순환", "리다이렉트 대상이 검사 경로에 다시 등장"
        else:
            return result
        result.findings.append(
            Finding("HTTP", cause, [Evidence(f"probe:{probe.probe_id}", claim)])
        )
        return result


class ReplayDiagnoser:
    """저장된 진단 결과를 순서대로 돌려준다.

    실제 Diagnoser가 붙기 전 심판 개발·시연용.
    """

    def __init__(self, diagnoses: list[dict]):
        self._queue = list(diagnoses)
        first = diagnoses[0] if diagnoses else {}
        self.model = first.get("model", "-")
        self.method = first.get("method", "-")

    def diagnose(self, brief: RoundBrief) -> Diagnosis:
        """저장된 진단 하나를 꺼내 이번 라운드의 round_id를 붙인다."""
        if not self._queue:
            raise RuntimeError("재생할 진단 결과가 더 없음")
        d = copy.deepcopy(self._queue.pop(0))
        d["round_id"] = brief.round_id
        return Diagnosis.from_dict(d)
