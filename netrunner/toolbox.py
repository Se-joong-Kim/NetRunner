"""심판이 라운드마다 만들어 Diagnoser에게 넘기는 검사 도구 창구.

Diagnoser의 모든 검사는 이 창구를 거친다. 창구는 도구 목록(tools.py)으로
이름·인자를 검사하고, 방식별 허용 도구와 검사 한도를 지키고, 실험망이
실제로 낸 출력을 기록한다. 채점은 이 기록으로만 한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .contracts import Limits, Probe, ProbeView
from .lab import ProbeRunner
from .tools import TOOLS, check_args, specs
from .validate import LabInfo

MAX_OUTPUT_CHARS = 20_000


@dataclass
class RejectedCall:
    """창구가 실행하지 않고 돌려보낸 호출 하나."""

    tool: object
    args: object
    reason: str


class Toolbox:
    """검사 도구 창구. 검사·한도·기록을 맡는다."""

    def __init__(
        self,
        runner: ProbeRunner | None,
        lab: LabInfo,
        limits: Limits,
        allowed: frozenset[str],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._runner = runner
        self._lab = lab
        self._limits = limits
        self._usable = frozenset(allowed) & set(lab.enabled_tools) & set(TOOLS)
        self._clock = clock
        self._started = clock()
        self._closed = False
        self.record: list[Probe] = []
        self.rejected: list[RejectedCall] = []

    def specs(self) -> list[dict]:
        """이 라운드에 쓸 수 있는 도구의 LLM용 설명."""
        return specs(self._usable)

    def remaining(self) -> int:
        """남은 검사 횟수."""
        return max(0, self._limits.max_tool_calls - len(self.record))

    def close(self) -> None:
        """라운드가 끝나면 심판이 부른다. 이후 호출은 모두 거부한다."""
        self._closed = True

    def call(self, name: str, args: dict) -> ProbeView:
        """검사 한 번. 거부되면 probe_id 없이 이유를 돌려준다."""
        reason = self._refuse(name, args)
        if reason:
            return self._reject(name, args, reason)
        try:
            result = self._runner.run_probe(name, dict(args))
        except Exception as e:  # 실험망 예외가 진단 전체를 깨뜨리지 않게 한다
            return self._reject(name, args, f"실험망 오류: {type(e).__name__}")
        if not result.ok:
            return self._reject(name, args, result.error or "실험망이 실행하지 못함")
        output = _truncate(result.output)
        probe = Probe(
            probe_id=f"p{len(self.record) + 1}",
            tool=name,
            args=dict(args),
            output=output,
            elapsed_ms=result.elapsed_ms,
            packets=result.packets,
            file=result.file,
        )
        self.record.append(probe)
        return ProbeView(probe.probe_id, output, result.packets, result.file)

    def _refuse(self, name: object, args: object) -> str:
        if self._closed:
            return "라운드가 끝나 검사할 수 없음"
        if not isinstance(name, str) or name not in self._usable:
            return f"이 라운드에서 쓸 수 없는 도구: {name!r}"
        err = check_args(name, args, self._lab)
        if err:
            return err
        if not self.remaining():
            return f"검사 한도 {self._limits.max_tool_calls}회를 넘음"
        if self._clock() - self._started >= self._limits.max_seconds:
            return f"시간 한도 {self._limits.max_seconds}초를 넘음"
        if self._runner is None:
            return "실험망이 검사를 지원하지 않음"
        return ""

    def _reject(self, name: object, args: object, reason: str) -> ProbeView:
        self.rejected.append(RejectedCall(name, args, reason))
        return ProbeView(None, error=reason)


def _truncate(output: str) -> str:
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    return output[:MAX_OUTPUT_CHARS] + f"\n…(출력 잘림: 원래 {len(output)}자)"
