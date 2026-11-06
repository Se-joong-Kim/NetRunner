"""장애 목록(설계안 6장). Breaker는 여기 있는 ID만 제안할 수 있고, 심판이 범위를 검사한다.

각 장애는 정답 라벨(단계/원인), 매개변수 범위, 관측 차단 여부, 근거 지지 판정용 신호를 가진다.
주입 방법(inject)은 실험망 담당이 구현할 때 참고하는 설명일 뿐 심판은 실행하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 단계 이름(값 쪽)은 설계안 9.3절의 단계 정확도 기준과 같다: DNS·경로·TCP·TLS·HTTP·차단.
# 진단 출력의 표기 흔들림을 흡수한다. 키는 소문자로 비교한다.
STAGE_ALIASES = {
    "dns": "DNS",
    "경로": "경로",
    "path": "경로",
    "network": "경로",
    "net": "경로",
    "tcp": "TCP",
    "tls": "TLS",
    "ssl": "TLS",
    "http": "HTTP",
    "차단": "차단",
    "block": "차단",
    "blocking": "차단",
    "middlebox": "차단",
}


@dataclass(frozen=True)
class Param:
    """장애 매개변수 하나의 형식과 허용 범위."""

    kind: str  # "domain" | "ip" | "port" | "int"
    required: bool = True
    min: int | None = None
    max: int | None = None


@dataclass(frozen=True)
class Fault:
    """장애 하나의 정답 라벨, 매개변수, 관측 특성."""

    id: str
    stage: str
    cause: str
    description: str
    inject: str
    params: dict[str, Param] = field(default_factory=dict)
    # 이 장애가 있으면 클라이언트 요청이 이 단계에서 끝나 뒤 단계 장애를 볼 수 없다.
    blocking: bool = False
    # 관측 순서: 1 DNS, 2 경로, 3 TCP, 4 SNI 차단(ClientHello),
    # 5 인증서, 6 HTTP. 작은 값의 차단형 장애가 큰 값의 장애를 가린다.
    layer: int = 0
    # 인용된 근거 출력에서 찾을 정규식. {param}은 해당 매개변수 값으로 바뀐다.
    signals: tuple[str, ...] = ()
    cause_aliases: tuple[str, ...] = ()
    # TLS 핸드셰이크나 SNI(ClientHello)를 거쳐야 보이는 장애. http URL에서는 관측되지 않는다.
    https_only: bool = False

    @property
    def label(self) -> str:
        """'단계 / 원인' 표기. 정답 라벨이다."""
        return label_of(self.stage, self.cause)


def label_of(stage: str, cause: str) -> str:
    """'단계 / 원인' 표기를 만든다. 정답 라벨과 진단 라벨이 이 함수를 같이 쓴다."""
    return f"{stage} / {cause}"


_DOMAIN = Param("domain")
_PORT = Param("port", min=1, max=65535)

FAULTS: dict[str, Fault] = {
    f.id: f
    for f in [
        Fault(
            "D1",
            "DNS",
            "이름 없음",
            "도메인이 없다고 응답",
            "dnsmasq에 NXDOMAIN 설정",
            {"domain": _DOMAIN},
            blocking=True,
            layer=1,
            signals=(r"NXDOMAIN", r"Could not resolve host"),
            cause_aliases=("nxdomain", "도메인 없음"),
        ),
        Fault(
            "D2",
            "DNS",
            "잘못된 주소",
            "틀린 IP 응답",
            "dnsmasq 주소 설정 변경",
            {"domain": _DOMAIN, "ip": Param("ip")},
            blocking=True,
            layer=1,
            signals=(r"{ip}",),
            cause_aliases=("wrong ip", "wrong address", "틀린 ip", "예전 ip", "잘못된 ip"),
        ),
        Fault(
            "D3",
            "DNS",
            "서버 무응답",
            "DNS 응답 없음",
            "라우터에서 UDP 53번 차단",
            {},
            blocking=True,
            layer=1,
            signals=(r"timed out", r"no servers could be reached"),
            cause_aliases=("timeout", "무응답", "응답 없음"),
        ),
        Fault(
            "D4",
            "DNS",
            "지연",
            "DNS만 느림",
            "53번 포트 트래픽만 netem 지연",
            {
                "domain": Param("domain", required=False),
                "delay_ms": Param("int", min=100, max=3000),
            },
            layer=1,
            signals=(r"Query time: \d{3,} msec", r"time_namelookup"),
            cause_aliases=("slow", "delay", "느림"),
        ),
        Fault(
            "T1",
            "TCP",
            "무응답 차단",
            "SYN에 응답 없음",
            "iptables DROP",
            {"port": _PORT},
            blocking=True,
            layer=3,
            signals=(r"timed out", r"[Rr]etransmi", r"재전송"),
            cause_aliases=("drop", "syn 무응답", "timeout"),
        ),
        Fault(
            "T2",
            "TCP",
            "연결 거부",
            "연결 즉시 거부",
            "iptables REJECT --reject-with tcp-reset",
            {"port": _PORT},
            blocking=True,
            layer=3,
            signals=(r"Connection refused", r"\bRST\b", r"\[R\.?\]"),
            cause_aliases=("refused", "connection refused", "rst", "reset"),
        ),
        Fault(
            "N1",
            "경로",
            "지연",
            "지연·지터",
            "tc netem delay",
            {
                "delay_ms": Param("int", min=20, max=2000),
                "jitter_ms": Param("int", required=False, min=0, max=500),
            },
            layer=2,
            # 정상 ping도 time=을 찍으므로 값이 큰 경우(100ms 이상)만 근거로 본다.
            signals=(r"time=\d{3,}",),
            cause_aliases=("latency", "delay", "jitter", "지터"),
        ),
        Fault(
            "N2",
            "경로",
            "손실·재전송",
            "패킷 손실",
            "tc netem loss",
            {"loss_pct": Param("int", min=1, max=30)},
            layer=2,
            # "0% packet loss"는 정상 출력이므로 0이 아닌 손실률만 본다.
            signals=(
                r"(?!0+(?:\.0+)?% packet loss)\d+(?:\.\d+)?% packet loss",
                r"[Rr]etransmi",
                r"재전송",
            ),
            cause_aliases=("loss", "packet loss", "손실", "재전송"),
        ),
        Fault(
            "N3",
            "경로",
            "MTU 블랙홀",
            "큰 패킷만 사라짐",
            "길이 조건 iptables DROP + ICMP 차단",
            {"min_size": Param("int", min=576, max=1500)},
            layer=2,
            signals=(r"pmtu", r"PMTU", r"[Rr]etransmi", r"재전송", r"no reply"),
            cause_aliases=("mtu", "pmtu blackhole", "mtu blackhole"),
        ),
        Fault(
            "N4",
            "경로",
            "큐잉 지연",
            "부하 중 지연 폭증",
            "tc 대역 제한 + 큰 버퍼 + 배경 트래픽",
            {"rate_kbit": Param("int", min=256, max=10000)},
            layer=2,
            signals=(r"p95", r"queue", r"큐"),
            cause_aliases=("bufferbloat", "queuing delay", "queueing delay", "큐잉"),
        ),
        Fault(
            "S1",
            "TLS",
            "인증서 만료",
            "만료된 인증서",
            "만료 인증서로 nginx 재시작",
            {},
            layer=5,
            https_only=True,
            # notAfter=는 정상 인증서 출력에도 나오므로 신호로 쓰지 않는다.
            signals=(r"certificate has expired", r"expired"),
            cause_aliases=("expired", "expired certificate", "만료"),
        ),
        Fault(
            "S2",
            "TLS",
            "이름 불일치",
            "이름이 다른 인증서",
            "다른 도메인 인증서로 교체",
            {"cert_domain": _DOMAIN},
            layer=5,
            https_only=True,
            signals=(
                r"no alternative certificate subject name",
                r"does(?:n't| not) match",
                r"subjectAltName",
            ),
            cause_aliases=("name mismatch", "hostname mismatch", "도메인 불일치"),
        ),
        Fault(
            "H1",
            "HTTP",
            "서버 오류",
            "502 오류",
            "nginx 뒷단 서버 중지",
            {},
            layer=6,
            signals=(r"\b502\b", r"Bad Gateway"),
            cause_aliases=("502", "bad gateway", "upstream down"),
        ),
        Fault(
            "H2",
            "HTTP",
            "리다이렉트 순환",
            "무한 리다이렉트",
            "nginx 301 순환 설정",
            {},
            layer=6,
            signals=(r"Maximum \(\d+\) redirects", r"too many redirects", r"\b301\b"),
            cause_aliases=("redirect loop", "too many redirects", "무한 리다이렉트"),
        ),
        Fault(
            "B1",
            "차단",
            "SNI 기반",
            "SNI 보고 연결 끊기",
            "iptables 문자열 일치(SNI 바이트) → tcp-reset",
            {"domain": _DOMAIN},
            blocking=True,
            layer=4,
            https_only=True,
            signals=(r"\bRST\b", r"Connection reset", r"reset by peer"),
            cause_aliases=("sni", "sni filtering", "sni 차단"),
        ),
        Fault(
            "B2",
            "차단",
            "DNS 변조",
            "특정 도메인만 가짜 IP",
            "dnsmasq가 차단 안내 IP 응답",
            {"domain": _DOMAIN, "block_ip": Param("ip")},
            blocking=True,
            layer=1,
            signals=(r"{block_ip}",),
            cause_aliases=("dns tampering", "dns hijack", "dns 위조"),
        ),
    ]
}


def normalize_stage(stage: str) -> str | None:
    """진단의 단계 표기를 표준 단계 이름으로 바꾼다. 모르면 None."""
    return STAGE_ALIASES.get(stage.strip().lower())


def cause_matches(fault: Fault, cause: str) -> bool:
    """진단의 원인 표기가 장애의 원인 또는 그 별칭과 같은지 본다."""
    c = cause.strip().lower()
    return c == fault.cause.lower() or c in fault.cause_aliases


def lookup_fault(stage: str, cause: str) -> Fault | None:
    """진단의 (단계, 원인) 표기를 목록의 장애로 되돌린다. 목록에 없는 원인이면 None."""
    st = normalize_stage(stage)
    return next((f for f in FAULTS.values() if f.stage == st and cause_matches(f, cause)), None)
