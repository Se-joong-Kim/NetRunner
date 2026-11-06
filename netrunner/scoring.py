"""채점(설계안 9.3절 지표).

정답과 진단을 대조하고, 인용된 근거가 실제로 있고 결론을 지지하는지
따로 본다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .contracts import EVIDENCE_REF, Diagnosis, Finding, Limits, Probe, Scenario
from .faults import FAULTS, Fault, cause_matches, lookup_fault, normalize_stage

# 짝짓기 종류 → 판정. 관측 불가 장애는 짝과 무관하게 masked.
VERDICT_BY_KIND = {"full": "correct", "stage": "cause_wrong", "any": "stage_wrong", None: "missed"}


@dataclass
class FaultScore:
    """장애 하나의 판정 결과."""

    id: str
    label: str
    # correct | cause_wrong | stage_wrong | missed | masked
    verdict: str
    finding: int | None = None  # 짝지어진 finding의 번호


@dataclass
class EvidenceCheck:
    """인용 근거 하나의 존재·지지 판정."""

    finding: int
    ref: str
    claim: str
    probe_id: str  # 참조가 가리키는 검사. 형식 오류면 ""
    exists: bool
    supported: bool | None  # None: 자동 신호로 판정 못 함 → 사람이 확인
    reason: str


@dataclass
class Score:
    """라운드 하나의 채점 결과 전체."""

    faults: list[FaultScore]
    false_positives: list[str]
    evidence: list[EvidenceCheck]
    stage_correct: int
    cause_correct: int
    scored_faults: int  # 관측 가능한 장애 수(분모)
    evidence_valid: int
    evidence_unverified: int
    evidence_total: int
    tool_calls: int
    llm_calls: int
    tokens: int
    elapsed_s: float  # 심판이 잰 진단 시간
    limit_violations: list[str] = field(default_factory=list)


def _signal_patterns(fault: Fault, params: dict) -> list[tuple[str, float | None]]:
    """장애의 신호를 (정규식, 최소값) 목록으로 바꾼다.

    최소값은 fault.value_param에 해당하는 매개변수 값이다. 매개변수가
    없으면 None이고 수치 조건은 없다. value_param을 쓰는 장애의 신호는
    반드시 (?P<v>...) 그룹을 가져야 한다(없으면 _signal_hit가 실패한다).
    """

    def fill(m: re.Match) -> str:
        key = m.group(1)
        return re.escape(str(params[key])) if key in params else m.group(0)

    floor = float(params[fault.value_param]) if fault.value_param in params else None
    return [(re.sub(r"\{(\w+)\}", fill, s), floor) for s in fault.signals]


def _signal_hit(pattern: str, floor: float | None, text: str) -> bool:
    """신호가 출력에 있는지 본다.

    최소값이 있으면 정규식의 (?P<v>) 값이 그 이상인 일치만 인정한다.
    판정은 정규식 일치와 수치 비교뿐이다. 의미 판단은 하지 않으므로,
    자동 지지는 "신호가 보인다"까지만 보장한다는 점을 기억한다.
    """
    return any(floor is None or float(m.group("v")) >= floor for m in re.finditer(pattern, text))


def _match(
    findings: list[Finding], faults: list[Fault], observable: list[bool]
) -> dict[int, tuple[int, str]]:
    """장애마다 finding 하나를 짝짓는다.

    순서는 완전 일치 → 단계만 일치 → (관측 가능한 장애에 한해) 아무
    남은 진단이다. 마지막 단계의 짝은 단계를 잘못 짚은 진단이다.
    반환: {장애 번호: (finding 번호, "full"|"stage"|"any")}
    """
    pairs: dict[int, tuple[int, str]] = {}
    used: set[int] = set()
    for kind in ("full", "stage", "any"):
        for fi, fault in enumerate(faults):
            # 관측 불가 장애는 어떤 단계에서도 짝을 짓지 않는다(그 진단은 오탐이 된다).
            if fi in pairs or not observable[fi]:
                continue
            for ni, n in enumerate(findings):
                if ni in used:
                    continue
                if kind != "any" and normalize_stage(n.stage) != fault.stage:
                    continue
                if kind == "full" and not cause_matches(fault, n.cause):
                    continue
                pairs[fi] = (ni, kind)
                used.add(ni)
                break
    return pairs


def _judge_ref(
    ref: str, probes: dict[str, Probe], patterns: list[tuple[str, float | None]] | None
) -> tuple[str, bool, bool | None, str]:
    """인용 하나 → (probe_id, 존재, 지지, 이유)."""
    m = EVIDENCE_REF.match(ref.strip())
    if not m:
        return "", False, False, "참조 형식 오류"
    pid = m.group("probe") or m.group("cap")
    probe = probes.get(pid)
    if probe is None:
        return pid, False, False, f"실행 기록에 없는 검사 {pid}"
    if m.group("cap"):
        a = int(m.group("a"))
        b = int(m.group("b") or a)
        if probe.packets is None:
            return pid, False, False, f"{pid}는 캡처가 아님"
        if not 1 <= a <= b <= probe.packets:
            return pid, False, False, f"패킷 번호 범위 밖 (1~{probe.packets})"
    if patterns is None:
        return pid, True, None, "목록에 없는 원인 — 수동 확인"
    hit = next((p for p, floor in patterns if _signal_hit(p, floor, probe.output)), None)
    return (
        (pid, True, True, f"신호 일치: {hit}")
        if hit
        else (pid, True, None, "자동 판정 불가 — 수동 확인")
    )


def _check_evidence(
    ni: int, finding: Finding, probes: dict[str, Probe], patterns: list[str] | None
) -> list[EvidenceCheck]:
    if not finding.evidence:
        return [EvidenceCheck(ni, "", "", "", False, False, "근거 없음")]
    return [
        EvidenceCheck(ni, ev.ref, ev.claim, *_judge_ref(ev.ref, probes, patterns))
        for ev in finding.evidence
    ]


def score(
    scenario: Scenario,
    observable: list[bool],
    diagnosis: Diagnosis,
    elapsed_s: float,
    limits: Limits | None = None,
) -> Score:
    """정답과 진단을 대조해 라운드 하나를 채점한다.

    elapsed_s는 심판이 잰 진단 시간이다. Diagnoser의 자체 보고값
    (diagnosis.elapsed_s)은 쓰지 않는다.
    """
    faults = [FAULTS[s.id] for s in scenario.faults]
    pairs = _match(diagnosis.findings, faults, observable)

    results = []
    for fi, (fault, seen) in enumerate(zip(faults, observable, strict=True)):
        ni, kind = pairs.get(fi, (None, None))
        results.append(
            FaultScore(fault.id, fault.label, VERDICT_BY_KIND[kind] if seen else "masked", ni)
        )

    # 완전·단계 일치로 짝지어지지 않은 진단은 오탐이다(단계를 잘못 짚은 진단 포함).
    explained = {ni for ni, kind in pairs.values() if kind != "any"}
    false_positives = [n.label for ni, n in enumerate(diagnosis.findings) if ni not in explained]

    # 근거는 실제 정답이 아니라 그 진단이 주장한 원인의 신호를 보이는지로 판정한다.
    probes = {p.probe_id: p for p in diagnosis.trace}
    injected = {s.id: s.params for s in scenario.faults}
    checks: list[EvidenceCheck] = []
    for ni, n in enumerate(diagnosis.findings):
        claimed = lookup_fault(n.stage, n.cause)
        patterns = _signal_patterns(claimed, injected.get(claimed.id, {})) if claimed else None
        checks += _check_evidence(ni, n, probes, patterns)

    violations = []
    if limits:
        if len(diagnosis.trace) > limits.max_tool_calls:
            violations.append(f"검사 {len(diagnosis.trace)}회 > 한도 {limits.max_tool_calls}")
        if diagnosis.llm_calls > limits.max_llm_calls:
            violations.append(f"LLM 호출 {diagnosis.llm_calls}회 > 한도 {limits.max_llm_calls}")
        if elapsed_s > limits.max_seconds:
            violations.append(f"진단 시간 {elapsed_s:.0f}s > 한도 {limits.max_seconds}s")

    scored = [r for r in results if r.verdict != "masked"]
    return Score(
        faults=results,
        false_positives=false_positives,
        evidence=checks,
        stage_correct=sum(r.verdict in ("correct", "cause_wrong") for r in scored),
        cause_correct=sum(r.verdict == "correct" for r in scored),
        scored_faults=len(scored),
        evidence_valid=sum(c.supported is True for c in checks),
        evidence_unverified=sum(c.exists and c.supported is None for c in checks),
        evidence_total=len(checks),
        tool_calls=len(diagnosis.trace),
        llm_calls=diagnosis.llm_calls,
        tokens=diagnosis.tokens,
        elapsed_s=round(elapsed_s, 3),
        limit_violations=violations,
    )
