"""지난 유효 라운드로 다음 출제 방향(힌트)을 계산한다.

파일 입출력과 모델 호출이 없는 순수 계산이다. 출제 단위는 장애 ID이고,
IP 매개변수가 있는 장애는 주소 부류를 덧붙인다(ip=empty는 빈 주소).
실험망은 호스트 주소를 D2의 가짜 응답으로 주입하지 않는다(docker_lab.inject,
1단계에서 확인). 그래서 호스트 부류는 출제 단위에 넣지 않고, 제안도
refused_by_lab으로 거부한다. 중복 서명과 고갈 판정도 여기서 정해 Breaker와
프롬프트가 같은 규칙을 쓴다.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .contracts import BreakerFeedback, FaultSpec, Scenario
from .faults import FAULTS, Fault, Param, lookup_fault
from .validate import MAX_FAULTS, LabInfo

# 진단이 틀린 판정. masked는 가려져서 판단한 적이 없으므로 시도로도 세지 않는다.
WRONG_VERDICTS = frozenset({"cause_wrong", "stage_wrong", "missed"})
NOT_ATTEMPTED = frozenset({"masked"})
# 힌트에 넣을 오탐 라벨 수(많이 나온 순).
OVER_PREDICTED_TOP = 3
EMPTY_IP = "ip=empty"
HOST_IP = "ip=host"


@dataclass(frozen=True)
class Hints:
    """프롬프트에 넣을 출제 힌트. 근거일 뿐이고 최종 선택은 모델이 한다."""

    ranked: tuple[tuple[str, float], ...]  # (출제 단위, 평활 오답률), 높은 순
    untried: tuple[str, ...]  # 유효 라운드에 한 번도 나오지 않은 출제 단위
    exhausted: tuple[str, ...]  # 같은 조합이 이미 나와 더 낼 수 없는 장애 ID
    over_predicted: tuple[str, ...]  # 진단이 자주 잘못 고른 정답 라벨


def is_fixed(name: str, p: Param) -> bool:
    """매개변수 값이 대상 URL에서 정해지는지 본다.

    도메인은 대상 도메인, 포트는 대상 포트로 고정된다. cert_domain처럼 다른
    도메인을 고를 수 있는 값은 고정이 아니다. 장애 ID로 가리지 않고 Param.kind와
    이름만 본다.
    """
    return p.kind == "port" or (p.kind == "domain" and name == "domain")


def signature(faults: Sequence[FaultSpec], domain: str) -> tuple:
    """중복 판정 키: 장애 ID와 실제 적용되는 매개변수 값.

    생략된 선택 매개변수는 None, 생략된 domain은 대상 도메인으로 본다.
    증상 문장과 메모는 보지 않는다.
    """
    items = []
    for f in faults:
        names = FAULTS[f.id].params if f.id in FAULTS else f.params
        values = {n: f.params.get(n, domain if n == "domain" else None) for n in sorted(names)}
        items.append((f.id, json.dumps(values, sort_keys=True)))
    return tuple(sorted(items))


def _ip_names(spec: Fault) -> list[str]:
    """IP 매개변수 이름 목록."""
    return [n for n, p in spec.params.items() if p.kind == "ip"]


def lab_refused_addresses(lab: LabInfo) -> frozenset[str]:
    """실험망이 주입하지 않는 주소: 실험망 호스트와 정상 서버 주소.

    docker_lab.inject가 같은 목록으로 D2 주입을 거부한다.
    """
    return frozenset(lab.hosts.values()) | {lab.web_ip}


def refused_values(fault: FaultSpec, lab: LabInfo) -> list[str]:
    """장애의 IP 매개변수 중 실험망이 주입하지 않는 값 목록."""
    spec = FAULTS.get(fault.id)
    if spec is None:
        return []
    refused = lab_refused_addresses(lab)
    return [
        fault.params[n]
        for n in _ip_names(spec)
        if isinstance(fault.params.get(n), str) and fault.params[n] in refused
    ]


def refused_by_lab(fault: FaultSpec, lab: LabInfo) -> bool:
    """IP 매개변수가 실험망이 주입하지 않는 주소인지 본다."""
    return bool(refused_values(fault, lab))


def canonical_label(label: str) -> str | None:
    """'단계 / 원인' 오탐 라벨을 장애 카탈로그의 정답 라벨로 바꾼다. 모르면 None.

    채점기는 오탐을 진단이 쓴 원문 라벨로 남긴다. 별칭이나 다른 표기도 카탈로그의
    장애를 찾아 같은 정답 라벨로 모아야 힌트가 흩어지지 않는다.
    """
    stage, sep, cause = label.partition(" / ")
    if not sep:
        return None
    fault = lookup_fault(stage, cause)
    return fault.label if fault else None


def unit_of(fault: FaultSpec, lab: LabInfo) -> str:
    """피드백 장애 하나가 속한 출제 단위. IP 장애면 주소 부류를 붙인다."""
    spec = FAULTS.get(fault.id)
    if spec is None or not _ip_names(spec):
        return fault.id
    return f"{fault.id}/{HOST_IP if refused_by_lab(fault, lab) else EMPTY_IP}"


def units(lab: LabInfo, faults: Mapping[str, Fault] = FAULTS) -> list[str]:
    """실험망이 켠 장애의 출제 단위 목록. 호스트 부류는 실험망이 주입하지 않아 뺀다."""
    out = []
    for fid in lab.enabled_faults:
        spec = faults.get(fid)
        if spec is None:
            continue
        out.append(f"{fid}/{EMPTY_IP}" if _ip_names(spec) else fid)
    return sorted(set(out))


def exhausted_faults(
    valid: Sequence[BreakerFeedback],
    target: Scenario,
    max_faults: int,
    faults: Mapping[str, Fault] = FAULTS,
) -> frozenset[str]:
    """더 낼 수 없는 장애 ID 모음.

    매개변수가 모두 고정된 장애는 서명이 하나뿐이다. 그 서명이 유효 라운드에
    있으면 한 장애만 내는 실험망에서는 더 낼 수 없다. 장애를 여럿 묶을 수 있으면
    (max_faults > 1) 다른 장애와 묶은 조합은 새 서명이 되므로 고갈로 보지 않는다.
    """
    if max_faults != 1:
        return frozenset()
    seen = {signature(fb.faults, target.domain) for fb in valid}
    out = set()
    for fid, fault in faults.items():
        if not all(is_fixed(n, p) for n, p in fault.params.items()):
            continue
        params = {
            n: target.port if p.kind == "port" else target.domain for n, p in fault.params.items()
        }
        if signature([FaultSpec(fid, params)], target.domain) in seen:
            out.add(fid)
    return frozenset(out)


def plan(
    history: Sequence[BreakerFeedback],
    lab: LabInfo,
    target_url: str,
    max_faults: int = MAX_FAULTS,
    faults: Mapping[str, Fault] = FAULTS,
) -> Hints:
    """지난 유효 라운드로 출제 힌트를 계산한다. 무효 라운드는 쓰지 않는다.

    평활 오답률 (오답 + 1) / (시도 + 2)는 시도가 적을 때 0.5 근처에 머물러
    우연한 한두 번에 극단으로 치우치지 않는다. 같은 값이면 단위 이름 순이다.
    """
    valid = [fb for fb in history if fb.valid]
    target = Scenario(scenario_id="", target_url=target_url, faults=[], complaint="")
    attempts: Counter[str] = Counter()
    wrong: Counter[str] = Counter()
    false_pos: Counter[str] = Counter()
    for fb in valid:
        unit_by_id = {f.id: unit_of(f, lab) for f in fb.faults}
        for o in fb.outcomes:
            if o.verdict in NOT_ATTEMPTED:
                continue
            unit = unit_by_id.get(o.id, o.id)
            attempts[unit] += 1
            if o.verdict in WRONG_VERDICTS:
                wrong[unit] += 1
        false_pos.update(
            label
            for x in fb.false_positives
            if isinstance(x, str) and (label := canonical_label(x))
        )
    exhausted = exhausted_faults(valid, target, max_faults, faults)
    open_units = [u for u in units(lab, faults) if u.split("/")[0] not in exhausted]
    ranked = sorted(
        ((u, (wrong[u] + 1) / (attempts[u] + 2)) for u in open_units),
        key=lambda item: (-item[1], item[0]),
    )
    top_false = sorted(false_pos.items(), key=lambda kv: (-kv[1], kv[0]))[:OVER_PREDICTED_TOP]
    return Hints(
        ranked=tuple(ranked),
        untried=tuple(u for u, _ in ranked if attempts[u] == 0),
        exhausted=tuple(sorted(exhausted)),
        over_predicted=tuple(label for label, _ in top_false),
    )
