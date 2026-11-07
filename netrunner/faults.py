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
    # 신호에 (?P<v>...) 그룹이 있을 때, 그 값이 이 매개변수 값 이상이어야 근거로 본다.
    value_param: str | None = None
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
            # 자동 판정 조건: 출력의 time= 값 >= 주입한 delay_ms.
            # 가정
            #  - 주입 지연이 왕복 시간(RTT)에 그대로 더해진다고 본다.
            #    netem이 한쪽 방향에만 걸려 있어도 RTT >= delay_ms다.
            #  - 측정 도구가 ping이어야 한다("time=12.3 ms" 형식).
            # 한계
            #  - jitter_ms가 있으면 개별 값이 delay_ms보다 작을 수
            #    있다. 그 인용은 자동 지지가 안 되고 수동 확인이 된다.
            #  - 정상 RTT가 주입 지연보다 크면 정상 출력이 지지로
            #    잘못 판정될 수 있다. 실험망 기본 RTT는 1ms 안팎이라
            #    최소 delay 20ms에서도 여유가 있지만, 망이 바뀌면 다시 본다.
            #  - mtr, traceroute 같은 다른 도구의 지연 표기는 잡지 않는다.
            signals=(r"time=(?P<v>\d+(?:\.\d+)?)",),
            value_param="delay_ms",
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
            # 자동 판정 신호는 세 가지다.
            #  - "N% packet loss": ping 요약. 0이 아닌 손실률만 인정한다
            #    ("0% packet loss"는 정상 출력).
            #  - "retransmi", "재전송": TCP 재전송 표기. 정상 출력에도
            #    드물게 나올 수 있다.
            # 한계
            #  - mtr의 "Loss%" 열, ss 통계 등은 잡지 않는다. 그 인용은
            #    수동 확인이 된다.
            #  - 손실률 값은 주입값과 비교하지 않는다. 0이 아니면 지지로
            #    보므로, 주입 손실과 무관한 손실도 지지될 수 있다.
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
            # 자동 판정 신호는 세 가지다.
            #  - "certificate has expired": curl, openssl verify 등 검증
            #    오류 문구.
            #  - "Certificate will expire": openssl x509 -checkend 0 출력.
            #    만료됐을 때만 나오고 "will not expire"와는 다르다.
            #  - "expired": 위 두 문구의 변형을 넓게 잡는다.
            # 가정
            #  - 진단자가 -checkend 0(기준 시각 0초)으로 실행했을 때만
            #    "will expire"가 나온다. 다른 기간을 주면 아직 유효한
            #    인증서도 "will expire"가 나와 잘못 지지된다.
            # 한계
            #  - notAfter= 날짜는 정상 인증서에도 나오므로 신호로 쓰지
            #    않는다. 날짜만 보여 준 인용은 수동 확인이 된다.
            #  - "expired"는 정상 출력의 다른 문맥에도 나올 수 있다.
            signals=(r"certificate has expired", r"Certificate will expire", r"expired"),
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
