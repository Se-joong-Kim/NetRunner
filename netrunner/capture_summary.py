"""tcpdump 출력의 관측 건수와 대표 패킷을 원래 번호로 보여 준다."""

from __future__ import annotations

import re
from collections import defaultdict

_NUMBERED = re.compile(r"^#([0-9]+)\s+(.+)$")
_DNS = re.compile(r"(\.53 > )| > \S+\.53:")
_TCP_FLAGS = re.compile(r"\bFlags \[([^\]]+)\]")


def summarize_capture(output: str, packets: int | None) -> str:
    """관측 종류별 건수와 처음·마지막 패킷을 번호 순으로 정리한다."""
    groups = defaultdict(list)
    numbered = []
    for line in output.splitlines():
        match = _NUMBERED.fullmatch(line)
        if match is None:
            continue
        text = match[2]
        flags = _TCP_FLAGS.search(text)
        dns = _DNS.search(text)
        if flags:
            kind = f"TCP [{flags[1]}]"
        elif dns:
            kind = "DNS 응답" if dns[1] else "DNS 질의"
        else:
            kind = "기타"
        position = len(numbered)
        numbered.append(line)
        groups[kind].append(position)

    if not numbered:
        return output

    samples = {index for positions in groups.values() for index in (positions[0], positions[-1])}
    counts = ", ".join(f"{kind} {len(positions)}" for kind, positions in groups.items())
    total = f" / 전체 {packets}개" if packets is not None else ""
    return "\n".join(
        [
            f"출력에서 확인한 패킷 {len(numbered)}개{total}",
            f"관측 종류별 건수: {counts}",
            "종류별 처음·마지막 패킷 (원래 번호와 출력 순서):",
            *(numbered[index] for index in sorted(samples)),
        ]
    )
