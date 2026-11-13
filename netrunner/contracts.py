"""네 모듈 사이에 오가는 데이터 형식(역할 분담안 '모듈 사이에 전달할 데이터' 표).

모두 JSON으로 주고받는다. 각 클래스의 from_dict가 필수 키와 타입을 검사하므로,
다른 담당자의 모듈 출력은 이 함수를 통과하면 형식 합의를 지킨 것이다.
예시는 docs/interfaces.md와 fixtures/를 본다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

from .faults import label_of


class ContractError(ValueError):
    """합의된 형식을 어긴 입력."""


def _require(d: dict, *keys: str, where: str) -> None:
    if not isinstance(d, dict):
        raise ContractError(f"{where}: JSON 객체가 아님")
    missing = [k for k in keys if k not in d]
    if missing:
        raise ContractError(f"{where}: 필수 키 없음 {missing}")


def _typed(d: dict, key: str, types, where: str, default=None):
    v = d.get(key, default)
    # bool은 int의 하위형이라 따로 막는다.
    if isinstance(v, bool) or not isinstance(v, types):
        raise ContractError(f"{where}.{key}: 타입이 맞지 않음 ({v!r})")
    return v


def _number(d: dict, key: str, cast, where: str, default):
    """숫자 필드를 바꾼다. 바꿀 수 없으면 ContractError(ValueError가 새지 않게)."""
    v = d.get(key, default)
    if isinstance(v, bool):
        raise ContractError(f"{where}.{key}: 숫자가 아님 ({v!r})")
    try:
        return cast(v)
    except (TypeError, ValueError) as e:
        raise ContractError(f"{where}.{key}: 숫자가 아님 ({v!r})") from e


def read_json(path: str | Path) -> Any:
    """UTF-8 JSON 파일을 읽어 파이썬 객체로 돌려준다."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- Breaker → 심판


@dataclass
class FaultSpec:
    """Breaker가 제안한 장애 하나: ID와 매개변수."""

    id: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> FaultSpec:
        """dict에서 FaultSpec을 만든다. 형식이 틀리면 ContractError."""
        _require(d, "id", where="fault")
        return cls(id=d["id"], params=dict(d.get("params") or {}))


@dataclass
class Scenario:
    """시나리오 하나: 대상 URL, 장애 목록, 증상 설명."""

    scenario_id: str
    target_url: str
    faults: list[FaultSpec]
    complaint: str
    origin: str = "fixed"  # "fixed"(고정 세트) | "breaker"(대결 세트)
    note: str = ""  # 사람용 메모. Diagnoser에 절대 넘기지 않는다.

    @classmethod
    def from_dict(cls, d: dict) -> Scenario:
        """dict에서 Scenario를 만든다. 형식이 틀리면 ContractError."""
        _require(d, "scenario_id", "target_url", "faults", "complaint", where="scenario")
        return cls(
            scenario_id=d["scenario_id"],
            target_url=d["target_url"],
            faults=[FaultSpec.from_dict(f) for f in d["faults"]],
            complaint=d["complaint"],
            origin=d.get("origin", "fixed"),
            note=d.get("note", ""),
        )

    @property
    def url(self) -> SplitResult:
        """대상 URL을 구성 요소로 나눈 결과."""
        return urlsplit(self.target_url)

    @property
    def domain(self) -> str:
        """대상 URL의 호스트 이름."""
        return self.url.hostname or ""

    @property
    def port(self) -> int:
        """URL 형식 오류면 ValueError. validate()가 먼저 검사한다."""
        u = self.url
        return u.port or (443 if u.scheme == "https" else 80)


# --- 심판 → Diagnoser


@dataclass
class Limits:
    """Diagnoser가 지켜야 할 검사 횟수, LLM 호출, 시간 한도."""

    max_tool_calls: int = 20
    max_seconds: int = 300
    max_llm_calls: int = 15


@dataclass
class RoundBrief:
    """Diagnoser가 받는 전부. 시나리오 ID·장애·주입 로그·정답은 들어가지 않는다."""

    round_id: str
    target_url: str
    complaint: str
    limits: Limits = field(default_factory=Limits)


# --- Diagnoser → 심판

# 근거 참조 형식: "probe:<probe_id>" 또는 "pcap:<probe_id>#<n>[-<m>]"
EVIDENCE_REF = re.compile(
    r"^(?:probe:(?P<probe>[\w.-]+)|pcap:(?P<cap>[\w.-]+)#(?P<a>\d+)(?:-(?P<b>\d+))?)$"
)


