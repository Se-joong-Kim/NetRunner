"""방식 A 규칙 기반 진단기(RuleDiagnoser) 테스트.

가짜 실험망은 실제 실험망(alpine: dig 9.20, busybox ping, curl 8.14)의
출력 형식을 그대로 흉내 낸다. 기본은 정상 상태이고, 테스트마다 장애
상태의 출력 몇 개만 바꾼다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netrunner import __main__ as cli
from netrunner.contracts import Limits, ProbeResult, RoundBrief
from netrunner.faults import FAULTS
from netrunner.rule_diagnoser import RuleDiagnoser
from netrunner.toolbox import Toolbox
from netrunner.tools import TOOLS
from netrunner.validate import LabInfo

ROOT = Path(__file__).resolve().parent.parent
LAB = LabInfo.load(ROOT / "config" / "lab.json")
WEB = "10.20.0.10"
SERVERS = {"dns": "10.20.0.53", "alt_dns": "10.20.0.54"}


def dig(server="dns", status="NOERROR", ip=WEB, ms=0):
    answer = f"\n;; ANSWER SECTION:\nshop.lab.\t\t0\tIN\tA\t{ip}\n" if ip else ""
    count = 1 if ip else 0
    return (
        f"\n; <<>> DiG 9.20.30 <<>> +time=2 +tries=1 @{SERVERS[server]} shop.lab A\n"
        ";; Got answer:\n"
        f";; ->>HEADER<<- opcode: QUERY, status: {status}, id: 15887\n"
        f";; flags: qr aa rd ra; QUERY: 1, ANSWER: {count}, AUTHORITY: 0, ADDITIONAL: 1\n"
        f"{answer}\n"
        f";; Query time: {ms} msec\n"
        f";; SERVER: {SERVERS[server]}#53({SERVERS[server]}) (UDP)\n"
    )


def dig_timeout(server="dns"):
    return (
        f"\n; <<>> DiG 9.20.30 <<>> +time=2 +tries=1 @{SERVERS[server]} shop.lab A\n"
        f";; communications error to {SERVERS[server]}#53: timed out\n"
        ";; no servers could be reached\n"
    )


def ping(host=WEB, sent=5, received=None, rtts=(0.07, 0.1, 0.2), size=56):
    received = sent if received is None else received
    loss = round(100 * (sent - received) / sent)
    lines = [f"PING {host} ({host}): {size} data bytes"]
    lines += [f"{size + 8} bytes from {host}: seq={i} ttl=63 time=0.1 ms" for i in range(received)]
    lines += [
        "",
        f"--- {host} ping statistics ---",
        f"{sent} packets transmitted, {received} packets received, {loss}% packet loss",
    ]
    if received:
        lines.append("round-trip min/avg/max = {:.3f}/{:.3f}/{:.3f} ms".format(*rtts))
    return "\n".join(lines) + "\n"


def curl(exit_code=0, code=200, lookup=0.0005, connect=0.0006, tls=0.0, log=""):
    return (
        f"exit_code={exit_code}\n\nhttp_code={code:03d}\n"
        f"time_namelookup={lookup:.6f}\ntime_connect={connect:.6f}\n"
        f"time_appconnect={tls:.6f}\ntime_starttransfer=0.001\ntime_total=0.002\n"
        f"{log}\n"
    )


def curl_error(exit_code, message, lookup=0.0005, connect=0.0, tls=0.0, code=0):
    return curl(
        exit_code, code, lookup, connect, tls, f"* {message}\ncurl: ({exit_code}) {message}"
    )


UNREACHABLE = curl_error(
    7, "Failed to connect to shop.lab port 80 after 3068 ms: Could not connect to server"
)
BLOCK_PAGE = curl(log="< HTTP/1.1 200 OK\n< Content-Length: 32")


class FakeLab:
    """정상 상태 출력을 기본으로 돌려주는 실험망 대역.

    출력은 키로 바꾼다: dig:dns, dig:alt_dns, ping, ping:big, curl,
    curl@<IP>(resolve_ip 지정), curl:<URL>(대상 밖 URL). 값이 목록이면
    호출마다 앞에서부터 하나씩 쓴다.
    """

    def __init__(self, url="http://shop.lab/", **outputs):
        self.url = url
        self.calls: list[tuple[str, dict]] = []
        self.outputs = {
            "dig:dns": dig("dns"),
            "dig:alt_dns": dig("alt_dns"),
            "ping": ping(),
            "ping:big": ping(sent=3, size=1472),
            "curl": curl(),
        }
        self.outputs.update({k.replace("__", ":"): v for k, v in outputs.items()})

    def key(self, tool, args):
        if tool == "dig":
            return f"dig:{args.get('server', 'dns')}"
        if tool == "ping":
            return "ping:big" if args.get("size", 56) > 1000 else "ping"
        if tool == "curl" and "resolve_ip" in args:
            return f"curl@{args['resolve_ip']}"
        if tool == "curl":
            return "curl" if args["url"] == self.url else f"curl:{args['url']}"
        return tool

    def run_probe(self, tool, args):
        self.calls.append((tool, dict(args)))
        out = self.outputs.get(self.key(tool, args), "")
        if isinstance(out, list):
            out = out.pop(0) if len(out) > 1 else out[0]
        return ProbeResult(True, out, 5)


def run(lab: FakeLab, limits: Limits | None = None, allowed=frozenset(TOOLS)):
    brief = RoundBrief("r1", lab.url, "사이트가 안 열려요", limits or Limits())
    tools = Toolbox(lab, LAB, brief.limits, allowed)
    diagnosis = RuleDiagnoser().diagnose(brief, tools)
    return diagnosis, tools


def labels(diagnosis) -> list[str]:
    return [f.label for f in diagnosis.findings]


def cited(diagnosis, tools) -> list[tuple[str, str]]:
    """각 진단이 인용한 검사의 (도구, 서버 또는 URL) 목록."""
    by_id = {f"probe:{p.probe_id}": p for p in tools.probes()}
    out = []
    for f in diagnosis.findings:
        for ev in f.evidence:
            p = by_id[ev.ref]
            out.append((p.tool, p.args.get("server") or p.args.get("resolve_ip") or ""))
    return out


# --- 등록


def test_run_registers_rules_as_method_a():
    args = cli.build_parser().parse_args(["run", "--diagnoser", "rules"])
    (name,) = args.diagnoser
    diagnoser = cli.DIAGNOSER_IMPLS[name](args)
    assert (diagnoser.method, diagnoser.model) == ("A", "rules")


@pytest.mark.parametrize("command", ["run", "match"])
def test_rules_requires_a_connected_probe_runner(command, tmp_path, capsys):
    assert (
        cli.main([command, "--impl", "mock", "--diagnoser", "rules", "--out", str(tmp_path)]) == 1
    )
    assert "검사 실행기가 연결되지 않았습니다" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


# --- 정상 상태와 검사 순서


def test_normal_lab_has_no_findings_and_runs_the_fixed_checks_in_order():
    lab = FakeLab()
    diagnosis, tools = run(lab)
    assert diagnosis.findings == []
    assert (diagnosis.method, diagnosis.model, diagnosis.round_id) == ("A", "rules", "r1")
    assert [(t, a.get("server") or a.get("size")) for t, a in lab.calls] == [
        ("dig", "dns"),
        ("dig", "alt_dns"),
        ("curl", None),
        ("ping", None),
        ("ping", 1472),
    ]
    assert lab.calls[3][1]["host"] == WEB  # 보조 DNS가 알려 준 주소로 경로를 본다
    assert not tools.rejected_calls()


def test_every_finding_uses_a_fault_list_label():
    known = {f.label for f in FAULTS.values()}
    lab = FakeLab(dig__dns=dig(status="NXDOMAIN", ip=None))
    diagnosis, _ = run(lab)
    assert diagnosis.findings and set(labels(diagnosis)) <= known


# --- DNS


def test_nxdomain_from_primary_dns_is_name_missing():
    lab = FakeLab(
        dig__dns=dig(status="NXDOMAIN", ip=None),
        curl=curl_error(6, "Could not resolve host: shop.lab", lookup=0.0),
    )
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["D1"].label]
    assert cited(diagnosis, tools) == [("dig", "dns")]


def test_primary_answer_differing_from_alt_with_dead_address_is_wrong_address():
    lab = FakeLab(
        dig__dns=dig(ip="10.20.0.99"), curl=UNREACHABLE, **{"curl@10.20.0.99": UNREACHABLE}
    )
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["D2"].label]
    assert cited(diagnosis, tools) == [("dig", "dns")]
    assert ("curl", {"url": "http://shop.lab/", "resolve_ip": "10.20.0.99"}) in lab.calls


def test_primary_answer_pointing_at_a_serving_page_is_dns_tampering():
    lab = FakeLab(
        dig__dns=dig(ip="10.20.0.12"), curl=BLOCK_PAGE, **{"curl@10.20.0.12": BLOCK_PAGE}
    )
    diagnosis, _ = run(lab)
    assert labels(diagnosis) == [FAULTS["B2"].label]


def test_tampering_check_uses_http_even_for_an_https_target():
    lab = FakeLab(
        "https://shop.lab/",
        dig__dns=dig(ip="10.20.0.12"),
        curl=curl_error(
            7, "Failed to connect to shop.lab port 443 after 1 ms: Connection refused"
        ),
        **{"curl@10.20.0.12": BLOCK_PAGE},
    )
    diagnosis, _ = run(lab)
    # 막힌 DNS 뒤의 연결 거부는 가짜 주소 때문이라 TCP 원인으로 따로 내지 않는다.
    assert labels(diagnosis) == [FAULTS["B2"].label]
    assert ("curl", {"url": "http://shop.lab/", "resolve_ip": "10.20.0.12"}) in lab.calls


def test_primary_timeout_twice_while_alt_answers_is_server_unresponsive():
    lab = FakeLab(
        dig__dns=dig_timeout("dns"),
        curl=curl_error(28, "Resolving timed out after 5001 milliseconds", lookup=0.0),
    )
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["D3"].label]
    assert [a.get("server") for t, a in lab.calls if t == "dig"] == ["dns", "alt_dns", "dns"]
    assert cited(diagnosis, tools) == [("dig", "dns")]


def test_primary_timeout_that_recovers_on_retry_is_not_a_dns_fault():
    lab = FakeLab(dig__dns=[dig_timeout("dns"), dig("dns")])
    diagnosis, _ = run(lab)
    assert diagnosis.findings == []


def test_slow_primary_dns_against_fast_alt_is_dns_delay():
    lab = FakeLab(dig__dns=dig(ms=499), curl=curl(lookup=0.5009, connect=0.5011))
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["D4"].label]
    assert cited(diagnosis, tools) == [("dig", "dns")]


def test_primary_timeout_but_slow_successful_lookup_is_dns_delay():
    lab = FakeLab(dig__dns=dig_timeout("dns"), curl=curl(lookup=4.502, connect=4.5022))
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["D4"].label]
    assert cited(diagnosis, tools) == [("curl", "")]


def capture(*lines: str) -> str:
    return "".join(f"#{i} {line}\n" for i, line in enumerate(lines, 1))


QUERY = "11:37:16.809191 IP 10.21.0.2.45483 > 10.20.0.53.53: 43002+ [1au] A? shop.lab. (49)"
RETRY = "11:37:18.810652 IP 10.21.0.2.51874 > 10.20.0.53.53: 43002+ [1au] A? shop.lab. (49)"
LATE_REPLY = "11:37:19.309432 IP 10.20.0.53.53 > 10.21.0.2.45483: 43002* 1/0/1 A 10.20.0.10 (53)"
RESOLVE_TIMEOUT = curl_error(28, "Resolving timed out after 5001 milliseconds", lookup=0.0)


def test_late_reply_seen_only_in_capture_is_dns_delay():
    # 지연이 클라이언트의 재질의 간격보다 길면 dig와 curl 모두 응답을 받지 못한다.
    # 캡처에는 늦게 도착한 응답이 남는다.
    lab = FakeLab(
        dig__dns=dig_timeout("dns"),
        curl=RESOLVE_TIMEOUT,
        capture=capture(QUERY, RETRY, LATE_REPLY),
    )
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["D4"].label]
    (probe,) = [p for p in tools.probes() if p.tool == "capture"]
    assert [ev.ref for f in diagnosis.findings for ev in f.evidence] == [
        f"pcap:{probe.probe_id}#3"
    ]
    assert "2.5초" in diagnosis.findings[0].evidence[0].claim


def test_capture_without_any_reply_keeps_server_unresponsive():
    lab = FakeLab(dig__dns=dig_timeout("dns"), curl=RESOLVE_TIMEOUT, capture=capture(QUERY, RETRY))
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["D3"].label]
    assert "capture" in [t for t, _ in lab.calls]
    assert cited(diagnosis, tools) == [("dig", "dns")]


def test_both_dns_servers_slow_is_not_dns_delay():
    lab = FakeLab(dig__dns=dig(ms=210), dig__alt_dns=dig("alt_dns", ms=205))
    diagnosis, _ = run(lab)
    assert FAULTS["D4"].label not in labels(diagnosis)


@pytest.mark.parametrize("alt", [dig("alt_dns"), dig_timeout("alt_dns")])
def test_quick_lookup_after_dns_timeouts_is_not_a_dns_fault(alt):
    # 손실이 큰 경로에서는 dig가 시간 초과해도 접속 요청의 이름 확인은 빨리 끝날 수 있다.
    lab = FakeLab(dig__dns=dig_timeout("dns"), dig__alt_dns=alt)
    diagnosis, _ = run(lab)
    assert diagnosis.findings == []


# --- 경로


def test_packet_loss_with_normal_rtt_is_path_loss():
    lab = FakeLab(
        dig__dns=dig_timeout("dns"),
        dig__alt_dns=dig_timeout("alt_dns"),
        ping=ping(sent=10, received=8),
        ping__big=ping(sent=3, received=2, size=1472),
    )
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["N2"].label]
    assert cited(diagnosis, tools) == [("ping", "")]


def test_stable_high_rtt_is_path_delay_not_dns_delay():
    lab = FakeLab(
        dig__dns=dig(ms=330),
        dig__alt_dns=dig("alt_dns", ms=199),
        ping=ping(rtts=(200.151, 200.183, 200.225)),
        ping__big=ping(sent=3, size=1472, rtts=(200.175, 200.290, 200.424)),
        curl=curl(lookup=0.2007, connect=0.4011),
    )
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["N1"].label]
    assert cited(diagnosis, tools) == [("ping", "")]


def test_jittery_delay_with_matching_averages_is_path_delay():
    lab = FakeLab(
        ping=ping(rtts=(52.825, 107.757, 148.021)),
        ping__big=ping(sent=3, size=1472, rtts=(75.989, 105.540, 128.350)),
    )
    diagnosis, _ = run(lab)
    assert labels(diagnosis) == [FAULTS["N1"].label]


@pytest.mark.parametrize(
    "small, big",
    [
        ((292.777, 403.402, 531.950), (74.307, 176.537, 278.762)),
        ((5.519, 16.039, 37.079), (17.549, 23.087, 28.739)),
    ],
)
def test_delay_that_shifts_between_pings_is_queuing_delay(small, big):
    lab = FakeLab(ping=ping(rtts=small), ping__big=ping(sent=3, size=1472, rtts=big))
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["N4"].label]
    assert cited(diagnosis, tools) == [("ping", "")]


def test_big_packets_lost_while_small_pass_is_mtu_blackhole():
    lab = FakeLab(ping__big=ping(sent=3, received=0, size=1472))
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["N3"].label]
    assert [p.args.get("size") for p in tools.probes() if p.probe_id == cite_id(diagnosis)] == [
        1472
    ]


def cite_id(diagnosis) -> str:
    (finding,) = diagnosis.findings
    (ev,) = finding.evidence
    return ev.ref.removeprefix("probe:")


# --- TCP


REFUSED = curl_error(
    7,
    "Failed to connect to shop.lab port 80 after 0 ms: Could not connect to server",
).replace(
    "* Failed",
    "* connect to 10.20.0.10 port 80 from 10.21.0.2 port 39218 failed: "
    "Connection refused\n* Failed",
)
CONNECT_TIMEOUT = curl_error(28, "Connection timed out after 5003 milliseconds")


def test_refused_connection_is_tcp_refused():
    diagnosis, tools = run(FakeLab(curl=REFUSED))
    assert labels(diagnosis) == [FAULTS["T2"].label]
    assert cited(diagnosis, tools) == [("curl", "")]


def test_connect_timeout_on_a_healthy_path_is_tcp_drop():
    diagnosis, tools = run(FakeLab(curl=CONNECT_TIMEOUT))
    assert labels(diagnosis) == [FAULTS["T1"].label]
    assert cited(diagnosis, tools) == [("curl", "")]


def test_connect_timeout_on_a_slow_path_is_only_path_delay():
    lab = FakeLab(
        curl=CONNECT_TIMEOUT,
        ping=ping(rtts=(1990.0, 2000.0, 2010.0)),
        ping__big=ping(sent=3, size=1472, rtts=(1995.0, 2001.0, 2008.0)),
    )
    diagnosis, _ = run(lab)
    assert labels(diagnosis) == [FAULTS["N1"].label]


def test_dns_timeouts_on_a_very_slow_path_are_only_path_delay():
    # 경로 지연이 dig 대기 시간(2초)에 가까우면 두 DNS 모두 시간 초과하고,
    # 접속 요청의 이름 확인은 느리게 성공한다. DNS 지연이 아니다.
    lab = FakeLab(
        dig__dns=dig_timeout("dns"),
        dig__alt_dns=dig_timeout("alt_dns"),
        curl=curl(lookup=2.001, connect=4.002),
        ping=ping(rtts=(1990.0, 2000.0, 2010.0)),
        ping__big=ping(sent=3, size=1472, rtts=(1995.0, 2001.0, 2008.0)),
    )
    diagnosis, _ = run(lab)
    assert labels(diagnosis) == [FAULTS["N1"].label]


# --- TLS와 차단


@pytest.mark.parametrize(
    "exit_code, message, fault",
    [
        (60, "SSL certificate problem: certificate has expired", "S1"),
        (
            60,
            "SSL: no alternative certificate subject name matches target hostname 'shop.lab'",
            "S2",
        ),
        (35, "Recv failure: Connection reset by peer", "B1"),
    ],
)
def test_tls_handshake_failures(exit_code, message, fault):
    lab = FakeLab("https://shop.lab/", curl=curl_error(exit_code, message, connect=0.0006))
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS[fault].label]
    assert cited(diagnosis, tools) == [("curl", "")]


# --- HTTP


def test_server_error_status_is_http_server_error():
    diagnosis, tools = run(FakeLab(curl=curl(code=502)))
    assert labels(diagnosis) == [FAULTS["H1"].label]
    assert cited(diagnosis, tools) == [("curl", "")]


def redirect(to: str) -> str:
    return curl(code=301, log=f"< HTTP/1.1 301 Moved Permanently\n< Location: {to}")


def test_redirect_to_itself_is_redirect_loop():
    lab = FakeLab(curl=redirect("http://shop.lab/"))
    diagnosis, tools = run(lab)
    assert labels(diagnosis) == [FAULTS["H2"].label]
    assert cited(diagnosis, tools) == [("curl", "")]
    assert [t for t, _ in lab.calls].count("curl") == 1


def test_redirect_cycle_through_another_url_is_redirect_loop():
    lab = FakeLab(curl=redirect("/a"), **{"curl:http://shop.lab/a": redirect("http://shop.lab/")})
    diagnosis, _ = run(lab)
    assert labels(diagnosis) == [FAULTS["H2"].label]
    assert ("curl", {"url": "http://shop.lab/a"}) in lab.calls


def test_redirect_to_a_working_page_is_not_a_fault():
    lab = FakeLab(curl=redirect("https://shop.lab/"), **{"curl:https://shop.lab/": curl()})
    diagnosis, _ = run(lab)
    assert diagnosis.findings == []


# --- 한도와 창구


def test_stops_at_the_tool_call_limit_without_rejected_calls():
    lab = FakeLab(dig__dns=dig(status="NXDOMAIN", ip=None))
    diagnosis, tools = run(lab, Limits(max_tool_calls=2))
    assert len(tools.probes()) == 2
    assert not tools.rejected_calls()
    assert labels(diagnosis) == [FAULTS["D1"].label]


def test_skips_tools_the_round_does_not_allow():
    lab = FakeLab(curl=REFUSED)
    diagnosis, tools = run(lab, allowed=frozenset(TOOLS) - {"ping"})
    assert "ping" not in [t for t, _ in lab.calls]
    assert not tools.rejected_calls()
    assert labels(diagnosis) == [FAULTS["T2"].label]


class FailingPingLab(FakeLab):
    def run_probe(self, tool, args):
        if tool == "ping":
            self.calls.append((tool, dict(args)))
            return ProbeResult(False, error="검사 실행 실패")
        return super().run_probe(tool, args)


def test_a_probe_the_lab_could_not_run_is_treated_as_missing():
    diagnosis, _ = run(FailingPingLab(curl=CONNECT_TIMEOUT))
    # 경로를 확인하지 못했으므로 연결 시간 초과를 TCP 차단으로 단정하지 않는다.
    assert diagnosis.findings == []
