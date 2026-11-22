"""방식 A: 정해진 순서로 검사하고 if-else 규칙으로 원인을 고르는 기준선.

LLM을 쓰지 않는다. 심판이 넘긴 라운드 정보(RoundBrief)와 도구 창구만
쓰고, 원인 이름은 장애 목록(faults.py)의 라벨만 낸다. 규칙은 장애
목록의 정의와 실험망의 정상·장애 출력 형식을 보고 정했고, 고정 세트
결과에 맞춰 고치지 않는다. 채점용 신호(faults.py의 signals)는 쓰지
않는다.

검사 순서(항상 같다)
  1. dig: 기본 DNS, 보조 DNS
  2. curl: 대상 URL
  3. ping: 보조 DNS가 알려 준 주소로 작은 패킷, 큰 패킷
  4. 필요할 때만: 기본 DNS 재질의, 캡처(기본 DNS만 무응답일 때),
     기본 DNS가 준 주소로 HTTP 요청, 리다이렉트 따라가기

규칙(단계 순서)
  DNS   NXDOMAIN → 이름 없음. 보조 DNS는 답하는데 기본 DNS만 두 번
        무응답 → 서버 무응답. 이때 접속 요청의 이름 확인이 느리게라도
        성공하거나 캡처에 늦은 응답이 보이면 지연, 이름 확인이 빠르게
        성공하면 일시적 손실로 보고 넘긴다.
        보조 DNS와 다른 주소 → 그 주소가 웹 페이지를 주면 DNS 변조,
        아니면 잘못된 주소. 보조 DNS보다 100ms 이상 느림 → 지연.
        경로가 느리면 DNS 쪽 원인은 내지 않고 경로 탓으로 본다.
  경로  왕복 10ms 이상 → 작은·큰 ping 평균이 25% 이상 다르면 큐잉 지연,
        아니면 지연. 지연이 없을 때 손실이 있으면 손실, 작은 패킷은
        모두 가고 큰 패킷만 모두 사라지면 MTU 블랙홀.
  TCP   연결 거부 → 연결 거부. 연결 시간 초과(경로 정상) → 무응답 차단.
  TLS   인증서 만료, 이름 불일치. https에서 핸드셰이크 중 연결 리셋 → SNI 차단.
  HTTP  5xx → 서버 오류. 리다이렉트가 지나온 주소로 돌아오면 순환.
  DNS가 막혀 접속이 가짜 주소로 가면 TCP 이후 단계는 판단하지 않는다.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .contracts import Diagnosis, Evidence, Finding, RoundBrief
from .faults import FAULTS
from .toolbox import Toolbox

# 기본 DNS가 보조 DNS보다 이만큼 느리면 DNS 지연으로 본다(D4 최소 100ms).
SLOW_DNS_MS = 100
# 왕복 시간 평균이 이 값 이상이면 경로가 느리다고 본다. 실험망의
# 평소 왕복 시간은 1ms 미만이다(faults.py N1 주석).
SLOW_PATH_MS = 10
# 작은·큰 ping의 평균이 이 비율 이상 다르면 지연이 부하에 따라 바뀌는
# 큐잉 지연으로 본다. 고정 지연은 크기와 시점에 상관없이 평균이 같다.
QUEUE_SHIFT = 0.25
# 경로 MTU를 볼 큰 ping의 데이터 크기. IP 패킷으로 1500바이트다.
BIG_PING_SIZE = 1472
PING_COUNT = 10
BIG_PING_COUNT = 3
MAX_REDIRECTS = 5


@dataclass(frozen=True)
class Seen:
    """실행된 검사 하나: 검사 번호와 원본 출력."""

    probe_id: str
    output: str

    @property
    def ref(self) -> str:
        """검사 전체를 가리키는 근거 참조."""
        return f"probe:{self.probe_id}"

    def packet(self, number: int) -> str:
        """캡처의 패킷 하나를 가리키는 근거 참조."""
        return f"pcap:{self.probe_id}#{number}"


@dataclass(frozen=True)
class DigView:
    """dig 출력에서 규칙에 쓰는 값."""

    status: str | None
    addresses: frozenset[str]
    query_ms: int | None
    timed_out: bool


@dataclass(frozen=True)
class CurlView:
    """curl 출력에서 규칙에 쓰는 값."""

    exit_code: int | None
    http_code: int
    lookup_s: float
    connect_s: float
    location: str | None

    @property
    def resolved(self) -> bool:
        """이름 확인을 마쳤는지. 마치지 못하면 시간이 0으로 남는다."""
        return self.lookup_s > 0 or self.connect_s > 0


@dataclass(frozen=True)
class PingView:
    """ping 요약에서 규칙에 쓰는 값."""

    sent: int
    received: int
    avg_ms: float | None


def read_dig(output: str) -> DigView:
    """DNS 질의(dig) 출력에서 응답 상태, A 레코드 주소, 질의 시간, 무응답을 읽는다."""
    status = re.search(r"status: (\w+)", output)
    query = re.search(r"Query time: (\d+) msec", output)
    return DigView(
        status.group(1) if status else None,
        frozenset(re.findall(r"(?m)^\S+\s+\d+\s+IN\s+A\s+(\d+\.\d+\.\d+\.\d+)\s*$", output)),
        int(query.group(1)) if query else None,
        status is None and bool(re.search(r"timed out|no servers could be reached", output)),
    )


def read_curl(output: str) -> CurlView:
    """HTTP 요청(curl) 출력의 -w 값과 -v 기록에서 규칙에 쓰는 값을 읽는다."""

    def value(name: str) -> str | None:
        m = re.search(rf"{name}=([\d.]+)", output)
        return m.group(1) if m else None

    exit_code = value("exit_code")
    location = re.search(r"(?mi)^< location:\s*(\S+)", output)
    return CurlView(
        int(exit_code) if exit_code else None,
        int(value("http_code") or 0),
        float(value("time_namelookup") or 0),
        float(value("time_connect") or 0),
        location.group(1) if location else None,
    )


def read_ping(output: str) -> PingView:
    """왕복 확인(ping) 요약 줄에서 보낸 수, 받은 수, 평균 왕복 시간을 읽는다."""
    counts = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", output)
    rtt = re.search(r"min/avg/max(?:/mdev)? = [\d.]+/([\d.]+)/", output)
    sent, received = (int(counts.group(1)), int(counts.group(2))) if counts else (0, 0)
    return PingView(sent, received, float(rtt.group(1)) if rtt else None)


def _finding(fault_id: str, seen: Seen, claim: str, ref: str | None = None) -> Finding:
    fault = FAULTS[fault_id]
    return Finding(fault.stage, fault.cause, [Evidence(ref or seen.ref, claim)])


class _Probes:
    """도구 창구를 감싼다. 쓸 수 없는 도구나 한도가 끝난 검사는 부르지 않는다."""

    def __init__(self, tools: Toolbox):
        self._tools = tools
        self._available = {s["function"]["name"] for s in tools.specs()}

    def run(self, tool: str, args: dict) -> Seen | None:
        if tool not in self._available or not self._tools.remaining():
            return None
        view = self._tools.call(tool, args)
        return Seen(view.probe_id, view.output) if view.probe_id else None


@dataclass(frozen=True)
class _Path:
    """ping으로 본 경로 상태. 확인하지 못했으면 ok와 delayed 모두 False."""

    ok: bool  # 작은 ping이 오가고 지연이 없다
    delayed: bool


class RuleDiagnoser:
    """정해진 검사 순서와 규칙으로 진단하는 방식 A 기준선."""

    model = "rules"
    method = "A"

    def diagnose(self, brief: RoundBrief, tools: Toolbox) -> Diagnosis:
        """검사 결과에 규칙을 적용해 진단을 돌려준다."""
        started = time.monotonic()
        probes = _Probes(tools)
        url = brief.target_url
        host = urlsplit(url).hostname or ""

        primary = probes.run("dig", {"domain": host, "server": "dns"})
        alt = probes.run("dig", {"domain": host, "server": "alt_dns"})
        web = probes.run("curl", {"url": url})
        path_host = _path_host(host, primary, alt)
        small = probes.run("ping", {"host": path_host, "count": PING_COUNT})
        big = probes.run(
            "ping", {"host": path_host, "count": BIG_PING_COUNT, "size": BIG_PING_SIZE}
        )

        path_findings, path = _path_findings(small, big)
        findings, blocked = _dns_findings(probes, url, primary, alt, web, path)
        findings += path_findings
        if not blocked:
            findings += _connection_findings(probes, url, web, path)
        result = Diagnosis(brief.round_id, self.model, self.method, findings)
        result.elapsed_s = time.monotonic() - started
        return result


def _path_host(host: str, primary: Seen | None, alt: Seen | None) -> str:
    """경로를 볼 주소. 보조 DNS 답을 먼저 쓰고, 없으면 기본 DNS 답, 그다음 이름."""
    for seen in (alt, primary):
        addresses = read_dig(seen.output).addresses if seen else frozenset()
        if addresses:
            return min(addresses)
    return host


def _dns_findings(
    probes: _Probes,
    url: str,
    primary: Seen | None,
    alt: Seen | None,
    web: Seen | None,
    path: _Path,
) -> tuple[list[Finding], bool]:
    """DNS 단계 규칙. (진단, 뒤 단계가 막혔는지)를 돌려준다."""
    if primary is None:
        return [], False
    host = urlsplit(url).hostname or ""
    p = read_dig(primary.output)
    a = read_dig(alt.output) if alt else None
    alt_answers = bool(a and a.addresses)

    if p.status == "NXDOMAIN":
        return [_finding("D1", primary, "기본 DNS가 NXDOMAIN(이름 없음)으로 응답")], True

    if p.timed_out:
        retry = probes.run("dig", {"domain": host, "server": "dns"})
        if retry and not read_dig(retry.output).timed_out:
            primary, p = retry, read_dig(retry.output)
        else:
            return _dns_timeout(probes, url, retry or primary, web, alt_answers, path)

    if p.addresses and alt_answers and p.addresses != a.addresses:
        ip = min(p.addresses)
        page = probes.run("curl", {"url": f"http://{host}/", "resolve_ip": ip})
        if page and 200 <= read_curl(page.output).http_code < 400:
            claim = (
                f"기본 DNS가 보조 DNS와 다른 주소 {ip}를 주고, 그 주소는 다른 웹 페이지를 보여 줌"
            )
            return [_finding("B2", primary, claim)], True
        claim = f"기본 DNS가 보조 DNS({', '.join(sorted(a.addresses))})와 다른 주소 {ip}를 줌"
        return [_finding("D2", primary, claim)], True

    if (
        not path.delayed
        and a
        and p.query_ms is not None
        and a.query_ms is not None
        and p.query_ms - a.query_ms >= SLOW_DNS_MS
    ):
        claim = f"기본 DNS 질의 {p.query_ms}ms, 보조 DNS {a.query_ms}ms"
        return [_finding("D4", primary, claim)], False
    return [], False


def _dns_timeout(
    probes: _Probes,
    url: str,
    primary: Seen,
    web: Seen | None,
    alt_answers: bool,
    path: _Path,
) -> tuple[list[Finding], bool]:
    """기본 DNS가 두 번 다 시간 초과했을 때. 이름 확인과 캡처로 가른다."""
    if not alt_answers or path.delayed:
        return [], False  # 보조 DNS도 답이 없거나 경로가 느리면 DNS만의 문제로 좁히지 않는다
    w = read_curl(web.output) if web else None
    if web and w and w.resolved:
        if w.lookup_s * 1000 < SLOW_DNS_MS:
            return [], False  # 이름 확인은 빨랐다: dig의 시간 초과는 일시적인 손실이다
        claim = (
            "dig는 기본 DNS 응답을 기다리다 시간 초과했지만, 접속 요청은 "
            f"이름 확인에 {w.lookup_s:.1f}초 걸려 성공"
        )
        return [_finding("D4", web, claim)], False
    late = _late_dns_reply(probes, url, primary)
    if late:
        return [late], False
    return [_finding("D3", primary, "기본 DNS가 두 번 모두 응답하지 않음(보조 DNS는 응답)")], True


def _late_dns_reply(probes: _Probes, url: str, primary: Seen) -> Finding | None:
    """캡처에서 기본 DNS의 늦은 응답을 찾는다. 있으면 DNS 지연이다.

    지연이 클라이언트의 재질의 간격보다 길면 dig와 접속 요청은 응답을
    받지 못하지만, 캡처는 요청이 끝난 뒤에도 잠시 패킷을 기록한다.
    """
    server = re.search(r"(\d+\.\d+\.\d+\.\d+)#53", primary.output)
    cap = probes.run("capture", {"url": url}) if server else None
    if not server or not cap:
        return None
    dns = f"{server.group(1)}.53"
    asked: dict[tuple[str, str], float] = {}  # (클라이언트 주소.포트, DNS ID) → 질의 시각
    replies: list[tuple[str, tuple[str, str], float]] = []
    for n, h, m, s, src, dst, dns_id in re.findall(
        r"(?m)^#(\d+) (\d+):(\d+):([\d.]+) IP (\S+) > ([^\s:]+): (\d+)", cap.output
    ):
        at = int(h) * 3600 + int(m) * 60 + float(s)
        if dst == dns:
            asked.setdefault((src, dns_id), at)
        elif src == dns:
            replies.append((n, (dst, dns_id), at))
    if not replies:
        return None
    # 캡처 안의 질의와 짝지어지는 응답을 먼저 쓴다. 캡처 전에 보낸 질의(이전
    # dig)의 응답도 늦은 응답이지만 얼마나 늦었는지는 잴 수 없다.
    paired = [(n, at - asked[key]) for n, key, at in replies if key in asked]
    number, waited = paired[0] if paired else (replies[0][0], None)
    when = f" 질의 {waited:.1f}초 뒤에" if waited is not None else ""
    claim = f"dig와 접속 요청은 시간 초과했지만, 캡처에는 기본 DNS 응답이{when} 도착함"
    return _finding("D4", cap, claim, ref=cap.packet(int(number)))


def _path_findings(small: Seen | None, big: Seen | None) -> tuple[list[Finding], _Path]:
    """경로 단계 규칙. (진단, 경로 상태)를 돌려준다."""
    s = read_ping(small.output) if small else None
    b = read_ping(big.output) if big else None
    if not small or not s or not s.received:
        return [], _Path(ok=False, delayed=False)  # 경로가 끊겼거나 확인하지 못함

    averages = [v.avg_ms for v in (s, b) if v and v.avg_ms is not None]
    if max(averages, default=0) >= SLOW_PATH_MS:
        path = _Path(ok=False, delayed=True)
        if len(averages) == 2 and abs(averages[0] - averages[1]) >= QUEUE_SHIFT * max(averages):
            claim = (
                f"작은 ping 평균 {averages[0]:.0f}ms, 큰 ping 평균 {averages[1]:.0f}ms로 "
                "지연이 측정마다 크게 바뀜"
            )
            return [_finding("N4", small, claim)], path
        return [_finding("N1", small, f"왕복 시간 평균 {s.avg_ms:.0f}ms")], path

    findings = []
    if s.received < s.sent:
        findings.append(_finding("N2", small, f"ping {s.sent}개 중 {s.received}개만 돌아옴"))
    elif big and b and 0 < b.received < b.sent:
        findings.append(_finding("N2", big, f"ping {b.sent}개 중 {b.received}개만 돌아옴"))
    if big and b and s.received == s.sent and b.sent and not b.received:
        claim = f"작은 ping은 모두 오가지만 {BIG_PING_SIZE}바이트 ping은 모두 사라짐"
        findings.append(_finding("N3", big, claim))
    return findings, _Path(ok=True, delayed=False)


def _connection_findings(
    probes: _Probes, url: str, web: Seen | None, path: _Path
) -> list[Finding]:
    """TCP·TLS·HTTP 단계 규칙. 대상 URL로 보낸 요청 결과를 본다."""
    w = read_curl(web.output) if web else None
    if not web or not w or not w.resolved:
        return []
    text = web.output
    if w.exit_code == 7 and "Connection refused" in text:
        return [_finding("T2", web, "TCP 연결이 즉시 거부됨(Connection refused)")]
    if w.exit_code == 28 and not w.connect_s:
        # 경로가 느리거나 확인하지 못했으면 시간 초과를 차단으로 단정하지 않는다.
        if not path.ok:
            return []
        return [_finding("T1", web, "ping은 오가지만 TCP 연결이 시간 초과(응답 없음)")]
    if w.exit_code == 60 and re.search(r"certificate has expired", text):
        return [_finding("S1", web, "서버 인증서 검증 실패: 만료된 인증서")]
    if w.exit_code == 60 and re.search(r"subject name|does(?:n't| not) match", text):
        return [_finding("S2", web, "서버 인증서의 이름이 접속한 도메인과 맞지 않음")]
    if (
        urlsplit(url).scheme == "https"
        and w.exit_code in (35, 56)
        and w.connect_s
        and "reset" in text.lower()
    ):
        return [_finding("B1", web, "TCP 연결 뒤 TLS 핸드셰이크 중 연결이 리셋됨")]
    if 500 <= w.http_code < 600:
        return [_finding("H1", web, f"HTTP {w.http_code} 응답")]
    if 300 <= w.http_code < 400 and _redirect_loops(probes, url, w.location):
        return [_finding("H2", web, f"HTTP {w.http_code} 리다이렉트가 지나온 주소로 돌아옴")]
    return []


def _redirect_loops(probes: _Probes, url: str, location: str | None) -> bool:
    """리다이렉트를 따라가다 지나온 주소로 돌아오면 True."""
    visited = {_normalize(url)}
    current = url
    for _ in range(MAX_REDIRECTS):
        if not location:
            return False
        current = urljoin(current, location)
        if _normalize(current) in visited:
            return True
        visited.add(_normalize(current))
        hop = probes.run("curl", {"url": current})
        if hop is None:
            return False
        h = read_curl(hop.output)
        if not 300 <= h.http_code < 400:
            return False
        location = h.location
    return False


def _normalize(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme.lower()}://{(u.netloc or '').lower()}{u.path or '/'}?{u.query}"
