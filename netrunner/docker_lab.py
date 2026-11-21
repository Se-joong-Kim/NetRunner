"""Docker Compose로 첫 통합 장애를 재현하는 실험망."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import time
import uuid
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
        if tool not in {"dig", "curl", "capture"}:
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
        if tool == "dig":
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
                u.scheme == "http"
                and u.hostname in self.config["domains"]
                and (u.port is None or u.port == 80)
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

    def _dns(self, server: str, domain: str) -> dict:
        return self._run(
            "client", "dig", "+time=2", "+tries=1", "+short", "@" + server, domain, "A"
        )

    def _set_dns(self, domain: str | None = None, ip: str | None = None) -> dict:
        hosts = [(ip if d == domain else self.config["web_ip"], d) for d in self.config["domains"]]
        args = [item for address, name in hosts if address is not None for item in (address, name)]
        script = (
            'set -e; : > /tmp/dns-hosts.new; while [ "$#" -gt 0 ]; do '
            'printf "%s %s\\n" "$1" "$2" >> /tmp/dns-hosts.new; shift 2; done; '
            'mv /tmp/dns-hosts.new /tmp/dns-hosts; kill -HUP "$(cat /run/dnsmasq.pid)"'
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

    def inject(self, fault: FaultSpec) -> LabResult:
        """DNS 응답 또는 웹 서버 방화벽을 바꿔 장애를 심는다."""
        if (
            fault.id == "D1"
            and set(fault.params) == {"domain"}
            and fault.params["domain"] in self.config["domains"]
        ):
            r = self._set_dns(fault.params["domain"])
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
        if fault.id not in ("D1", "D2", "T2"):
            return LabResult(False, "지원하지 않는 장애")
        domain = scenario.domain
        primary = self._dns(self.hosts["dns"], domain)
        alternate = self._dns(self.hosts["alt_dns"], domain)
        curl = self._curl(scenario.target_url)
        capture, path = self._capture(scenario.target_url)
        observations = {
            "dns": primary,
            "alt_dns": alternate,
            "curl": curl,
            "capture": capture,
            "pcap": path,
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
        else:
            ok = (
                dns_ok
                and alt_ok
                and primary["stdout"].strip() == self.config["web_ip"]
                and curl["exit_code"] == 7
                and "Connection refused" in curl["stderr"]
                and "Flags [R" in capture["stdout"]
            )
        ok = ok and capture["exit_code"] == 0 and capture["packets"] > 0 and Path(path).is_file()
        return LabResult(ok, f"{fault.id} 증상 {'재현' if ok else '확인 실패'}", observations)

    def reset(self) -> LabResult:
        """캡처를 멈추고 DNS와 방화벽을 정상 설정으로 되돌린다."""
        capture = self._run("client", "sh", "/stop-capture.sh", timeout=10)
        dns = self._set_dns()
        firewall = self._run("web", "iptables", "-F", "NETRUNNER")
        ok = capture["exit_code"] == dns["exit_code"] == firewall["exit_code"] == 0
        return LabResult(
            ok,
            "초기화 완료" if ok else "초기화 실패",
            {"capture": capture, "dns": dns, "firewall": firewall},
        )
