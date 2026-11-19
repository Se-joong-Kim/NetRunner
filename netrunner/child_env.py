"""외부 프로세스에 넘길 환경변수 허용 목록.

opencode, Docker Compose, HTTP 실험망 도구가 함께 쓴다. 목록에 있는 이름과
허용 접두사만 넘기고, 접두사가 맞아도 비밀 표지가 든 이름은 넘기지 않는다.
목록을 늘리는 이유는 CLAUDE.md의 opencode 권한 절에 적는다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TERM",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        # Docker 클라이언트 설정(연결 대상과 컨텍스트). 인증 값은 HOME의 설정 파일에서 읽는다.
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY",
        "COMPOSE_PROJECT_NAME",
    }
)
PREFIXES = ("LC_", "XDG_")
# Windows에서만 넘기는 시스템 변수. Node.js(OpenSSL)는 SYSTEMROOT가 없으면 난수
# 생성기를 초기화하지 못해 시작하자마자 종료한다(#40). USERPROFILE과 APPDATA는
# 실제 GLM 호출로 필요한지 확인한 뒤 넣는다.
WINDOWS_NAMES = frozenset(
    {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "PROGRAMFILES", "TEMP", "TMP"}
)
IS_WINDOWS = os.name == "nt"
# 접두사로 허용해도 이름에 이 표지가 있으면 넘기지 않는다(예: LC_TOKEN).
SECRET_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")


def is_allowed(name: str) -> bool:
    """허용 목록의 이름이거나 허용 접두사로 시작하고, 비밀 표지가 없는지 본다.

    Windows에서는 WINDOWS_NAMES의 이름도 허용한다. 이름은 대문자로 비교한다.
    """
    if any(marker in name.upper() for marker in SECRET_MARKERS):
        return False
    if name in NAMES or name.startswith(PREFIXES):
        return True
    return IS_WINDOWS and name.upper() in WINDOWS_NAMES


def child_environment(
    environ: Mapping[str, str], extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """허용된 변수만 복사하고 extra를 더한다.

    extra는 호출하는 쪽이 정한 값(예: 설정 파일 경로)이라 이름 검사를 하지 않는다.
    """
    kept = {k: v for k, v in environ.items() if is_allowed(k)}
    kept.update(extra or {})
    return kept