@dataclass
class Probe:
    """Diagnoser가 실행한 검사 한 건.

    원본 출력은 클라이언트 컨테이너에서 실행한 결과 그대로다.
    """

    probe_id: str
    # dig | curl | openssl | ping | tracepath | tcpdump | load ...
    tool: str
    args: dict[str, Any]
    output: str
    elapsed_ms: int = 0
    # 캡처 검사일 때 패킷 수. 인용된 패킷 번호의 범위 검사에 쓴다.
    packets: int | None = None
    file: str | None = None  # 캡처 파일 경로

    @classmethod
    def from_dict(cls, d: dict) -> Probe:
        """dict에서 Probe를 만든다. 형식이 틀리면 ContractError."""
        _require(d, "probe_id", "tool", "output", where="probe")
        return cls(
            probe_id=str(d["probe_id"]),
            tool=_typed(d, "tool", str, "probe"),
            args=dict(d.get("args") or {}),
            output=_typed(d, "output", str, "probe"),
            elapsed_ms=_typed(d, "elapsed_ms", (int, float), "probe", 0),
            packets=_typed(d, "packets", (int, type(None)), "probe"),
            file=_typed(d, "file", (str, type(None)), "probe"),
        )


@dataclass
class ProbeResult:
    """실험망이 검사 한 건을 실행한 결과(실험망 → 도구 창구).

    ok는 실험망이 명령을 실행했는지다. 연결 거부처럼 네트워크가 실패한
    결과도 ok=True이고 그 실패는 output에 담긴다. error에는 장애 정보를
    넣지 않는다.
    """

    ok: bool
    output: str = ""
    elapsed_ms: int = 0
    packets: int | None = None
    file: str | None = None
    error: str = ""


@dataclass
class ProbeView:
    """도구 창구가 Diagnoser에게 돌려주는 검사 결과.

    probe_id는 실행된 검사에만 붙고, 거부된 호출은 None과 error를 갖는다.
    """

    probe_id: str | None
    output: str = ""
    packets: int | None = None
    file: str | None = None
    error: str = ""


@dataclass
class Evidence:
    """진단이 든 근거 하나: 검사 참조와 그 검사가 보여 준다는 주장."""

    ref: str
    claim: str = ""

    @classmethod
    def from_dict(cls, d: dict | str) -> Evidence:
        """문자열(참조만) 또는 dict에서 Evidence를 만든다."""
        if isinstance(d, str):
            return cls(ref=d)
        _require(d, "ref", where="evidence")
        return cls(
            ref=_typed(d, "ref", str, "evidence"), claim=_typed(d, "claim", str, "evidence", "")
        )


@dataclass
class Finding:
    """진단 한 건: 단계, 원인, 근거 목록."""

    stage: str
    cause: str
    evidence: list[Evidence] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> Finding:
        """dict에서 Finding을 만든다. 형식이 틀리면 ContractError."""
        _require(d, "stage", "cause", where="finding")
        return cls(
            stage=_typed(d, "stage", str, "finding"),
            cause=_typed(d, "cause", str, "finding"),
            evidence=[Evidence.from_dict(e) for e in _typed(d, "evidence", list, "finding", [])],
        )

    @property
    def label(self) -> str:
        """'단계 / 원인' 표기. 정답 라벨과 같은 형식(faults.label_of)이다."""
        return label_of(self.stage, self.cause)


@dataclass
class Diagnosis:
    """Diagnoser의 출력 한 건: 진단 목록, 검사 기록, 사용량."""

    round_id: str
    # 예: "solar-pro2", "glm-4.5". 교체 전후 숫자가 섞이지 않게 남긴다.
    model: str
    # 설계안 9.2절: "A" 규칙 | "B" 도구 없는 LLM | "C" 전체 | "C-1" | "C-2"
    method: str
    findings: list[Finding]
    trace: list[Probe] = field(default_factory=list)
    llm_calls: int = 0
    tokens: int = 0
    elapsed_s: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> Diagnosis:
        """dict에서 Diagnosis를 만든다. 형식이 틀리면 ContractError."""
        _require(d, "round_id", "model", "method", "findings", where="diagnosis")
        return cls(
            round_id=d["round_id"],
            model=_typed(d, "model", str, "diagnosis"),
            method=_typed(d, "method", str, "diagnosis"),
            findings=[Finding.from_dict(f) for f in _typed(d, "findings", list, "diagnosis")],
            trace=[Probe.from_dict(p) for p in _typed(d, "trace", list, "diagnosis", [])],
            llm_calls=_number(d, "llm_calls", int, "diagnosis", 0),
            tokens=_number(d, "tokens", int, "diagnosis", 0),
            elapsed_s=_number(d, "elapsed_s", float, "diagnosis", 0.0),
        )


# --- 심판 ↔ 실험망


@dataclass
class LabResult:
    """실험망 조작 함수의 공통 반환값.

    observations에는 측정값과 캡처 파일 경로를 넣는다.
    """

    ok: bool
    detail: str = ""
    observations: dict[str, Any] = field(default_factory=dict)


# --- 심판 → Breaker


@dataclass
class FaultOutcome:
    """장애 하나의 채점 결과 한 줄(Breaker에게 돌려주는 형식)."""

    id: str
    label: str
    # correct | cause_wrong | stage_wrong | missed | masked
    verdict: str


@dataclass
class BreakerFeedback:
    """채점이 끝난 라운드만 보낸다.

    고정 세트(최종 평가용) 라운드는 보내지 않는다.
    """

    scenario_id: str
    faults: list[FaultSpec]
    complaint: str
    valid: bool
    invalid_reason: str = ""
    outcomes: list[FaultOutcome] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)  # "단계 / 원인"
