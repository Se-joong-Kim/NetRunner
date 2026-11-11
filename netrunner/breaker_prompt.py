"""Breaker가 받을 프롬프트를 만든다. 외부 호출 없이 문자열만 만든다.

장애 목록(허용 범위 포함), 증상 작성 금지어, 지난 대결 라운드 요약을
한 문자열에 담는다. 어떤 모델을 쓰는지와는 무관하다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from .contracts import BreakerFeedback, Scenario
from .faults import FAULTS, Fault, Param
from .validate import CLUE_WORDS, CONFLICT_GROUPS, MAX_COMPLAINT_LEN, MAX_FAULTS, LabInfo

# 프롬프트에 넣을 지난 라운드 수. 최근 것부터 고른다.
HISTORY_LIMIT = 10


def _param_how(name: str, p: Param, target: Scenario, lab: LabInfo) -> str:
    """매개변수 하나의 허용 값을 사람이 읽을 문장으로 바꾼다."""
    if p.kind == "domain":
        if name == "domain":
            return f"대상 도메인 {target.domain}"
        return f"실험망 도메인({', '.join(lab.domains)}) 중 대상과 다른 것"
    if p.kind == "ip":
        return f"실험망 대역 {lab.subnet} 안의 IP, 정상 서버 {lab.web_ip} 제외"
    if p.kind == "port":
        return f"정수, 대상 포트 {target.port}로 고정"
    return f"정수 {p.min}~{p.max}"


def _fault_line(fault: Fault, target: Scenario, lab: LabInfo) -> str:
    """장애 하나를 'ID: 설명; 매개변수' 한 줄로 만든다."""
    params = "; ".join(
        f"{name}({_param_how(name, p, target, lab)}{'' if p.required else ', 선택'})"
        for name, p in fault.params.items()
    )
    return f"- {fault.id}: {fault.description}" + (f" / 매개변수: {params}" if params else "")


def _history_line(fb: BreakerFeedback) -> str:
    """지난 라운드 하나를 한 줄로 요약한다. 정답 공개가 끝난 라운드만 들어온다."""
    status = "유효" if fb.valid else f"무효({fb.invalid_reason})"
    faults = ", ".join(f"{f.id} {json.dumps(f.params, ensure_ascii=False)}" for f in fb.faults)
    verdicts = "; ".join(f"{o.id} {o.label}={o.verdict}" for o in fb.outcomes) or "판정 없음"
    false_pos = ", ".join(fb.false_positives) or "없음"
    return (
        f"- 시나리오 {fb.scenario_id} [{status}] 장애: {faults} / "
        f'증상: "{fb.complaint}" / 판정: {verdicts} / 오탐: {false_pos}'
    )


def build_prompt(
    history: list[BreakerFeedback],
    target_url: str,
    lab: LabInfo,
    faults: Mapping[str, Fault] = FAULTS,
) -> str:
    """지난 라운드와 실험망 정보로 Breaker 프롬프트 한 문자열을 만든다.

    history에는 고정 세트 라운드가 없어야 한다(심판의 feedback_for가 거른다).
    """
    target = Scenario(scenario_id="", target_url=target_url, faults=[], complaint="")
    enabled = [faults[i] for i in lab.enabled_faults if i in faults]
    conflicts = "; ".join(str(sorted(g)) for g in CONFLICT_GROUPS)
    recent = history[-HISTORY_LIMIT:]
    history_block = "\n".join(_history_line(fb) for fb in recent) or "- 지난 라운드 없음"
    return "\n".join(
        [
            "너는 네트워크 장애를 설계하는 출제자다. 진단자는 증상 문장만 보고 원인을 찾는다.",
            "장애를 골라 조합하고, 원인이 드러나지 않는 사용자 불만 한 문장을 쓴다.",
            "",
            "## 규칙",
            f"- 장애는 1~{MAX_FAULTS}개. 아래 목록의 ID만 쓴다.",
            f"- 함께 걸 수 없는 조합: {conflicts}",
            f"- 대상 URL: {target_url} (도메인 {target.domain}, 포트 {target.port})",
            f"- 증상 문장은 {MAX_COMPLAINT_LEN}자 이내. 다음 단어를 쓰지 않는다: "
            + ", ".join(CLUE_WORDS),
            f"- 정상 서버 IP({lab.web_ip})와 같은 IP는 장애가 아니다.",
            "- 진단이 틀렸던 장애(cause_wrong, stage_wrong, missed)를 다시 시험해 볼 만하다.",
            "  같은 장애 조합과 매개변수는 반복하지 않는다.",
            "",
            "## 장애 목록",
            *(_fault_line(f, target, lab) for f in enabled),
            "",
            "## 지난 라운드",
            history_block,
            "",
            "## 출력",
            "JSON 객체 하나만 출력한다. 다른 말은 쓰지 않는다.",
            '{"faults": [{"id": "장애 ID", "params": {...}}], '
            '"complaint": "증상 한 문장", "note": "사람용 메모(선택)"}',
        ]
    )


def with_rejection(prompt: str, reason: str) -> str:
    """직전 제안이 거부된 이유를 붙여 다시 제안받는 프롬프트를 만든다."""
    return (
        f"{prompt}\n\n직전 제안이 거부됨: {reason}\n"
        "같은 실수를 피해 다시 제안하라. JSON 객체 하나만 출력하라."
    )
