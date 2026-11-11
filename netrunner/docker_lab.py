"""Docker Compose로 첫 통합 장애를 재현하는 실험망."""
from __future__ import annotations

import ipaddress
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import FaultSpec, LabResult, Scenario, read_json

ROOT = Path(__file__).resolve().parent.parent


class DockerLab:
    # shortcut: 한 실험망은 순차 실행만 지원, 동시 라운드 지원 시 설정과 캡처를 격리한다.
    def __init__(self, config: str | Path = ROOT / "config/lab.json"):
        self.config = read_json(config)
        self.hosts = self.config["hosts"]

    def _run(self, service: str, *args: str, timeout: int = 20) -> dict:
        cmd = ["docker", "compose", "-f", str(ROOT / "compose.yaml"), "exec", "-T", service, *args]
        start = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return {"command": cmd, "exit_code": p.returncode, "stdout": p.stdout,
                    "stderr": p.stderr, "elapsed_ms": round((time.monotonic() - start) * 1000)}
        except subprocess.TimeoutExpired as e:
            outputs = {key: value.decode(errors="replace") if isinstance(value, bytes) else value or ""
                       for key, value in (("stdout", e.stdout), ("stderr", e.stderr))}
            return {"command": cmd, "exit_code": None, **outputs, "error": str(e),
                    "elapsed_ms": round((time.monotonic() - start) * 1000)}
        except OSError as e:
            return {"command": cmd, "exit_code": None, "stdout": "", "stderr": str(e),
                    "elapsed_ms": round((time.monotonic() - start) * 1000)}

    def _target(self, url: str) -> bool:
        try:
            u = urlsplit(url)
            return (u.scheme == "http" and u.hostname in self.config["domains"]
                    and (u.port is None or u.port == 80) and not u.username and not u.password)
        except ValueError:
            return False

    def _curl(self, url: str, *extra: str) -> dict:
        return self._run("client", "curl", "--noproxy", "*", "--max-time", "5", "-sS", "-v",
                         "-o", "/dev/null", "-w", "%{http_code} %{time_namelookup} %{time_connect} %{time_total}",
                         *extra, url)

    def _dns(self, server: str, domain: str) -> dict:
        return self._run("client", "dig", "+time=2", "+tries=1", "+short", "@" + server, domain, "A")

    def _set_dns(self, domain: str | None = None, ip: str | None = None) -> dict:
        hosts = [(ip if d == domain else self.config["web_ip"], d) for d in self.config["domains"]]
        args = [item for pair in hosts for item in pair]
        script = ('set -e; : > /tmp/dns-hosts.new; while [ "$#" -gt 0 ]; do '
                  'printf "%s %s\\n" "$1" "$2" >> /tmp/dns-hosts.new; shift 2; done; '
                  'mv /tmp/dns-hosts.new /tmp/dns-hosts; kill -HUP "$(cat /run/dnsmasq.pid)"')
        result = self._run("dns", "sh", "-c", script, "sh", *args)
        if result["exit_code"] == 0:
            for _ in range(3):
                checks = [self._dns(self.hosts["dns"], d) for _, d in hosts]
                result["checks"] = checks
                if all(check["exit_code"] == 0 and check["stdout"].strip() == expected
                       for (expected, _), check in zip(hosts, checks)):
                    break
                time.sleep(0.1)
            else:
                result["exit_code"] = 1
                result["stderr"] += "DNS 설정 적용 확인 실패"
        return result

    def health_check(self, target_url: str) -> LabResult:
        if not self._target(target_url):
            return LabResult(False, "허용되지 않는 대상 URL")
        r = self._curl(target_url)
        ok = r["exit_code"] == 0 and r["stdout"].split()[:1] == ["200"]
        return LabResult(ok, "정상 HTTP 200" if ok else "정상 접속 실패", {"curl": r})

    def inject(self, fault: FaultSpec) -> LabResult:
        if fault.id == "D2" and set(fault.params) == {"domain", "ip"}:
            domain, ip = fault.params["domain"], fault.params["ip"]
            try:
                address = ipaddress.ip_address(ip)
                allowed = (isinstance(ip, str) and isinstance(domain, str) and domain in self.config["domains"]
                           and address in ipaddress.ip_network(self.config["subnet"])
                           and str(address) not in self.hosts.values())
            except (ValueError, TypeError):
                allowed = False
            if not allowed:
                return LabResult(False, "허용되지 않는 D2 도메인 또는 IP")
            r = self._set_dns(domain, str(address))
        elif fault.id == "T2" and fault.params == {"port": 80} and type(fault.params["port"]) is int:
            r = self._run("web", "iptables", "-A", "NETRUNNER", "-p", "tcp", "--dport", "80",
                          "-j", "REJECT", "--reject-with", "tcp-reset")
        else:
            return LabResult(False, "지원하지 않는 장애 또는 매개변수")
        return LabResult(r["exit_code"] == 0, f"{fault.id} 주입", {"command": r})

    def _capture(self, url: str) -> tuple[dict, str]:
        (ROOT / "captures").mkdir(exist_ok=True)
        name = f"lab-{uuid.uuid4().hex}.pcap"
        path = "/captures/" + name
        try:
            result = self._run("client", "sh", "-c", 'setsid sh /capture.sh "$@" & wait $!',
                               "sh", path, url, timeout=15)
        finally:
            stopped = self._run("client", "sh", "/stop-capture.sh", timeout=10)
        result["cleanup"] = stopped
        if stopped["exit_code"] != 0:
            result["exit_code"] = 1
        lines = result["stdout"].splitlines()
        numbers = [int(match.group(1)) for line in lines if (match := re.match(r"^\s*(\d+)\s", line))]
        result["output"] = re.sub(r"(?m)^\s*(\d+)\s+", r"#\1 ", result["stdout"])
        result["packets"] = max(numbers, default=0)
        result["file"] = str(ROOT / "captures" / name)
        return result, str(ROOT / "captures" / name)

    def verify_symptom(self, scenario: Scenario) -> LabResult:
        if not self._target(scenario.target_url) or len(scenario.faults) != 1:
            return LabResult(False, "첫 통합은 허용된 URL의 단일 장애만 지원")
        fault = scenario.faults[0]
        if fault.id not in ("D2", "T2"):
            return LabResult(False, "지원하지 않는 장애")
        domain = scenario.domain
        primary = self._dns(self.hosts["dns"], domain)
        alternate = self._dns(self.hosts["alt_dns"], domain)
        curl = self._curl(scenario.target_url)
        capture, path = self._capture(scenario.target_url)
        observations = {"dns": primary, "alt_dns": alternate, "curl": curl,
                        "capture": capture, "pcap": path}
        dns_ok = primary["exit_code"] == alternate["exit_code"] == 0
        alt_ok = alternate["stdout"].strip() == self.config["web_ip"]
        if fault.id == "D2":
            direct = self._curl(scenario.target_url, "--resolve", f"{domain}:80:{self.config['web_ip']}")
            observations["direct_curl"] = direct
            ok = (dns_ok and alt_ok and primary["stdout"].strip() == fault.params.get("ip")
                  and curl["exit_code"] in (7, 28) and direct["exit_code"] == 0
                  and direct["stdout"].split()[:1] == ["200"])
        else:
            ok = (dns_ok and alt_ok and primary["stdout"].strip() == self.config["web_ip"]
                  and curl["exit_code"] == 7 and "Connection refused" in curl["stderr"]
                  and "Flags [R" in capture["stdout"])
        ok = ok and capture["exit_code"] == 0 and capture["packets"] > 0 and Path(path).is_file()
        return LabResult(ok, f"{fault.id} 증상 {'재현' if ok else '확인 실패'}", observations)

    def reset(self) -> LabResult:
        capture = self._run("client", "sh", "/stop-capture.sh", timeout=10)
        dns = self._set_dns()
        firewall = self._run("web", "iptables", "-F", "NETRUNNER")
        ok = capture["exit_code"] == dns["exit_code"] == firewall["exit_code"] == 0
        return LabResult(ok, "초기화 완료" if ok else "초기화 실패",
                         {"capture": capture, "dns": dns, "firewall": firewall})
