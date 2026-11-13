"""심판 ↔ 실험망 경계.

실제 구현(Docker 실험망의 주입·해제)은 실험망 담당이 Lab을 만족하도록
만든다. 심판은 아래 네 함수만 호출한다. 재현 실패는 무효 라운드로
기록한다.
"""

from __future__ import annotations

from typing import Protocol

from .contracts import FaultSpec, LabResult, ProbeResult, Scenario


class Lab(Protocol):
    """실험망이 따를 형태. 주입·확인·초기화 함수 네 개."""

    def health_check(self, target_url: str) -> LabResult:
        """클라이언트 컨테이너에서 대상 URL에 정상 접속되는지 확인한다."""

    def inject(self, fault: FaultSpec) -> LabResult:
        """장애 하나를 심는다. observations에 실제로 적용한 명령·설정을 남긴다."""

    def verify_symptom(self, scenario: Scenario) -> LabResult:
        """심은 뒤 클라이언트에서 증상(접속 실패, 지연 등)이 실제로 나타나는지 확인한다."""

    def reset(self) -> LabResult:
        """모든 규칙을 지우고 서버 설정을 원래대로 되돌린다."""


class ProbeRunner(Protocol):
    """검사 도구를 실행하는 쪽(실험망)이 따를 형태.

    심판의 도구 창구(Toolbox)가 인자를 검사한 뒤 부른다. 클라이언트
    컨테이너에서만 실행하고, 출력에 장애 정보를 넣지 않는다.
    """

    def run_probe(self, tool: str, args: dict) -> ProbeResult:
        """tools.py에 정의된 도구 하나를 실행하고 원본 출력을 돌려준다."""


class MockLab:
    """실험망이 준비되기 전 심판 개발용 모의 구현.

    장애를 기억만 하고 실제로 아무것도 바꾸지 않는다.
    fail_inject / no_symptom / broken_reset으로 실패 경로를 시험할 수 있다.
    """

    def __init__(
        self,
        fail_inject: set[str] | None = None,
        no_symptom: bool = False,
        broken_reset: bool = False,
    ):
        self.active: list[FaultSpec] = []
        self.fail_inject = fail_inject or set()
        self.no_symptom = no_symptom
        self.broken_reset = broken_reset
        self.calls: list[str] = []

    def health_check(self, target_url: str) -> LabResult:
        """남은 장애가 없으면 정상(HTTP 200)으로 응답한다."""
        self.calls.append("health_check")
        if self.active:
            return LabResult(
                False,
                f"장애 {len(self.active)}개가 남아 있음",
                {"active": [f.id for f in self.active]},
            )
        return LabResult(True, "HTTP 200 (mock)", {"http_code": 200})

    def inject(self, fault: FaultSpec) -> LabResult:
        """장애를 목록에 기록한다. fail_inject에 있으면 실패로 응답한다."""
        self.calls.append(f"inject:{fault.id}")
        if fault.id in self.fail_inject:
            return LabResult(False, f"{fault.id} 주입 실패 (mock)")
        self.active.append(fault)
        return LabResult(True, f"{fault.id} 주입 (mock)", {"params": fault.params})

    def verify_symptom(self, scenario: Scenario) -> LabResult:
        """장애가 있으면 접속 실패를 재현한 것으로 응답한다."""
        self.calls.append("verify_symptom")
        if self.no_symptom or not self.active:
            return LabResult(False, "증상 없음 (mock)")
        return LabResult(True, "접속 실패 재현 (mock)", {"http_code": 0})

    def reset(self) -> LabResult:
        """기록한 장애를 모두 지운다. broken_reset이면 지우지 않는다."""
        self.calls.append("reset")
        if not self.broken_reset:
            self.active.clear()
        return LabResult(not self.broken_reset, "초기화 (mock)")
