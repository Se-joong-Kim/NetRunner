"""Docker Compose로 첫 통합 장애를 재현하는 실험망."""

from __future__ import annotations

import ipaddress
import math
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

from .child_env import child_environment
from .contracts import FaultSpec, LabResult, ProbeResult, Scenario, read_json
from .records import ensure_private_dir
from .tools import check_args
from .validate import LabInfo

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "compose.yaml"
# Windows Docker Desktop에서는 internal 네트워크가 라우터 경유 통신을 막는다(#47).
# 덮어쓰기 파일은 그곳에서만 internal을 끈다. 외부 차단은 lab/start.sh의 기본 경로 삭제에 맡긴다.
WINDOWS_OVERRIDE = ROOT / "compose.windows.yaml"
IS_WINDOWS = os.name == "nt"


def _ping_rtt_samples(output: str) -> list[float]:
    """Ping 출력의 왕복 시간(ms) 표본을 뽑는다."""
    return [float(value) for value in re.findall(r"time[=<](\d+(?:\.\d+)?)\s*ms", output)]


def compose_files(windows: bool) -> list[Path]:
    """실험망 compose 파일 목록. Windows에서는 덮어쓰기 파일을 뒤에 붙인다."""
    return [COMPOSE_FILE, WINDOWS_OVERRIDE] if windows else [COMPOSE_FILE]


