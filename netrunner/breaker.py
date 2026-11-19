"""Breaker → 심판 경계. Breaker 담당은 propose(history)를 구현한다.

history는 채점이 끝난 대결 라운드의 BreakerFeedback 목록이다.
고정 세트 라운드는 빠져 있다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .breaker_prompt import build_prompt, with_rejection
from .breaker_strategy import plan, refused_by_lab, signature
from .contracts import BreakerFeedback, Scenario
from .validate import MAX_FAULTS, LabInfo, validate


class Breaker(Protocol):
    """Breaker가 따를 형태. 시나리오 하나를 제안한다."""

    def propose(self, history: list[BreakerFeedback]) -> Scenario:
        """지난 라운드 피드백을 보고 다음 시나리오를 제안한다."""


class FixedBreaker:
    """정해 둔 시나리오를 순서대로 낸다.

    첫 통합 시연은 이것으로 돌리고, 이후 같은 자리에 실제 Breaker를
    연결한다.
    """

    def __init__(self, scenarios: list[Scenario]):
        self._queue = list(scenarios)

    def propose(self, history: list[BreakerFeedback]) -> Scenario:
        """남은 시나리오를 앞에서부터 하나 낸다."""
        if not self._queue:
            raise RuntimeError("남은 시나리오가 없음")
        return self._queue.pop(0)


class BreakerError(RuntimeError):
    """정해진 횟수 안에 유효한 제안을 만들지 못했다."""


class CompletionError(RuntimeError):
    """complete 함수가 호출에 실패했다(네트워크, API 오류, 응답 잘림 등)."""


@dataclass
class ProposalStats:
    """모델 호출 한 번마다 결과를 세는 기록. 제안 품질 보고에 쓴다."""

    calls: int = 0
    api_errors: int = 0  # 호출 자체가 실패함(CompletionError)
    unparsable: int = 0  # JSON이나 시나리오 형식이 아님
    duplicate: int = 0  # 이미 낸 장애 조합·매개변수와 같음
    invalid: int = 0  # 심판 검증 규칙에 걸림
    masked: int = 0  # 앞 단계 장애에 가려 클라이언트에서 볼 수 없음
    over_limit: int = 0  # 실험망이 동시에 켤 수 있는 장애 수를 넘음
    lab_refused: int = 0  # 실험망이 주입하지 않는 주소(호스트, 정상 서버 IP)를 씀
    accepted: int = 0

    @property
    def rejected_ratio(self) -> float:
        """호출 중 채택되지 않은 비율(0~1). 호출이 없으면 0."""
        return 0.0 if not self.calls else (self.calls - self.accepted) / self.calls


def parse_proposal(text: str) -> dict:
    """모델 출력에서 첫 JSON 객체를 꺼낸다. 없으면 ValueError.

    코드 펜스나 앞뒤 설명문은 무시한다. 설명문 속 중괄호(예: {D2})는
    JSON으로 읽히지 않으므로 건너뛴다.
    """
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("JSON 객체가 없음")


class LLMBreaker:
    """텍스트 모델을 호출해 시나리오를 제안하는 Breaker.

    complete는 프롬프트를 받아 응답 문자열을 돌려주는 함수다. 모델 공급자와
    호출 방식은 이 함수를 만드는 진입점이 정한다. 시나리오 ID와 대상 URL은
    이 클래스가 정하고, 모델은 장애·증상·메모만 낸다.

    max_faults는 실험망이 한 라운드에 켤 수 있는 동시 장애 수다. 진입점이
    실험망 구현에 맞춰 넘긴다.
    """

    def __init__(
        self,
        complete: Callable[[str], str],
        lab: LabInfo,
        target_url: str,
        max_attempts: int = 3,
        max_faults: int = MAX_FAULTS,
    ):
        self._complete = complete
        self._lab = lab
        self._target_url = target_url
        self._max_attempts = max_attempts
        self._max_faults = max_faults
        self._seen: set[tuple] = set()
        self.stats = ProposalStats()

    def propose(self, history: list[BreakerFeedback]) -> Scenario:
        """지난 라운드를 반영한 프롬프트로 제안을 받아 검사를 통과한 것을 낸다.

        채점이 끝난 유효 라운드의 장애 조합도 중복으로 본다(프롬프트의 "반복하지
        않는다"와 같은 기준). 거부된 라운드는 빼는데, 그래야 같은 장애를 증상만
        고쳐 다시 낼 수 있다. 거부 사유는 다음 시도의 프롬프트에 붙는다. 호출
        실패도 한 번의 시도로 세며 사유는 붙이지 않는다. max_attempts번 안에
        통과하지 못하면 BreakerError. 출제 힌트(plan)는 유효 라운드만으로 계산한다.
        """
        target = Scenario(scenario_id="", target_url=self._target_url, faults=[], complaint="")
        self._seen.update(signature(fb.faults, target.domain) for fb in history if fb.valid)
        hints = plan(history, self._lab, self._target_url, self._max_faults)
        prompt = build_prompt(
            history,
            self._target_url,
            self._lab,
            exhausted=hints.exhausted,
            max_faults=self._max_faults,
            hints=hints,
        )
        reason = ""
        last_error = ""
        for _ in range(self._max_attempts):
            try:
                text = self._complete(with_rejection(prompt, reason) if reason else prompt)
            except CompletionError as e:
                self.stats.calls += 1
                self.stats.api_errors += 1
                reason = ""
                last_error = str(e)
                continue
            self.stats.calls += 1
            sc, reason = self._check(text)
            if sc is not None:
                self.stats.accepted += 1
                return sc
        detail = reason or f"API 오류: {last_error}"
        raise BreakerError(f"{self._max_attempts}회 시도 안에 유효한 제안 없음: {detail}")

    def _check(self, text: str) -> tuple[Scenario | None, str]:
        """출력 하나를 시나리오로 바꿔 검사한다. (시나리오, '') 또는 (None, 사유)."""
        try:
            data = parse_proposal(text)
            faults = data.get("faults", [])
            complaint = data.get("complaint", "")
            # 모델 출력은 신뢰하지 않는다. 타입이 틀리면 검증 전에 형식 오류로 센다.
            if not isinstance(faults, list) or not isinstance(complaint, str):
                raise ValueError("faults는 목록, complaint는 문자열이어야 함")
            sc = Scenario.from_dict(
                {
                    "scenario_id": f"BR-{self.stats.accepted + 1:03d}",
                    "target_url": self._target_url,
                    "faults": faults,
                    "complaint": complaint,
                    "note": str(data.get("note", "")),
                    "origin": "breaker",
                }
            )
            if not all(isinstance(f.id, str) and isinstance(f.params, dict) for f in sc.faults):
                raise ValueError("장애 ID는 문자열, params는 객체여야 함")
            key = signature(sc.faults, sc.domain)
        except (ValueError, TypeError) as e:
            self.stats.unparsable += 1
            return None, f"형식 오류: {e}"
        if key in self._seen:
            self.stats.duplicate += 1
            return None, "이미 낸 장애 조합과 매개변수와 같음"
        v = validate(sc, self._lab)
        if not v.ok:
            self.stats.invalid += 1
            return None, "; ".join(v.errors)
        if any(refused_by_lab(f, self._lab) for f in sc.faults):
            self.stats.lab_refused += 1
            return None, "실험망이 주입하지 않는 주소(실험망 호스트 또는 정상 서버 IP)를 씀"
        if len(sc.faults) > self._max_faults:
            self.stats.over_limit += 1
            return None, f"동시 장애 {len(sc.faults)}개, 실험망 한도 {self._max_faults}개"
        hidden = [f.id for f, seen in zip(sc.faults, v.observable, strict=True) if not seen]
        if hidden:
            self.stats.masked += 1
            return None, f"앞 단계 장애에 가려 관측할 수 없음: {', '.join(hidden)}"
        return sc, ""
