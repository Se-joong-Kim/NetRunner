"""H1 전용 Docker Lab. 모든 HTTP 관측은 client 컨테이너에서 실행한다."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .contracts import FaultSpec, LabResult, Scenario

ROOT = Path(__file__).resolve().parent.parent
TARGET = "http://nginx/"

# 인자를 쉘로 해석하지 않고 python argv로 전달한다. HTTP 오류도 원본 응답을 보관한다.
CLIENT_PROBE = """import json, sys, time
from urllib.request import build_opener, ProxyHandler
from urllib.error import HTTPError, URLError
t = time.monotonic()
try:
    try:
        response = build_opener(ProxyHandler({})).open(sys.argv[1], timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read(4096).decode("utf-8", errors="replace")
        raw = "HTTP %s %s\\n%s\\n%s" % (response.code, response.reason, response.headers, body)
        result = {"http_code": response.code, "raw_http": raw}
except (URLError, OSError, TimeoutError) as exc:
    result = {"http_code": None, "raw_http": str(exc)}
result["elapsed_ms"] = round((time.monotonic() - t) * 1000)
print(json.dumps(result))
"""


class HttpDockerLab:
    """Lab의 네 함수를 구현한다. H1 외의 장애는 명시적으로 거부한다."""

    def __init__(self):
        self.directory = ROOT / "labs" / "http"
        self.baseline = (self.directory / "nginx.conf").read_bytes()
        if self.baseline.count(b"http://backend:8000") != 1:
            raise ValueError("nginx.conf에 정상 upstream 설정이 한 번 있어야 합니다.")
        self.active = self.directory / "active.conf"
        if not self.active.exists():
            self.active.write_bytes(self.baseline)
        candidates = [
            shutil.which("docker"),
            str(
                Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
                / "Docker/Docker/resources/bin/docker.exe"
            ),
            str(
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Programs/DockerDesktop/resources/bin/docker.exe"
            ),
            str(Path.home() / "AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe"),
        ]
        self.docker = next((p for p in candidates if p and Path(p).is_file()), None)
        if not self.docker:
            raise RuntimeError("Docker 명령을 찾을 수 없습니다.")

    def compose(self, *args):
        """전용 Compose 프로젝트에서 명령을 실행하고 출력을 보관한다."""
        command = [
            self.docker,
            "compose",
            "-p",
            "netrunner-http",
            "-f",
            str(self.directory / "compose.yaml"),
            *args,
        ]
        result = subprocess.run(
            command,
            cwd=self.directory,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        observation = {
            "command": ["docker", *command[1:]],
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
        if result.returncode:
            raise RuntimeError(json.dumps(observation, ensure_ascii=False))
        return observation

    def start(self):
        """HTTP 실험망의 서버와 클라이언트를 실행한다."""
        self.compose("up", "-d")

    def probe(self, target_url: str) -> dict:
        """클라이언트 컨테이너에서 대상의 실제 HTTP 응답을 수집한다."""
        if target_url != TARGET:
            raise ValueError(f"HTTP 단독 실험망은 {TARGET}만 지원합니다.")
        command = self.compose("exec", "-T", "client", "python", "-c", CLIENT_PROBE, target_url)
        result = json.loads(command["stdout"])
        result["execution"] = command
        result["location"] = "client container"
        return result

    def _wait(self, target_url: str, expected: int) -> LabResult:
        attempts = []
        deadline = time.monotonic() + 20
        while True:
            observation = self.probe(target_url)
            attempts.append(observation)
            ok = observation["http_code"] == expected
            if ok or time.monotonic() >= deadline:
                return LabResult(
                    ok,
                    f"HTTP {expected} {'확인' if ok else '확인 실패'}",
                    {"http_code": observation["http_code"], "attempts": attempts},
                )
            time.sleep(1)

    def health_check(self, target_url: str) -> LabResult:
        """대상에서 정상 HTTP 200 응답이 돌아오는지 확인한다."""
        return self._wait(target_url, 200)

    def inject(self, fault: FaultSpec) -> LabResult:
        """단일 H1 장애를 upstream 포트 변경으로 주입한다."""
        if fault.id != "H1" or fault.params:
            return LabResult(False, "매개변수 없는 H1만 지원합니다.")
        self.active.write_bytes(
            self.baseline.replace(b"http://backend:8000", b"http://backend:8001")
        )
        command = self.compose("restart", "nginx")
        return LabResult(
            True,
            "H1 주입: upstream 포트 8000 → 8001",
            {"mechanism": "upstream port mismatch", "execution": command},
        )

    def verify_symptom(self, scenario: Scenario) -> LabResult:
        """단일 H1 시나리오의 HTTP 502 증상이 재현되는지 확인한다."""
        if len(scenario.faults) != 1 or scenario.faults[0].id != "H1" or scenario.faults[0].params:
            return LabResult(False, "단일 H1 시나리오만 지원합니다.")
        return self._wait(scenario.target_url, 502)

    def reset(self) -> LabResult:
        """정상 nginx 설정을 복구하고 서버를 재시작한다."""
        self.active.write_bytes(self.baseline)
        command = self.compose("restart", "nginx")
        return LabResult(True, "nginx 정상 설정 복구", {"execution": command})
