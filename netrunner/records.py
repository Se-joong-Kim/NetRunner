"""정답이 든 기록의 저장 위치와 파일 권한.

라운드 기록, Breaker 호출 기록, 제안 시나리오, lab-check 기록, 결과
화면에는 정답이 들어 있다. 기본 위치는 저장소 밖 ~/netrunner-private/runs
이고, 새로 만드는 폴더는 700, 파일은 600으로 둔다(CLAUDE.md 역할별 권한).
기록을 쓰는 코드는 모두 이 모듈을 거친다.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO

DIR_MODE = 0o700
FILE_MODE = 0o600


def default_runs_dir() -> Path:
    """기록 폴더 기본값. 부를 때마다 홈 폴더를 다시 읽는다."""
    return Path.home() / "netrunner-private" / "runs"


def ensure_private_dir(path: str | Path) -> Path:
    """폴더가 없으면 위쪽까지 700으로 만든다.

    이미 있는 폴더의 권한은 바꾸지 않는다. --out으로 받은 기존 폴더(예:
    홈)를 함부로 잠그지 않기 위해서다.
    """
    path = Path(path).expanduser()
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(mode=DIR_MODE)
        os.chmod(d, DIR_MODE)  # umask가 mode를 깎을 수 있어 다시 맞춘다
    return path


def open_private(path: str | Path, mode: str = "a") -> TextIO:
    """기록 파일을 600으로 연다. mode는 "a"(덧붙이기) 또는 "w"(새로 쓰기).

    전에 다른 권한으로 만들어진 파일이면 600으로 좁힌다.
    """
    if mode not in ("a", "w"):
        raise ValueError(f"지원하지 않는 mode: {mode!r}")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    # Windows의 os.open은 텍스트 모드라 줄바꿈이 두 번 바뀐다. 변환은 fdopen만 하게 한다.
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, FILE_MODE)
    try:
        os.chmod(path, FILE_MODE)
        return os.fdopen(fd, mode, encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def write_private_text(path: str | Path, text: str) -> None:
    """기록 파일 하나를 600으로 새로 쓴다."""
    with open_private(path, "w") as fp:
        fp.write(text)
