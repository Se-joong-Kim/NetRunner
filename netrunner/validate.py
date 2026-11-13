"""심판의 시나리오 검사(설계안 8장의 규칙). Breaker는 제안만 하고, 허용 여부는 여기서 정한다."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import Scenario, read_json
from .faults import FAULTS, Fault

# 증상 설명에 들어가면 원인이 드러나는 단어(설계안 8장: "MTU", "인증서" 같은 단어는 거부).
CLUE_WORDS = (
    "DNS",
    "TCP",
    "UDP",
    "TLS",
    "SSL",
    "HTTP",
    "MTU",
    "RST",
    "SNI",
    "ICMP",
    "NXDOMAIN",
    "IP",
    "port",
    "packet",
    "certificate",
    "redirect",
    "firewall",
    "iptables",
    "netem",
    "resolv",
    "502",
    "301",
    "SYN",
    "포트",
    "인증서",
    "패킷",
    "손실",
    "재전송",
    "리다이렉트",
    "방화벽",
    "차단",
    "라우터",
    "네임서버",
    "이름 해석",
    "도메인 해석",
    "핸드셰이크",
)
# 한국어 조사가 바로 붙어도("IP가") 잡히도록 \b 대신 영숫자 경계만 본다.
# 영어 활용형(redirects, resolved, certificates)도 잡도록 접미사를 허용한다.
_ASCII_CLUES = re.compile(
    r"(?<![A-Za-z0-9])((?:"
    + "|".join(re.escape(w) for w in CLUE_WORDS if w.isascii())
    + r")(?:e|s|es|d|ed|ing)?)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_KOREAN_CLUES = tuple(w for w in CLUE_WORDS if not w.isascii())

# 한 라운드에 같이 걸면 서로를 덮어써서 정답이 모호해지는 묶음.
CONFLICT_GROUPS = (
    {"D1", "D2", "D3", "B2"},  # DNS 응답 자체를 바꾸는 장애
    {"T1", "T2"},
    {"S1", "S2"},
    {"H1", "H2"},
)

MAX_FAULTS = 3
MAX_COMPLAINT_LEN = 300


@dataclass
class LabInfo:
    """실험망 담당이 확정하는 주소 정보. config/lab.json에서 읽는다."""

    domains: list[str]
    web_ip: str
    subnet: str
    enabled_faults: list[str] = field(default_factory=lambda: sorted(FAULTS))
    # 이름 → IP. 검사 도구가 DNS 서버 이름(dns, alt_dns)을 IP로 확인할 때 쓴다.
    hosts: dict[str, str] = field(default_factory=dict)
    # 실험망이 실행할 수 있는 검사 도구. 없으면 도구를 쓸 수 없다.
    enabled_tools: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> LabInfo:
        """config/lab.json 형식의 파일에서 실험망 정보를 읽는다."""
        d = read_json(path)
        optional = ("enabled_faults", "hosts", "enabled_tools")
        extra = {k: d[k] for k in optional if k in d}
        return cls(domains=d["domains"], web_ip=d["web_ip"], subnet=d["subnet"], **extra)


@dataclass
class ValidationResult:
    """시나리오 검사 결과: 오류(거부 사유)와 주의 사항."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 시나리오의 faults 순서와 같은 길이.
    # False면 선행 장애에 가려 클라이언트에서 볼 수 없다.
    observable: list[bool] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """오류가 하나도 없으면 True."""
        return not self.errors


def find_clues(text: str) -> list[str]:
    """설명문에 들어 있는 원인 단서 단어를 모두 찾는다."""
    found = [m.group(0) for m in _ASCII_CLUES.finditer(text)]
    found += [w for w in _KOREAN_CLUES if w in text]
    return found


def observability(faults: list[Fault]) -> list[bool]:
    """선행 단계의 차단형 장애가 있으면 뒤 단계 장애는 관측 불가로 본다.

    경로 장애는 ping·tracepath로 서버 IP에 직접 확인할 수 있으므로 가려지지 않는다.
    """
    return [
        f.stage == "경로" or not any(g.blocking and g.layer < f.layer for g in faults)
        for f in faults
    ]