class DockerLab:
    """Docker Compose 실험망에서 단일 장애를 주입하고 되돌리는 Lab 구현."""

    # shortcut: 한 실험망은 순차 실행만 지원, 동시 라운드 지원 시 설정과 캡처를 격리한다.
    def __init__(self, config: str | Path = ROOT / "config/lab.json"):
        self.config = read_json(config)
        self.hosts = self.config["hosts"]
        self.lab_info = LabInfo.load(config)

    def run_probe(self, tool: str, args: dict) -> ProbeResult:
        """클라이언트에서 허용된 검사 도구를 실행한다."""
        if tool not in {"dig", "curl", "capture", "openssl", "ping", "tracepath"}:
            return ProbeResult(False, error="지원하지 않는 검사 도구")
        error = check_args(tool, args, self.lab_info)
        if error:
            return ProbeResult(False, error=error)
        if tool == "capture":
            result, path = self._capture(args["url"])
            if result["exit_code"] != 0:
                return ProbeResult(False, error="캡처 실행 실패")
            return ProbeResult(
                True, result["output"], result["elapsed_ms"], result["packets"], path
            )
        if tool == "ping":
            size = ["-s", str(args["size"])] if "size" in args else []
            result = self._run(
                "client", "ping", "-c", str(args.get("count", 4)), "-W", "3", *size, args["host"]
            )
            ok = result["exit_code"] in (0, 1)
            output = result["stdout"] + result["stderr"]
        elif tool == "tracepath":
            result = self._run("client", "tracepath", "-n", args["host"])
            ok = result["exit_code"] in (0, 1)
            output = result["stdout"] + result["stderr"]
        elif tool == "openssl":
            script = (
                "out=$(mktemp) || exit 1; trap 'rm -f \"$out\"' EXIT; "
                'openssl s_client -connect "$1:$2" -servername "$3" -verify 5 -showcerts '
                '</dev/null >"$out" 2>&1 || true; '
                'openssl x509 -in "$out" -noout -subject -issuer -dates 2>&1; '
                'grep "Verify return code" "$out" || cat "$out"'
            )
            result = self._run(
                "client",
                "sh",
                "-c",
                script,
                "sh",
                args["host"],
                str(args.get("port", 443)),
                args.get("servername", args["host"]),
            )
            output = result["stdout"] + result["stderr"]
            ok = result["exit_code"] == 0 and (
                "notAfter=" in output or "Verify return code" in output
            )
        elif tool == "dig":
            server = args.get("server", "dns")
            server = self.hosts.get(server, server)
            result = self._run(
                "client", "dig", "+time=2", "+tries=1", "@" + server, args["domain"], "A"
            )
            ok = result["exit_code"] in (0, 9)
            output = result["stdout"] + result["stderr"]
        else:
            url = args["url"]
            extra = []
            if "resolve_ip" in args:
                parsed = urlsplit(url)
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                extra = [
                    "--resolve",
                    f"{parsed.hostname}:{port}:{args['resolve_ip']}",
                ]
            fields = (
                "http_code",
                "time_namelookup",
                "time_connect",
                "time_appconnect",
                "time_starttransfer",
                "time_total",
            )
            format_string = "\n" + "\n".join(f"{field}=%{{{field}}}" for field in fields)
            result = self._run(
                "client",
                "curl",
                "--noproxy",
                "*",
                "--max-time",
                "5",
                "-sS",
                "-v",
                "-o",
                "/dev/null",
                "-w",
                format_string,
                *extra,
                url,
            )
            ok = result["exit_code"] is not None and "http_code=" in result["stdout"]
            output = f"exit_code={result['exit_code']}\n{result['stdout']}\n{result['stderr']}"
        if not ok:
            return ProbeResult(False, error="검사 실행 실패")
        return ProbeResult(True, output, result["elapsed_ms"])

    def _run(self, service: str, *args: str, timeout: int = 20) -> dict:
        files = [arg for f in compose_files(IS_WINDOWS) for arg in ("-f", str(f))]
        cmd = ["docker", "compose", *files, "exec", "-T", service, *args]
        start = time.monotonic()
        try:
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=child_environment(os.environ),
            )
            return {
                "command": cmd,
                "exit_code": p.returncode,
                "stdout": p.stdout,
                "stderr": p.stderr,
                "elapsed_ms": round((time.monotonic() - start) * 1000),
            }
        except subprocess.TimeoutExpired as e:
            outputs = {
                key: value.decode(errors="replace") if isinstance(value, bytes) else value or ""
                for key, value in (("stdout", e.stdout), ("stderr", e.stderr))
            }
            return {
                "command": cmd,
                "exit_code": None,
                **outputs,
                "error": str(e),
                "elapsed_ms": round((time.monotonic() - start) * 1000),
            }
        except OSError as e:
            return {
                "command": cmd,
                "exit_code": None,
                "stdout": "",
                "stderr": str(e),
                "elapsed_ms": round((time.monotonic() - start) * 1000),
            }

    def _target(self, url: str) -> bool:
        try:
            u = urlsplit(url)
            return (
                u.scheme in ("http", "https")
                and u.hostname in self.config["domains"]
                and (u.port is None or u.port == (443 if u.scheme == "https" else 80))
                and not u.username
                and not u.password
            )
        except ValueError:
            return False

    def _curl(self, url: str, *extra: str) -> dict:
        return self._run(
            "client",
            "curl",
            "--noproxy",
            "*",
            "--max-time",
            "5",
            "-sS",
            "-v",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code} %{time_namelookup} %{time_connect} %{time_total}",
            *extra,
            url,
        )

    def _dns(self, server: str, domain: str, timeout: int = 2) -> dict:
        return self._run(
            "client", "dig", f"+time={timeout}", "+tries=1", "+short", "@" + server, domain, "A"
        )

    def _dns_timed(self, server: str, domain: str) -> dict:
        return self._run(
            "client", "dig", "+time=5", "+tries=1", "+stats", "@" + server, domain, "A"
        )

    def _start_load(self) -> dict:
        script = """\
if [ -e /tmp/netrunner-load.pid ]; then exit 1; fi
setsid sh /load.sh >/tmp/netrunner-load.log 2>&1 &
i=0
while [ "$i" -lt 10 ]; do
    if [ -s /tmp/netrunner-load.pid ]; then
        pid=$(cat /tmp/netrunner-load.pid)
        if kill -0 "$pid" 2>/dev/null; then exit 0; fi
    fi
    sleep 0.1
    i=$((i + 1))
done
exit 1
"""
        return self._run(
            "client",
            "sh",
            "-c",
            script,
        )

    def _stop_load(self) -> dict:
        return self._run("client", "sh", "/stop-load.sh", timeout=10)

    def _set_dns(self, domain: str | None = None, ip: str | None = None) -> dict:
        hosts = [(ip if d == domain else self.config["web_ip"], d) for d in self.config["domains"]]
        args = [item for address, name in hosts if address is not None for item in (address, name)]
        script = (
            'set -e; : > /tmp/dns-hosts.new; while [ "$#" -gt 0 ]; do '
            'printf "%s %s\\n" "$1" "$2" >> /tmp/dns-hosts.new; shift 2; done; '
            "mv /tmp/dns-hosts.new /tmp/dns-hosts; "
            'su-exec dnsmasq kill -HUP "$(cat /tmp/dnsmasq.pid)"'
        )
        result = self._run("dns", "sh", "-c", script, "sh", *args)
        if result["exit_code"] == 0:
            for _ in range(3):
                checks = [self._dns(self.hosts["dns"], d) for _, d in hosts]
                result["checks"] = checks
                if all(
                    check["exit_code"] == 0 and check["stdout"].strip() == (expected or "")
                    for (expected, _), check in zip(hosts, checks, strict=True)
                ):
                    break
                time.sleep(0.1)
            else:
                result["exit_code"] = 1
                result["stderr"] += "DNS 설정 적용 확인 실패"
        return result

    def health_check(self, target_url: str) -> LabResult:
        """클라이언트에서 대상 URL이 HTTP 200으로 응답하는지 확인한다."""
        if not self._target(target_url):
            return LabResult(False, "허용되지 않는 대상 URL")
        r = self._curl(target_url)
        ok = r["exit_code"] == 0 and r["stdout"].split()[:1] == ["200"]
        return LabResult(ok, "정상 HTTP 200" if ok else "정상 접속 실패", {"curl": r})

    def _set_cert(self, cert: str) -> dict:
        domain = "news.lab" if cert == "shop" else "shop.lab"
        r = self._run(
            "web",
            "sh",
            "-c",
            'cp "$1" /tmp/server.crt.new && mv /tmp/server.crt.new /tmp/server.crt && '
            'su-exec nobody kill -HUP "$(cat /tmp/nginx.pid)"',
            "sh",
            f"/certs/{cert}.crt",
        )
        if r["exit_code"] == 0:
            for _ in range(20):
                check = self._curl(
                    f"https://{domain}/", "--resolve", f"{domain}:443:{self.config['web_ip']}"
                )
                r["check"] = check
                if (
                    (
                        cert == "expired"
                        and check["exit_code"] == 60
                        and "certificate has expired" in check["stderr"].lower()
                    )
                    or (cert == "valid" and check["exit_code"] == 0)
                    or (
                        cert in ("shop", "news")
                        and check["exit_code"] == 60
                        and "subject name" in check["stderr"].lower()
                    )
                ):
                    break
                time.sleep(0.1)
            else:
                r["exit_code"] = 1
                r["stderr"] += " 인증서 적용 확인 실패"
        return r

    def _set_redirect(self, enabled: bool) -> dict:
        text = "return 301 $scheme://$host$request_uri;" if enabled else ""
        r = self._run(
            "web",
            "sh",
            "-c",
            'printf "%s\\n" "$1" > /tmp/redirect.conf && '
            'su-exec nobody kill -HUP "$(cat /tmp/nginx.pid)"',
            "sh",
            text,
        )
        if r["exit_code"] == 0:
            for _ in range(20):
                check = self._curl(
                    "http://shop.lab/", "--resolve", f"shop.lab:80:{self.config['web_ip']}"
                )
                r["check"] = check
                if check["exit_code"] == 0 and check["stdout"].split()[:1] == (
                    ["301"] if enabled else ["200"]
                ):
                    break
                time.sleep(0.1)
            else:
                r["exit_code"] = 1
                r["stderr"] += " 리다이렉트 설정 확인 실패"
        return r

    def inject(self, fault: FaultSpec) -> LabResult:
        """DNS 응답 또는 웹 서버 방화벽을 바꿔 장애를 심는다."""
        if (
            fault.id == "D1"
            and set(fault.params) == {"domain"}
            and fault.params["domain"] in self.config["domains"]
        ):
            r = self._set_dns(fault.params["domain"])
        elif (
            fault.id == "N1"
            and set(fault.params) in ({"delay_ms"}, {"delay_ms", "jitter_ms"})
            and type(fault.params["delay_ms"]) is int
            and 20 <= fault.params["delay_ms"] <= 2000
            and type(fault.params.get("jitter_ms", 0)) is int
            and 0 <= fault.params.get("jitter_ms", 0) <= 500
        ):
            delay = str(fault.params["delay_ms"]) + "ms"
            jitter = fault.params.get("jitter_ms", 0)
            args = [str(jitter) + "ms"] if jitter else []
            r = self._run(
                "client",
                "tc",
                "qdisc",
                "add",
                "dev",
                "eth0",
                "root",
                "handle",
                "10:",
                "netem",
                "delay",
                delay,
                *args,
            )
        elif (
            fault.id == "N2"
            and set(fault.params) == {"loss_pct"}
            and type(fault.params["loss_pct"]) is int
            and 1 <= fault.params["loss_pct"] <= 30
        ):
            r = self._run(
                "client",
                "tc",
                "qdisc",
                "add",
                "dev",
                "eth0",
                "root",
                "handle",
                "10:",
                "netem",
                "loss",
                f"{fault.params['loss_pct']}%",
            )
        elif (
            fault.id == "N3"
            and set(fault.params) == {"min_size"}
            and type(fault.params["min_size"]) is int
            and 576 <= fault.params["min_size"] <= 1500
        ):
            r = self._run(
                "router",
                "iptables",
                "-A",
                "NETRUNNER",
                "-m",
                "length",
                "--length",
                f"{fault.params['min_size']}:65535",
                "-j",
                "DROP",
            )
            if r["exit_code"] == 0:
                r = self._run(
                    "router",
                    "iptables",
                    "-A",
                    "NETRUNNER",
                    "-p",
                    "icmp",
                    "--icmp-type",
                    "fragmentation-needed",
                    "-j",
                    "DROP",
                )
        elif (
            fault.id == "D4"
            and set(fault.params) in ({"delay_ms"}, {"domain", "delay_ms"})
            and type(fault.params["delay_ms"]) is int
            and 100 <= fault.params["delay_ms"] <= 3000
            and ("domain" not in fault.params or fault.params["domain"] in self.config["domains"])
        ):
            r = self._run(
                "dns",
                "tc",
                "qdisc",
                "add",
                "dev",
                "eth0",
                "root",
                "handle",
                "20:",
                "netem",
                "delay",
                f"{fault.params['delay_ms']}ms",
            )
        elif (
            fault.id == "N4"
            and set(fault.params) == {"rate_kbit"}
            and type(fault.params["rate_kbit"]) is int
            and 256 <= fault.params["rate_kbit"] <= 10000
        ):
            script = """\
set -e
dev=$(ip route get "$1" | awk '$2 == "dev" {print $3}')
case "$dev" in eth[0-9]*) ;; *) exit 1;; esac
tc qdisc add dev "$dev" root handle 30: tbf rate "${2}kbit" \
    burst 32k latency 1000ms
"""
            r = self._run(
                "router",
                "sh",
                "-c",
                script,
                "sh",
                self.hosts["client"],
                str(fault.params["rate_kbit"]),
            )
            if r["exit_code"] == 0:
                load = self._start_load()
                r = {**r, "load": load, "exit_code": load["exit_code"]}
        elif (
            fault.id == "B1"
            and set(fault.params) == {"domain"}
            and fault.params["domain"] in self.config["domains"]
        ):
            # shortcut: split SNI is missed, use stream reassembly if tests require it.
            r = self._run(
                "router",
                "iptables",
                "-A",
                "NETRUNNER",
                "-p",
                "tcp",
                "--dport",
                "443",
                "-m",
                "string",
                "--string",
                fault.params["domain"],
                "--algo",
                "bm",
                "--icase",
                "-j",
                "REJECT",
                "--reject-with",
                "tcp-reset",
            )
        elif (
            fault.id == "B2"
            and set(fault.params) == {"domain", "block_ip"}
            and fault.params["domain"] in self.config["domains"]
            and fault.params["block_ip"] == self.hosts.get("block_page")
        ):
            r = self._set_dns(fault.params["domain"], fault.params["block_ip"])
        elif fault.id == "H1" and not fault.params:
            r = self._run(
                "backend",
                "sh",
                "-c",
                "su-exec nobody killall httpd; while pidof httpd >/dev/null; do sleep 0.1; done",
            )
        elif fault.id == "H2" and not fault.params:
            r = self._set_redirect(True)
        elif fault.id == "S1" and not fault.params:
            r = self._set_cert("expired")
        elif (
            fault.id == "S2"
            and set(fault.params) == {"cert_domain"}
            and fault.params["cert_domain"] in ("shop.lab", "news.lab")
        ):
            r = self._set_cert(fault.params["cert_domain"].split(".")[0])
        elif fault.id == "D3" and not fault.params:
            r = self._run(
                "router",
                "iptables",
                "-A",
                "NETRUNNER",
                "-p",
                "udp",
                "-d",
                self.hosts["dns"],
                "--dport",
                "53",
                "-j",
                "DROP",
            )
        elif fault.id == "D2" and set(fault.params) == {"domain", "ip"}:
            domain, ip = fault.params["domain"], fault.params["ip"]
            try:
                address = ipaddress.ip_address(ip)
                allowed = (
                    isinstance(ip, str)
                    and isinstance(domain, str)
                    and domain in self.config["domains"]
                    and address in ipaddress.ip_network(self.config["subnet"])
                    and str(address) != self.config["web_ip"]
                )
            except (ValueError, TypeError):
                allowed = False
            if not allowed:
                return LabResult(False, "허용되지 않는 D2 도메인 또는 IP")
            r = self._set_dns(domain, str(address))
        elif (
            fault.id == "T1" and fault.params == {"port": 80} and type(fault.params["port"]) is int
        ):
            r = self._run(
                "web", "iptables", "-A", "NETRUNNER", "-p", "tcp", "--dport", "80", "-j", "DROP"
            )
        elif (
            fault.id == "T2" and fault.params == {"port": 80} and type(fault.params["port"]) is int
        ):
            r = self._run(
                "web",
                "iptables",
                "-A",
                "NETRUNNER",
                "-p",
                "tcp",
                "--dport",
                "80",
                "-j",
                "REJECT",
                "--reject-with",
                "tcp-reset",
            )
        else:
            return LabResult(False, "지원하지 않는 장애 또는 매개변수")
        return LabResult(r["exit_code"] == 0, f"{fault.id} 주입", {"command": r})

    def _capture(self, url: str) -> tuple[dict, str]:
        out = ensure_private_dir(Path.home() / "netrunner-private" / "captures")
        name = f"lab-{uuid.uuid4().hex}.pcap"
        path = "/tmp/" + name
        local = out / name
        try:
            result = self._run(
                "client",
                "sh",
                "-c",
                'setsid sh /capture.sh "$@" & wait $!',
                "sh",
                path,
                url,
                timeout=15,
            )
        finally:
            stopped = self._run("client", "sh", "/stop-capture.sh", timeout=10)
        result["cleanup"] = stopped
        if stopped["exit_code"] != 0:
            result["exit_code"] = 1
        if result["exit_code"] == 0:
            files = [arg for f in compose_files(IS_WINDOWS) for arg in ("-f", str(f))]
            cmd = ["docker", "compose", *files, "exec", "-T", "client", "cat", path]
            created = False
            try:
                fd = os.open(
                    local, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
                )
                created = True
                with os.fdopen(fd, "wb") as fp:
                    copied = subprocess.run(
                        cmd,
                        stdout=fp,
                        stderr=subprocess.PIPE,
                        timeout=15,
                        env=child_environment(os.environ),
                    )
                if copied.returncode != 0:
                    raise OSError("캡처 복사 실패")
            except (OSError, subprocess.TimeoutExpired) as e:
                if created:
                    local.unlink(missing_ok=True)
                result["exit_code"] = 1
                result["stderr"] += f" 캡처 저장 실패: {e}"
        removed = self._run("client", "rm", "-f", path)
        if removed["exit_code"] != 0:
            result["exit_code"] = 1
            result["stderr"] += " 컨테이너 캡처 정리 실패"
        lines = result["stdout"].splitlines()
        numbers = [
            int(match.group(1)) for line in lines if (match := re.match(r"^\s*(\d+)\s", line))
        ]
        result["output"] = re.sub(r"(?m)^\s*(\d+)\s+", r"#\1 ", result["stdout"])
        result["packets"] = max(numbers, default=0)
        result["file"] = str(local)
        return result, str(local)

    def verify_symptom(self, scenario: Scenario) -> LabResult:
        """DNS 답, 접속 결과, 패킷 캡처로 장애별 증상을 확인한다."""
        if not self._target(scenario.target_url) or len(scenario.faults) != 1:
            return LabResult(False, "첫 통합은 허용된 URL의 단일 장애만 지원")
        fault = scenario.faults[0]
        if fault.id not in (
            "D1",
            "D2",
            "D3",
            "D4",
            "N1",
            "N2",
            "N3",
            "N4",
            "S1",
            "S2",
            "H1",
            "H2",
            "T1",
            "T2",
            "B1",
            "B2",
        ):
            return LabResult(False, "지원하지 않는 장애")
        domain = scenario.domain
        dns_timeout = 5 if fault.id == "D4" else 2
        primary = self._dns(self.hosts["dns"], domain, dns_timeout)
        alternate = self._dns(self.hosts["alt_dns"], domain, dns_timeout)
        curl = self._curl(scenario.target_url)
        observations = {
            "dns": primary,
            "alt_dns": alternate,
            "curl": curl,
        }
        dns_ok = primary["exit_code"] == alternate["exit_code"] == 0
        alt_ok = alternate["stdout"].strip() == self.config["web_ip"]
        if fault.id == "D1":
            full_dns = self._run(
                "client", "dig", "+time=2", "+tries=1", "@" + self.hosts["dns"], domain, "A"
            )
            observations["full_dns"] = full_dns
            ok = (
                dns_ok
                and alt_ok
                and full_dns["exit_code"] == 0
                and "status: NXDOMAIN" in full_dns["stdout"]
                and curl["exit_code"] == 6
            )
        elif fault.id == "N1":
            ping = self._run("client", "ping", "-c", "4", "-W", "3", self.config["web_ip"])
            observations["ping"] = ping
            samples = _ping_rtt_samples(ping["stdout"])
            threshold = fault.params["delay_ms"] / 2
            ok = (
                dns_ok
                and alt_ok
                and ping["exit_code"] == 0
                and len(samples) >= 4
                and sum(samples) / len(samples) >= threshold
            )
        elif fault.id == "D4":
            timed_dns = self._dns_timed(self.hosts["dns"], domain)
            alternate_timed_dns = self._dns_timed(self.hosts["alt_dns"], domain)
            observations.update(timed_dns=timed_dns, alternate_timed_dns=alternate_timed_dns)
            match = re.search(r"Query time: (\d+) msec", timed_dns["stdout"])
            alternate_match = re.search(r"Query time: (\d+) msec", alternate_timed_dns["stdout"])
            delay = fault.params["delay_ms"]
            ok = (
                dns_ok
                and alt_ok
                and self.config["web_ip"] in timed_dns["stdout"]
                and match is not None
                and int(match.group(1)) >= delay * 0.75
                and alternate_match is not None
                and int(alternate_match.group(1)) < delay * 0.5
            )
        elif fault.id == "S1":
            ok = curl["exit_code"] == 60 and "certificate has expired" in curl["stderr"].lower()
        elif fault.id == "S2":
            ok = curl["exit_code"] == 60 and "subject name" in curl["stderr"].lower()
        elif fault.id == "H1":
            ok = curl["exit_code"] == 0 and curl["stdout"].split()[:1] == ["502"]
        elif fault.id == "H2":
            redirects = self._curl(scenario.target_url, "--location", "--max-redirs", "3")
            observations["redirects"] = redirects
            ok = redirects["exit_code"] == 47 and curl["stdout"].split()[:1] == ["301"]
        elif fault.id == "N3":
            small = self._run("client", "ping", "-c", "2", "-W", "3", self.config["web_ip"])
            large = self._run(
                "client",
                "ping",
                "-c",
                "2",
                "-W",
                "3",
                "-s",
                str(fault.params["min_size"] - 28),
                self.config["web_ip"],
            )
            observations.update(small_ping=small, large_ping=large)
            large_url = f"http://{domain}/large"
            large_transfer = self._curl(large_url)
            observations["large_transfer"] = large_transfer
            ok = (
                small["exit_code"] == 0
                and large["exit_code"] == 1
                and "100% packet loss" in large["stdout"]
                and curl["exit_code"] == 0
                and curl["stdout"].split()[:1] == ["200"]
                and large_transfer["exit_code"] == 28
            )
        elif fault.id == "N2":
            samples = []
            # shortcut: 300 probes can miss low loss, increase sample size for tighter confidence.
            for _ in range(3):
                ping = self._run(
                    "client", "ping", "-c", "100", "-i", "0.05", "-W", "3", self.config["web_ip"]
                )
                samples.append(ping)
                match = re.search(r"(\d+(?:\.\d+)?)% packet loss", ping["stdout"])
                if ping["exit_code"] == 0 and match and float(match.group(1)) > 0:
                    break
            observations["ping_samples"] = samples
            ok = bool(match and float(match.group(1)) > 0 and ping["exit_code"] == 0)
        elif fault.id == "N4":
            load_stop = self._stop_load()
            baseline = self._run(
                "client", "ping", "-c", "20", "-i", "0.05", "-W", "3", self.config["web_ip"]
            )
            load_start = self._start_load()
            loaded = self._run(
                "client", "ping", "-c", "20", "-i", "0.05", "-W", "3", self.config["web_ip"]
            )
            baseline_rtts = [
                float(v) for v in re.findall(r"time[=<](\d+(?:\.\d+)?)\s*ms", baseline["stdout"])
            ]
            loaded_rtts = [
                float(v) for v in re.findall(r"time[=<](\d+(?:\.\d+)?)\s*ms", loaded["stdout"])
            ]
            # shortcut: 20 RTT samples make p95 coarse, increase ping count for stable percentiles.
            baseline_p95 = (
                sorted(baseline_rtts)[math.ceil(0.95 * len(baseline_rtts)) - 1]
                if baseline_rtts
                else None
            )
            loaded_p95 = (
                sorted(loaded_rtts)[math.ceil(0.95 * len(loaded_rtts)) - 1]
                if loaded_rtts
                else None
            )
            observations.update(
                baseline_ping=baseline,
                loaded_ping=loaded,
                baseline_p95_ms=baseline_p95,
                loaded_p95_ms=loaded_p95,
                load_stop=load_stop,
                load_start=load_start,
            )
            ok = (
                dns_ok
                and alt_ok
                and primary["stdout"].strip() == self.config["web_ip"]
                and curl["exit_code"] == 0
                and curl["stdout"].split()[:1] == ["200"]
                and load_stop["exit_code"] == 0
                and baseline["exit_code"] == 0
                and load_start["exit_code"] == 0
                and baseline_p95 is not None
                and loaded_p95 is not None
                and len(loaded_rtts) >= 10
                and loaded_p95 >= max(10, baseline_p95 * 3)
            )
        elif fault.id == "D3":
            direct = self._curl(
                scenario.target_url, "--resolve", f"{domain}:80:{self.config['web_ip']}"
            )
            observations["direct_curl"] = direct
            ok = (
                primary["exit_code"] == 9
                and alt_ok
                and alternate["exit_code"] == 0
                and "no servers could be reached" in primary["stdout"]
                and curl["exit_code"] == 28
                and direct["exit_code"] == 0
                and direct["stdout"].split()[:1] == ["200"]
            )
        elif fault.id == "D2":
            direct = self._curl(
                scenario.target_url, "--resolve", f"{domain}:80:{self.config['web_ip']}"
            )
            observations["direct_curl"] = direct
            ok = (
                dns_ok
                and alt_ok
                and primary["stdout"].strip() == fault.params.get("ip")
                and curl["exit_code"] in (7, 28)
                and direct["exit_code"] == 0
                and direct["stdout"].split()[:1] == ["200"]
            )
        elif fault.id == "T1":
            ok = (
                dns_ok
                and alt_ok
                and primary["stdout"].strip() == self.config["web_ip"]
                and curl["exit_code"] == 28
            )
        elif fault.id == "B1":
            other_domain = next((name for name in self.config["domains"] if name != domain), None)
            other = self._curl(f"https://{other_domain}/") if other_domain else {"exit_code": 1}
            observations["other_domain_curl"] = other
            ok = (
                dns_ok
                and alt_ok
                and primary["stdout"].strip() == self.config["web_ip"]
                and curl["exit_code"] in (35, 56)
                and "reset" in curl["stderr"].lower()
                and other["exit_code"] == 0
                and other["stdout"].split()[:1] == ["200"]
            )
        elif fault.id == "B2":
            blocked = self._run(
                "client",
                "curl",
                "--noproxy",
                "*",
                "--max-time",
                "5",
                "-sS",
                "-w",
                "\\n%{http_code}",
                scenario.target_url,
            )
            observations["block_page"] = blocked
            if urlsplit(scenario.target_url).scheme == "https":
                ok = blocked["exit_code"] == 7
            else:
                ok = (
                    blocked["exit_code"] == 0
                    and blocked["stdout"].splitlines()[-1:] == ["200"]
                    and "Access blocked by NetRunner lab" in blocked["stdout"]
                )
            ok = ok and dns_ok and alt_ok and primary["stdout"].strip() == fault.params["block_ip"]
        elif fault.id == "T2":
            ok = (
                dns_ok
                and alt_ok
                and primary["stdout"].strip() == self.config["web_ip"]
                and curl["exit_code"] == 7
                and "Connection refused" in curl["stderr"]
            )
        capture, path = self._capture(scenario.target_url)
        observations.update(capture=capture, pcap=path)
        if fault.id == "T1":
            ok = ok and "Flags [S]" in capture["stdout"] and "Flags [R" not in capture["stdout"]
        elif fault.id == "B1" or fault.id == "T2":
            ok = ok and "Flags [R" in capture["stdout"]
        elif fault.id == "B2" and urlsplit(scenario.target_url).scheme == "https":
            ok = ok and any(
                line.rsplit("IP ", 1)[-1].startswith(f"{fault.params['block_ip']}.443 >")
                and "Flags [R" in line
                for line in capture["stdout"].splitlines()
            )
        ok = ok and capture["exit_code"] == 0 and capture["packets"] > 0 and Path(path).is_file()
        return LabResult(ok, f"{fault.id} 증상 {'재현' if ok else '확인 실패'}", observations)

    def verify_lab_combo(self, scenario: Scenario, observable: dict[str, bool]) -> LabResult:
        """lab-check 전용 복수 장애 확인. 심판/Breaker 경로에서는 호출하지 않는다.

        가려진 장애는 확인하지 않고 false로 기록한다. N1은 다른 계층 장애의
        DNS/HTTP 결과와 무관하게 고정 웹 IP ping으로 직접 확인한다.
        """
        checks: dict[str, dict] = {}
        for fault in scenario.faults:
            if not observable[fault.id]:
                checks[fault.id] = {
                    "observable": False,
                    "verified": False,
                    "masked": True,
                    "reason": "선행 장애에 가려져 확인하지 않음",
                }
                continue
            if fault.id == "N1":
                ping = self._run("client", "ping", "-c", "4", "-W", "3", self.config["web_ip"])
                samples = _ping_rtt_samples(ping["stdout"])
                verified = (
                    ping["exit_code"] == 0
                    and len(samples) >= 4
                    and sum(samples) / len(samples) >= fault.params["delay_ms"] / 2
                )
                result = LabResult(
                    verified,
                    f"N1 증상 {'재현' if verified else '확인 실패'}",
                    {"ping": ping, "rtt_samples_ms": samples},
                )
            else:
                result = self.verify_symptom(replace(scenario, faults=[fault]))
            checks[fault.id] = {
                "observable": True,
                "verified": result.ok,
                "masked": False,
                "result": asdict(result),
            }
        visible = [fault.id for fault in scenario.faults if observable[fault.id]]
        ok = bool(visible) and all(checks[fault_id]["verified"] for fault_id in visible)
        return LabResult(
            ok,
            "복수 장애 가시 증상 " + ("확인 완료" if ok else "확인 실패"),
            {"fault_checks": checks, "observable_faults": visible},
        )

    def reset(self) -> LabResult:
        """캡처를 멈추고 DNS와 방화벽을 정상 설정으로 되돌린다."""
        capture = self._run("client", "sh", "/stop-capture.sh", timeout=10)
        load = self._stop_load()
        netem = self._run(
            "client",
            "sh",
            "-c",
            "set -e; q=$(tc qdisc show dev eth0); "
            'case "$q" in *"qdisc netem 10:"*) tc qdisc del dev eth0 root;; esac',
        )
        dns_netem = self._run(
            "dns",
            "sh",
            "-c",
            "q=$(tc qdisc show dev eth0); "
            'case "$q" in *"qdisc netem 20:"*) tc qdisc del dev eth0 root;; esac',
        )
        router_qdisc_script = """\
set -e
dev=$(ip route get "$1" | awk '$2 == "dev" {print $3}')
case "$dev" in eth[0-9]*) ;; *) exit 1;; esac
q=$(tc qdisc show dev "$dev")
case "$q" in *"qdisc tbf 30:"*) tc qdisc del dev "$dev" root;; esac
"""
        router_qdisc = self._run(
            "router",
            "sh",
            "-c",
            router_qdisc_script,
            "sh",
            self.hosts["client"],
        )
        router = self._run("router", "iptables", "-F", "NETRUNNER")
        firewall = self._run("web", "iptables", "-F", "NETRUNNER")
        backend = self._run(
            "backend",
            "sh",
            "-c",
            "pidof httpd >/dev/null || su-exec nobody httpd -p 8000 -h /www",
        )
        tls = self._set_cert("valid")
        redirect = self._set_redirect(False)
        dns = self._set_dns()
        ok = all(
            r["exit_code"] == 0
            for r in (
                capture,
                load,
                netem,
                dns_netem,
                router_qdisc,
                dns,
                firewall,
                router,
                backend,
                tls,
                redirect,
            )
        )
        return LabResult(
            ok,
            "초기화 완료" if ok else "초기화 실패",
            {
                "capture": capture,
                "netem": netem,
                "dns_netem": dns_netem,
                "load": load,
                "router_qdisc": router_qdisc,
                "dns": dns,
                "firewall": firewall,
                "router": router,
                "backend": backend,
                "tls": tls,
                "redirect": redirect,
            },
        )
