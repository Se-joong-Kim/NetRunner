"""심판이 라운드마다 만들어 Diagnoser에게 넘기는 검사 도구 창구.

Diagnoser의 모든 검사는 이 창구를 거친다. 창구는 도구 목록(tools.py)으로
이름·인자를 검사하고, 방식별 허용 도구와 검사 한도를 지키고, 실험망이
실제로 낸 출력을 기록한다. 채점은 이 기록으로만 한다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from .contracts import Limits, Probe, ProbeView
from .lab import ProbeRunner
from .tools import TOOLS, check_args, specs
from .validate import LabInfo

MAX_OUTPUT_CHARS = 20_000
# 거부된 호출을 기록할 때 남기는 인자 표현의 최대 길이
MAX_REJECTED_CHARS = 2_000


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
        # 채점 근거. Diagnoser가 고칠 수 없게 숨기고, 심판은 probes()로 사본을 읽는다.
        self._record: list[Probe] = []
        self._rejected: list[RejectedCall] = []

    def specs(self) -> list[dict]:
        """이 라운드에 쓸 수 있는 도구의 LLM용 설명."""
        return specs(self._usable)

    def probes(self) -> list[Probe]:
        """실행된 검사 기록의 사본(심판용). 채점은 이것으로만 한다."""
        return [replace(p, args=dict(p.args)) for p in self._record]

    def rejected_calls(self) -> list[RejectedCall]:
        """거부된 호출 기록의 사본(심판용)."""
        return [replace(c) for c in self._rejected]

    def remaining(self) -> int:
        """남은 검사 횟수."""
        return max(0, self._limits.max_tool_calls - len(self._record))

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
            probe_id=f"p{len(self._record) + 1}",
            tool=name,
            args=dict(args),
            output=output,
            elapsed_ms=result.elapsed_ms,
            packets=result.packets,
            file=result.file,
        )
        self._record.append(probe)
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
        # 인자는 Diagnoser가 낸 그대로라 JSON이 아닐 수 있고, 호출 뒤에 바뀔 수도 있다.
        self._rejected.append(RejectedCall(_snapshot(name), _snapshot(args), reason))
        return ProbeView(None, error=reason)


def _truncate(output: str) -> str:
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    return output[:MAX_OUTPUT_CHARS] + f"\n…(출력 잘림: 원래 {len(output)}자)"


def _snapshot(value: object) -> object:
    """기록용 사본. JSON으로 쓸 수 있는 값이면 그 사본, 아니면 잘린 repr."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        text = None
    if text is not None and len(text) <= MAX_REJECTED_CHARS:
        return json.loads(text)
    return repr(value)[:MAX_REJECTED_CHARS]