def _check_param(fault: Fault, name: str, value, scenario: Scenario, lab: LabInfo) -> str | None:
    spec = fault.params[name]
    where = f"{fault.id}.{name}"
    if spec.kind == "domain":
        if value not in lab.domains:
            return f"{where}: 실험망 도메인이 아님 ({value})"
        if name == "domain" and value != scenario.domain:
            return (
                f"{where}: 대상 URL의 도메인({scenario.domain})과 달라 "
                "클라이언트 증상으로 나타나지 않음"
            )
        if name == "cert_domain" and value == scenario.domain:
            return f"{where}: 대상 도메인과 같으면 이름 불일치가 생기지 않음"
    elif spec.kind == "ip":
        # ip_address는 정수도 받으므로 문자열만 허용한다(계약: IP 문자열).
        if not isinstance(value, str):
            return f"{where}: IP 형식이 아님 ({value})"
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return f"{where}: IP 형식이 아님 ({value})"
        if ip not in ipaddress.ip_network(lab.subnet):
            return f"{where}: 실험망 대역({lab.subnet}) 밖의 IP"
        if str(ip) == lab.web_ip:
            return f"{where}: 정상 서버 IP와 같아 장애가 아님"
    elif spec.kind in ("int", "port"):
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{where}: 정수가 아님 ({value!r})"
        if spec.min is not None and value < spec.min or spec.max is not None and value > spec.max:
            return f"{where}: 범위 밖 ({value}, 허용 {spec.min}~{spec.max})"
        if spec.kind == "port" and value != scenario.port:
            return f"{where}: 대상 URL 포트({scenario.port})가 아니라 관측할 수 없음"
    return None


def validate(scenario: Scenario, lab: LabInfo) -> ValidationResult:
    """시나리오 하나를 설계안 8장 규칙으로 검사한다."""
    r = ValidationResult()

    # 매개변수 검사가 scenario.port를 쓰므로 URL 형식부터 확인한다.
    try:
        u = scenario.url
        port = u.port
    except ValueError as e:
        r.errors.append(f"target_url: URL 형식 오류 ({e})")
        return r
    if port == 0:  # urlsplit은 0을 오류 없이 돌려준다
        r.errors.append("target_url: 포트는 1~65535여야 함")
        return r
    if u.scheme not in ("http", "https"):
        r.errors.append(f"target_url: http/https가 아님 ({scenario.target_url})")
        return r
    https_needed = [s.id for s in scenario.faults if s.id in FAULTS and FAULTS[s.id].https_only]
    if https_needed and u.scheme != "https":
        r.errors.append(f"target_url: {https_needed}은(는) https URL에서만 관측됨")

    if scenario.domain not in lab.domains:
        r.errors.append(f"target_url: 실험망 도메인이 아님 ({scenario.domain})")

    if not 1 <= len(scenario.faults) <= MAX_FAULTS:
        r.errors.append(f"faults: 1~{MAX_FAULTS}개여야 함 (현재 {len(scenario.faults)}개)")

    ids = [f.id for f in scenario.faults]
    if len(set(ids)) != len(ids):
        r.errors.append(f"faults: 같은 장애 중복 {ids}")
    for group in CONFLICT_GROUPS:
        both = sorted(group.intersection(ids))
        if len(both) > 1:
            r.errors.append(f"faults: 함께 걸 수 없는 조합 {both}")

    known: list[Fault] = []
    for spec in scenario.faults:
        fault = FAULTS.get(spec.id)
        if fault is None:
            r.errors.append(f"{spec.id}: 허용 목록에 없는 장애")
            continue
        if spec.id not in lab.enabled_faults:
            r.errors.append(f"{spec.id}: 아직 실험망에서 구현되지 않은 장애")
        known.append(fault)
        for name in spec.params:
            if name not in fault.params:
                r.errors.append(f"{spec.id}.{name}: 정의되지 않은 매개변수")
        for name, p in fault.params.items():
            if name not in spec.params:
                if p.required:
                    r.errors.append(f"{spec.id}.{name}: 필수 매개변수 없음")
                continue
            err = _check_param(fault, name, spec.params[name], scenario, lab)
            if err:
                r.errors.append(err)

    complaint = scenario.complaint.strip()
    if not complaint:
        r.errors.append("complaint: 비어 있음")
    elif len(complaint) > MAX_COMPLAINT_LEN:
        r.errors.append(f"complaint: {MAX_COMPLAINT_LEN}자 초과")
    clues = find_clues(complaint)
    if clues:
        r.errors.append(f"complaint: 원인 단서가 들어 있음 {clues}")

    if len(known) == len(scenario.faults):
        r.observable = observability(known)
        for f, seen in zip(known, r.observable, strict=True):
            if not seen:
                r.warnings.append(f"{f.id}({f.label}): 선행 장애에 가려 관측 불가 — 채점에서 제외")
        if known and not any(r.observable):
            r.errors.append("faults: 관측 가능한 장애가 없음")
    return r
