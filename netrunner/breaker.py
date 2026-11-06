"""Breaker → 심판 경계. Breaker 담당은 propose(history)를 구현한다.

history는 채점이 끝난 대결 라운드의 BreakerFeedback 목록이다(고정 세트 라운드는 빠져 있다).
"""
from __future__ import annotations

from typing import Protocol

from .contracts import BreakerFeedback, Scenario


class Breaker(Protocol):
    def propose(self, history: list[BreakerFeedback]) -> Scenario: ...


class FixedBreaker:
    """정해 둔 시나리오를 순서대로 낸다. 첫 통합 시연은 이것으로 돌리고, 이후 같은 자리에 실제 Breaker를 연결한다."""

    def __init__(self, scenarios: list[Scenario]):
        self._queue = list(scenarios)

    def propose(self, history: list[BreakerFeedback]) -> Scenario:
        if not self._queue:
            raise RuntimeError("남은 시나리오가 없음")
        return self._queue.pop(0)
