"""Diagnoser가 쓸 수 있는 검사 도구 목록. 도구의 단일 출처다.

도구 이름, 인자, 허용 값, 출력에 반드시 들어갈 내용을 데이터로 둔다.
인자 검사, LLM용 도구 설명(OpenAI tools 형식), 방식별 도구 묶음이 모두
여기서 나온다. 새 도구는 TOOLS에, 새 인자 종류는 _KINDS에 한 줄씩 더한다.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .validate import LabInfo


@dataclass(frozen=True)
class Arg:
    """도구 인자 하나. kind가 허용 값 규칙을 정한다."""

    name: str
    kind: str  # domain | dns_server | url | lab_ip | host | port | count
    required: bool = False
    description: str = ""


@dataclass(frozen=True)
class Tool:
    """검사 도구 하나. must_include는 출력에 있어야 할 정규식 목록이다."""

    name: str
    description: str
    args: tuple[Arg, ...]
    must_include: tuple[str, ...]


# --- 인자 종류별 허용 값

_URL_MAX = 200
_BAD_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")


def _domain(v: object, lab: LabInfo) -> bool:
    return isinstance(v, str) and v in lab.domains


def _lab_ip(v: object, lab: LabInfo) -> bool:
    if not isinstance(v, str):
        return False
    try:
        return ipaddress.IPv4Address(v) in ipaddress.ip_network(lab.subnet)
    except ValueError:
        return False


def _host(v: object, lab: LabInfo) -> bool:
    return _domain(v, lab) or _lab_ip(v, lab)


def _dns_server(v: object, lab: LabInfo) -> bool:
    names = ("dns", "alt_dns")
    return isinstance(v, str) and (v in names or v in {lab.hosts.get(n) for n in names} - {None})


def _url(v: object, lab: LabInfo) -> bool:
    if not isinstance(v, str) or len(v) > _URL_MAX or _BAD_URL_CHARS.search(v):
        return False
    try:
        u = urlsplit(v)
        port = u.port
    except ValueError:
        return False
    return (
        u.scheme in ("http", "https")
        and u.hostname in lab.domains
        and not u.username
        and not u.password
        and (port is None or 1 <= port <= 65535)
    )


def _int_between(lo: int, hi: int) -> Callable[[object, LabInfo], bool]:
    # bool은 int의 하위 타입이라 type으로 직접 비교한다.
    return lambda v, lab: type(v) is int and lo <= v <= hi


_KINDS: dict[str, Callable[[object, LabInfo], bool]] = {
    "domain": _domain,
    "dns_server": _dns_server,
    "url": _url,
    "lab_ip": _lab_ip,
    "host": _host,
    "port": _int_between(1, 65535),
    "count": _int_between(1, 10),
}

# LLM용 JSON 스키마의 타입과 범위
_SCHEMA: dict[str, dict] = {
    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
    "count": {"type": "integer", "minimum": 1, "maximum": 10},
}

# --- 도구 목록. 설명에 장애 원인 이름을 넣지 않는다.

TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "dig",
            "클라이언트에서 도메인의 A 레코드를 DNS 서버에 질의한다. "
            "응답 섹션과 질의 시간을 보여 준다.",
            (
                Arg("domain", "domain", True, "질의할 실험망 도메인"),
                Arg("server", "dns_server", False, "dns 또는 alt_dns(또는 그 IP). 생략하면 dns"),
            ),
            (r"ANSWER: \d+", r"Query time: \d+ msec"),
        ),
        Tool(
            "curl",
            "클라이언트에서 URL로 요청을 보내고 종료 코드, 상태 코드, "
            "단계별 시간(이름 확인, 연결, 첫 바이트, 전체)을 보여 준다.",
            (
                Arg("url", "url", True, "요청할 URL(실험망 도메인, http 또는 https)"),
                Arg("resolve_ip", "lab_ip", False, "주면 이름 확인 없이 이 IP로 접속한다"),
            ),
            (
                r"exit_code=\d+",
                r"http_code=\d+",
                r"time_namelookup=",
                r"time_connect=",
                r"time_total=",
            ),
        ),
        Tool(
            "capture",
            "클라이언트에서 패킷을 캡처하는 동안 URL로 요청을 한 번 보낸다. "
            "패킷마다 줄 머리에 #번호가 붙는다.",
            (Arg("url", "url", True, "요청할 URL(실험망 도메인, http 또는 https)"),),
            (r"(?m)^#\d+ ",),
        ),
        Tool(
            "openssl",
            "클라이언트에서 host:port로 TLS 연결을 열어 서버 인증서의 주체, "
            "유효 기간, 검증 결과를 보여 준다.",
            (
                Arg("host", "domain", True, "접속할 실험망 도메인"),
                Arg("port", "port", False, "포트. 생략하면 443"),
                Arg("servername", "domain", False, "인증서를 요청할 서버 이름. 생략하면 host"),
            ),
            (r"notAfter=|Verify return code",),
        ),
        Tool(
            "ping",
            "클라이언트에서 host로 ICMP 에코를 보내 왕복 시간과 응답 비율을 보여 준다.",
            (
                Arg("host", "host", True, "실험망 도메인 또는 실험망 IP"),
                Arg("count", "count", False, "보낼 횟수(1~10). 생략하면 4"),
            ),
            (r"packet loss",),
        ),
        Tool(
            "tracepath",
            "클라이언트에서 host까지의 홉 목록과 경로에서 보낼 수 있는 최대 크기(pmtu)를 보여 준다.",
            (Arg("host", "host", True, "실험망 도메인 또는 실험망 IP"),),
            (r"pmtu \d+",),
        ),
    )
}

# 설계안 9.2절 비교 방식별로 쓸 수 있는 도구
METHOD_TOOLS: dict[str, frozenset[str]] = {
    "A": frozenset(TOOLS),
    "B": frozenset(),
    "C": frozenset(TOOLS),
    "C-1": frozenset(TOOLS) - {"capture"},
    "C-2": frozenset(TOOLS),
}


def tools_for_method(method: str) -> frozenset[str] | None:
    """진단 방식에 허용된 도구 이름들. 모르는 방식이면 None."""
    return METHOD_TOOLS.get(method)


def check_args(tool: str, args: object, lab: LabInfo) -> str | None:
    """도구 이름과 인자를 검사해 오류 문구를 돌려준다. 통과하면 None."""
    spec = TOOLS.get(tool) if isinstance(tool, str) else None
    if spec is None:
        return f"모르는 도구: {tool!r}"
    if not isinstance(args, dict):
        return f"{tool}: 인자는 객체여야 함"
    known = {a.name: a for a in spec.args}
    unknown = sorted(str(k) for k in args if k not in known)
    if unknown:
        return f"{tool}: 모르는 인자 {unknown}"
    for a in spec.args:
        if a.name not in args:
            if a.required:
                return f"{tool}: {a.name} 인자가 필요함"
            continue
        if not _KINDS[a.kind](args[a.name], lab):
            return f"{tool}: {a.name} 값이 허용 범위 밖 ({args[a.name]!r})"
    return None


def specs(names: Iterable[str]) -> list[dict]:
    """도구 이름들을 LLM용 OpenAI tools 형식으로 바꾼다. 이름순이다."""
    out = []
    for name in sorted(set(names)):
        t = TOOLS[name]
        props = {
            a.name: {**_SCHEMA.get(a.kind, {"type": "string"}), "description": a.description}
            for a in t.args
        }
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": [a.name for a in t.args if a.required],
                        "additionalProperties": False,
                    },
                },
            }
        )
    return out


def missing_parts(tool: str, output: str) -> list[str]:
    """출력에 없는 must_include 패턴들. 실험망 테스트에서 쓴다."""
    return [p for p in TOOLS[tool].must_include if not re.search(p, output)]
