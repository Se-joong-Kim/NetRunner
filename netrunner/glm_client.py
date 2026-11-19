"""GLM 호출 연결부. opencode CLI를 subprocess로 불러 텍스트 응답을 받는다.

Breaker와 Diagnoser가 같은 함수를 쓴다. 호출 실패는 CompletionError로
올리고, 재시도와 프롬프트 구성은 호출하는 쪽이 맡는다. 샌드박스 설정은
config/opencode_sandbox.json을 쓰며, 실행 디렉터리를 프로젝트 루트로 맞춰야
상대 경로 권한이 맞는다.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .breaker import CompletionError
from .child_env import child_environment

DEFAULT_MODEL = "opencode-go/glm-5.3-flash"
DEFAULT_AGENT = "netrunner"
DEFAULT_TIMEOUT = 120.0

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class OpencodeComplete:
    """프롬프트를 받아 GLM 응답 문자열을 돌려주는 호출 함수."""

    binary: str
    root: Path
    config: Path
    model: str = DEFAULT_MODEL
    agent: str = DEFAULT_AGENT
    timeout: float = DEFAULT_TIMEOUT
    run: Runner = subprocess.run

    def __call__(self, prompt: str) -> str:
        """프롬프트를 보내고 응답 문자열을 돌려준다. 실패는 CompletionError."""
        argv = [
            self.binary,
            "run",
            "--standalone",
            "--agent",
            self.agent,
            "-m",
            self.model,
            "--format",
            "json",
            "--archived",
        ]
        env = child_environment(os.environ, {"OPENCODE_CONFIG": str(self.config.resolve())})
        try:
            proc = self.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=self.root,
                env=env,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise CompletionError(f"opencode 시간 초과({self.timeout:g}초)") from e
        except OSError as e:
            raise CompletionError(f"opencode 실행 실패: {e}") from e
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()[:300]
            raise CompletionError(f"opencode 종료 코드 {proc.returncode}: {detail}")
        return extract_text(proc.stdout)


def extract_text(stdout: str) -> str:
    """응답 문자열을 모은다. 입력은 --format json 출력(줄마다 이벤트 하나)이다.

    이벤트가 error면 그 메시지로 CompletionError를 낸다. JSON이 아닌 줄은
    무시한다. 응답이 비어 있으면 CompletionError다.
    """
    parts: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "error":
            raise CompletionError(f"모델 오류: {event.get('error')}")
        if kind == "text":
            parts.append(str(event.get("part", {}).get("text", "")))
    text = "".join(parts).strip()
    if not text:
        raise CompletionError("모델 응답이 비어 있음")
    return text
