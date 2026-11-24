"""라운드 기록(JSONL)으로 비교 지표와 결과 화면을 만든다.

비율은 설계안 9.3절대로 "23/30 (77%)"처럼 개수와 함께 적고, 모델·방식별로
나눈다.
"""

from __future__ import annotations

import html
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from .contracts import Status
from .faults import FAULTS

VERDICT_KO = {
    "correct": "정답",
    "cause_wrong": "원인 틀림",
    "stage_wrong": "단계 틀림",
    "missed": "놓침",
    # 증상이 재현된 뒤 진단기가 답을 못 냄(형식 오류, 시간 초과 등)
    "failed": "진단 실패",
    "masked": "관측 불가(제외)",
}


def load_records(path: str | Path) -> list[dict]:
    """JSONL 파일의 라운드 기록을 모두 읽는다."""
    with Path(path).open(encoding="utf-8") as fp:
        return [json.loads(line) for line in fp if line.strip()]


def ratio(n: int, d: int) -> str:
    """'n/d (비율)' 문자열. 분모가 0이면 '0/0 (-)'."""
    return f"{n}/{d} ({n / d:.0%})" if d else "0/0 (-)"


def p95(values: list[float]) -> float:
    """올림 방식 95 백분위수."""
    s = sorted(values)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def _reproduced(rec: dict) -> bool:
    """실험망 기록에 성공한 증상 확인 단계가 있으면 True."""
    return any(s["step"] == "verify_symptom" and s["ok"] for s in rec["lab_log"])


def diagnosis_failed(rec: dict) -> bool:
    """증상이 재현된 뒤 진단 단계에서 무효가 된 라운드면 True.

    재현 확인 뒤에 라운드가 무효가 되는 경로는 진단기 쪽(형식 오류,
    실행 오류, round_id 불일치)뿐이다. 이런 라운드는 설계안 9.3절대로
    심은 장애를 못 맞힌 것으로 정확도 분모에 넣는다. 실험망 쪽 무효
    (주입·재현 실패)는 진단기 책임이 아니므로 뺀다.
    """
    return rec["status"] == Status.INVALID and _reproduced(rec)


def _failed_faults(rec: dict) -> list[tuple[str, bool]]:
    """진단 실패 라운드의 정답 장애 ID와 관측 가능 여부."""
    ids = [f["id"] for f in rec["answer"]["faults"]]
    return list(zip(ids, rec["validation"]["observable"], strict=True))


def _failed_scored(rec: dict) -> int:
    """진단 실패 라운드에서 정확도 분모에 들어갈 장애 수(관측 가능한 것만)."""
    return sum(seen for _, seen in _failed_faults(rec))


def summarize_breaker(records: list[dict]) -> dict:
    """Breaker 제안이 실험망에서 얼마나 재현됐는지 센다(대결 기록용).

    고정 세트 라운드는 뺀다. 제안 실패(매치 종료)는 제안 수에 넣지 않고
    따로 센다. 진단 결과와 상관없는 숫자라서 자리표시 진단기로도 잴 수 있다.
    """
    recs = [r for r in records if r["origin"] == "breaker"]
    proposals = [r for r in recs if r["status"] != Status.PROPOSAL_FAILED]
    lab = [r for r in proposals if r["status"] != Status.REJECTED]
    reasons = Counter(r["invalid_reason"] for r in lab if r["status"] == Status.INVALID)
    return {
        "proposals": len(proposals),
        "rejected": len(proposals) - len(lab),
        "lab_rounds": len(lab),
        "reproduced": sum(_reproduced(r) for r in lab),
        "reset_ok": sum(r["reset_verified"] is True for r in lab),
        "proposal_failed": len(recs) - len(proposals),
        "invalid_reasons": dict(reasons),
    }


def format_breaker_summary(s: dict) -> str:
    """summarize_breaker 결과를 사람이 읽을 줄로 만든다."""
    lines = [
        f"Breaker 제안 {s['proposals']}개: 검증 거부 {s['rejected']}, "
        f"실험망 라운드 {s['lab_rounds']}",
        f"증상 재현 {ratio(s['reproduced'], s['lab_rounds'])}, "
        f"정상화 확인 {ratio(s['reset_ok'], s['lab_rounds'])}, 제안 실패 {s['proposal_failed']}",
    ]
    if s["invalid_reasons"]:
        reasons = ", ".join(f"{k} {n}" for k, n in sorted(s["invalid_reasons"].items()))
        lines.append(f"무효 사유: {reasons}")
    return "\n".join(lines)


def summarize(records: list[dict]) -> list[dict]:
    """모델·방식·세트별 지표.

    검증에서 거부된 제안과 Breaker의 제안 실패는 지표에서 뺀다.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        if r["status"] not in (Status.REJECTED, Status.PROPOSAL_FAILED):
            groups[(r["model"], r["method"], r["origin"])].append(r)

    rows = []
    for (model, method, origin), rs in sorted(groups.items()):
        sc = [r["score"] for r in rs if r["status"] == Status.SCORED]
        failed = [r for r in rs if diagnosis_failed(r)]
        times = [s["elapsed_s"] for s in sc]
        tools = [s["tool_calls"] for s in sc]
        faults = sum(s["scored_faults"] for s in sc) + sum(_failed_scored(r) for r in failed)
        fooled = len(failed) + sum(
            any(f["verdict"] not in ("correct", "masked") for f in s["faults"])
            or bool(s["false_positives"])
            for s in sc
        )
        rows.append(
            {
                "model": model,
                "method": method,
                "origin": origin,
                "rounds": ratio(len(sc), len(rs)),
                "stage_acc": ratio(sum(s["stage_correct"] for s in sc), faults),
                "cause_acc": ratio(sum(s["cause_correct"] for s in sc), faults),
                "diag_failed": len(failed),
                "false_pos": sum(len(s["false_positives"]) for s in sc),
                "evidence": ratio(
                    sum(s["evidence_valid"] for s in sc), sum(s["evidence_total"] for s in sc)
                ),
                "evidence_unverified": sum(s["evidence_unverified"] for s in sc),
                "tool_calls_med": statistics.median(tools) if tools else "-",
                "time_med_s": round(statistics.median(times), 2) if times else "-",
                "time_p95_s": round(p95(times), 2) if times else "-",
                "fooled": ratio(fooled, len(sc) + len(failed)),
                # 실험망을 쓴 라운드 중 주입 뒤 증상까지 확인된 비율
                "repro": ratio(sum(_reproduced(r) for r in rs), len(rs)),
                "reset_ok": ratio(
                    sum(r["reset_verified"] is True for r in rs),
                    sum(r["reset_verified"] is not None for r in rs),
                ),
            }
        )
    return rows


def _scored(records: list[dict]) -> list[dict]:
    return [r for r in records if r["status"] == Status.SCORED]


def misdiagnosis_rows(records: list[dict]) -> list[dict]:
    """방식·모델·세트·장애별로 판정(정답, 원인 틀림, 놓침 등)을 센다."""
    counts: dict[tuple, Counter] = defaultdict(Counter)
    for r in _scored(records):
        for f in r["score"]["faults"]:
            key = (r["method"], r["model"], r["origin"], f"{f['id']} {f['label']}")
            counts[key][f["verdict"]] += 1
    for r in records:
        if diagnosis_failed(r):
            for fid, seen in _failed_faults(r):
                key = (r["method"], r["model"], r["origin"], f"{fid} {FAULTS[fid].label}")
                counts[key]["failed" if seen else "masked"] += 1
    return [
        {"model": m, "method": me, "origin": o, "fault": fault, **{v: c[v] for v in VERDICT_KO}}
        for (me, m, o, fault), c in sorted(counts.items())
    ]


def false_positive_rows(records: list[dict]) -> list[dict]:
    """방식·모델·세트별로 정답에 없는 진단(오탐)의 라벨을 센다."""
    counts: Counter = Counter()
    for r in _scored(records):
        for label in r["score"]["false_positives"]:
            counts[(r["method"], r["model"], r["origin"], label)] += 1
    return [
        {"model": m, "method": me, "origin": o, "label": label, "count": n}
        for (me, m, o, label), n in sorted(counts.items())
    ]


def repeat_rows(records: list[dict]) -> list[dict]:
    """방식·모델·세트·반복 회차별 단계·원인 정답 수.

    회차는 기록 순서로 정한다. 같은 방식·모델·세트에서 같은 시나리오가
    k번째로 나오면 k회차다(run --repeat의 반복 → 시나리오 → 진단기
    순서와 맞는다). 무효 라운드도 회차는 센다. 진단 실패 라운드는
    분모에 넣고, 실험망 쪽 무효 라운드는 채점에서 뺀다.
    stage와 cause는 (맞힌 수, 채점한 장애 수) 짝이다.
    """
    seen: Counter = Counter()
    sums: dict[tuple, list[int]] = {}
    for r in records:
        if r["status"] == Status.PROPOSAL_FAILED:
            continue
        group = (r["method"], r["model"], r["origin"])
        seen[(*group, r["scenario_id"])] += 1
        s = sums.setdefault((*group, seen[(*group, r["scenario_id"])]), [0, 0, 0])
        if r["status"] == Status.SCORED:
            sc = r["score"]
            s[0] += sc["stage_correct"]
            s[1] += sc["cause_correct"]
            s[2] += sc["scored_faults"]
        elif diagnosis_failed(r):
            s[2] += _failed_scored(r)
    return [
        {
            "model": m,
            "method": me,
            "origin": o,
            "repeat": k,
            "stage": (st, n),
            "cause": (ca, n),
        }
        for (me, m, o, k), (st, ca, n) in sorted(sums.items())
    ]


COLUMNS = [
    ("model", "모델"),
    ("method", "방식"),
    ("origin", "세트"),
    ("rounds", "유효 라운드"),
    ("stage_acc", "단계 정확도"),
    ("cause_acc", "원인 정확도"),
    ("diag_failed", "진단 실패"),
    ("false_pos", "오탐"),
    ("evidence", "근거 유효율"),
    ("evidence_unverified", "수동 확인 필요"),
    ("tool_calls_med", "검사 수(중앙)"),
    ("time_med_s", "시간 중앙(s)"),
    ("time_p95_s", "시간 p95(s)"),
    ("fooled", "오진 라운드"),
    ("repro", "증상 재현"),
    ("reset_ok", "초기화 확인"),
]


def format_table(rows: list[dict]) -> str:
    """지표 행을 고정폭 텍스트 표로 바꾼다."""
    header = " | ".join(t for _, t in COLUMNS)
    lines = [header, "-" * len(header)]
    lines += [" | ".join(str(r[k]) for k, _ in COLUMNS) for r in rows]
    return "\n".join(lines)


# --- 결과 화면

_CSS = """
:root { --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e3e3e8; --card:#f7f7f9;
        --ok:#1a7f37; --bad:#c62828; --warn:#9a6700; --bar:#2a78d6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141416; --fg:#ececf0; --muted:#9a9aa3; --line:#2c2c31; --card:#1c1c20;
          --ok:#4ac26b; --bad:#ff6b6b; --warn:#d4a72c; --bar:#3987e5; } }
body { background:var(--bg); color:var(--fg); font:14px/1.5 system-ui, "Malgun Gothic", sans-serif;
       max-width:1100px; margin:0 auto; padding:24px 16px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:0; }
.muted { color:var(--muted); }
table { border-collapse:collapse; width:100%; margin:8px 0 16px; font-size:13px; }
th, td { border-bottom:1px solid var(--line); padding:6px 8px;
         text-align:left; vertical-align:top; }
.wrap { overflow-x:auto; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px;
        padding:14px 16px; margin:14px 0; }
.ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
pre { white-space:pre-wrap; word-break:break-all; font-size:12px; margin:6px 0 0; }
code { font-size:12px; }
h2.sec { margin:24px 0 4px; }
svg.chart { width:100%; max-width:600px; height:auto; display:block; }
svg.chart text { fill:var(--muted); font-size:11px; }
svg.chart .grid { stroke:var(--line); stroke-width:1; }
svg.chart .bar { fill:var(--bar); }
"""


def _e(x) -> str:
    return html.escape(str(x))


def _verdict_class(v: str) -> str:
    return "ok" if v == "correct" else "muted" if v == "masked" else "bad"


# 근거 판정 → (CSS 클래스, 표시). None은 자동 판정 불가.
_MARK = {True: ("ok", "O"), None: ("warn", "?"), False: ("bad", "X")}
_RESET = {
    True: ("ok", "초기화·정상화 확인"),
    False: ("bad", "초기화 확인 실패"),
    None: ("muted", "실험망 사용 안 함"),
}


def _mark(v) -> str:
    cls, text = _MARK[v]
    return f"<td class='{cls}'>{text}</td>"


def _round_card(r: dict) -> str:
    if r["status"] == Status.PROPOSAL_FAILED:
        return (
            f"<div class='card'><h2>Breaker 제안 실패 · {_e(r['round_id'])}</h2>"
            f"<p class='warn'>{_e(r['invalid_reason'])}</p>"
            f"<p class='muted'>{_e(r['finished_at'])}</p></div>"
        )
    ans = r["answer"]
    head = (
        f"<h2>라운드 {_e(r['round_id'])} · {_e(ans['scenario_id'])} "
        f"<span class='muted'>({_e(r['origin'])}, "
        f"{_e(r['model'])}, 방식 {_e(r['method'])})</span></h2>"
    )
    out = [
        f"<div class='card'>{head}",
        f"<p><b>증상</b> {_e(ans['complaint'])} · "
        f"<b>대상</b> <code>{_e(ans['target_url'])}</code></p>",
    ]

    if r["status"] != Status.SCORED:
        title = "거부" if r["status"] == Status.REJECTED else "무효"
        out.append(f"<p class='warn'><b>{title}</b> {_e(r['invalid_reason'])}</p>")
        for err in (r["validation"] or {}).get("errors", []):
            out.append(f"<div class='warn'>· {_e(err)}</div>")
    else:
        s = r["score"]
        out.append(
            "<div class='wrap'><table><tr>"
            "<th>정답 장애</th><th>매개변수</th><th>판정</th><th>짝지은 진단</th></tr>"
        )
        findings = r["diagnosis"]["findings"]
        for spec, fs in zip(ans["faults"], s["faults"], strict=True):
            matched = findings[fs["finding"]] if fs["finding"] is not None else None
            mtxt = f"{matched['stage']} / {matched['cause']}" if matched else "-"
            params = _e(json.dumps(spec["params"], ensure_ascii=False))
            out.append(
                f"<tr><td>{_e(fs['id'])} {_e(fs['label'])}</td>"
                f"<td><code>{params}</code></td>"
                f"<td class='{_verdict_class(fs['verdict'])}'>"
                f"{_e(VERDICT_KO[fs['verdict']])}</td><td>{_e(mtxt)}</td></tr>"
            )
        for fp in s["false_positives"]:
            out.append(f"<tr><td>-</td><td></td><td class='bad'>오탐</td><td>{_e(fp)}</td></tr>")
        out.append("</table></div>")

        # 창구 기록이 근거다. probes가 없는 옛 기록만 Diagnoser가 적은 trace를 쓴다.
        source = r["probes"] if "probes" in r else r["diagnosis"]["trace"]
        probes = {p["probe_id"]: p for p in source}
        cited = [
            pid for pid in dict.fromkeys(c["probe_id"] for c in s["evidence"]) if pid in probes
        ]
        out.append(
            "<div class='wrap'><table><tr>"
            "<th>진단</th><th>인용</th><th>존재</th><th>지지</th><th>판정 이유</th></tr>"
        )
        for c in s["evidence"]:
            f = findings[c["finding"]]
            ref = (
                f"<a href='#r{r['round_id']}-{c['probe_id']}'><code>{_e(c['ref'])}</code></a>"
                if c["probe_id"] in probes
                else f"<code>{_e(c['ref'] or '-')}</code>"
            )
            out.append(
                f"<tr><td>{_e(f['stage'])} / {_e(f['cause'])}</td>"
                f"<td>{ref} {_e(c['claim'])}</td>"
                f"{_mark(c['exists'])}{_mark(c['supported'])}"
                f"<td class='muted'>{_e(c['reason'])}</td></tr>"
            )
        out.append("</table></div>")
        # 원본 출력은 인용 횟수와 무관하게 검사마다 한 번만 싣는다.
        for pid in cited:
            p = probes[pid]
            out.append(
                f"<details id='r{r['round_id']}-{pid}'>"
                f"<summary>{_e(pid)} · {_e(p['tool'])} 원본 출력</summary>"
                f"<pre>{_e(p['output'])}</pre></details>"
            )
        viol = " · ".join(s["limit_violations"])
        viol_html = f" · <span class=bad>{_e(viol)}</span>" if viol else ""
        out.append(
            f"<p class='muted'>검사 {s['tool_calls']}회 · "
            f"LLM 호출 {s['llm_calls']}회 · 토큰 {s['tokens']} · "
            f"심판 측정 {s['elapsed_s']}s{viol_html}</p>"
        )

    cls, text = _RESET[r["reset_verified"]]
    out.append(
        f"<p class='muted'><span class='{cls}'>{text}</span> · {_e(r['started_at'])}</p></div>"
    )
    return "\n".join(out)


def _html_table(headers: list[str], body: list[list[str]]) -> str:
    """머리글과 이미 이스케이프한 칸으로 표를 만든다."""
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    rows = "".join("<tr>" + "".join(body_row) + "</tr>" for body_row in body)
    return f"<div class='wrap'><table><tr>{head}</tr>{rows}</table></div>"


def misdiagnosis_section(records: list[dict]) -> str:
    """장애별 판정 횟수와 오탐 라벨 표. 채점한 라운드가 없으면 빈 문자열."""
    rows = misdiagnosis_rows(records)
    if not rows:
        return ""

    def count(v: str, n: int) -> str:
        cls = "" if n == 0 else "ok" if v == "correct" else "muted" if v == "masked" else "bad"
        return f"<td class='{cls}'>{n}</td>"

    body = [
        [f"<td>{_e(r[k])}</td>" for k in ("model", "method", "origin", "fault")]
        + [count(v, r[v]) for v in VERDICT_KO]
        for r in rows
    ]
    out = [
        "<h2 class='sec'>오진 유형</h2>",
        "<p class='muted'>채점한 라운드와 진단 실패 라운드에서 정답 장애마다 받은 판정 횟수</p>",
        _html_table(["모델", "방식", "세트", "정답 장애", *VERDICT_KO.values()], body),
    ]
    fps = false_positive_rows(records)
    if fps:
        body = [
            [f"<td>{_e(r[k])}</td>" for k in ("model", "method", "origin", "label")]
            + [f"<td class='bad'>{r['count']}</td>"]
            for r in fps
        ]
        out.append(_html_table(["모델", "방식", "세트", "오탐(정답에 없는 진단)", "횟수"], body))
    return "\n".join(out)


# 반복 측정 그래프 치수(px, viewBox 기준)
_LABEL_W, _PLOT_W, _BAR_H, _ROW_H, _GROUP_GAP, _AXIS_H = 120, 440, 10, 18, 8, 22


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """기준선(왼쪽)은 각지고 값 쪽 끝만 둥근 가로 막대."""
    r = min(r, w, h / 2)
    return (
        f"M{x:.1f},{y:.1f}h{w - r:.1f}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r:.1f}"
        f"a{r},{r} 0 0 1 -{r},{r}h-{w - r:.1f}z"
    )


def _repeat_chart(rows: list[dict], metric: str, title: str) -> str:
    """방식·회차마다 가로 막대 하나. 값은 마우스를 올리면 보인다."""
    marks, y, prev = [], 0, None
    for r in rows:
        group = (r["model"], r["method"], r["origin"])
        if prev is not None and group != prev:
            y += _GROUP_GAP
        prev = group
        n, d = r[metric]
        label = f"{r['method']} · {r['repeat']}회"
        tip = f"{r['model']} · 방식 {r['method']} · {r['repeat']}회차: {title} {ratio(n, d)}"
        bar = (
            f"<path class='bar' d='{_bar_path(_LABEL_W, y + 4, _PLOT_W * n / d, _BAR_H)}'/>"
            if d and n
            else ""
        )
        marks.append(
            f"<g><title>{_e(tip)}</title>"
            f"<rect x='0' y='{y}' width='{_LABEL_W + _PLOT_W}' height='{_ROW_H}' "
            f"fill='transparent'/>"
            f"<text x='{_LABEL_W - 8}' y='{y + 13}' text-anchor='end'>{_e(label)}</text>"
            f"{bar}</g>"
        )
        y += _ROW_H
    grid = "".join(
        f"<line class='grid' x1='{_LABEL_W + _PLOT_W * p}' x2='{_LABEL_W + _PLOT_W * p}' "
        f"y1='0' y2='{y}'/>"
        f"<text x='{_LABEL_W + _PLOT_W * p}' y='{y + 14}' text-anchor='middle'>{p:.0%}</text>"
        for p in (0, 0.5, 1)
    )
    width, height = _LABEL_W + _PLOT_W + 20, y + _AXIS_H
    return (
        f"<p><b>{_e(title)}</b></p>"
        f"<svg class='chart' viewBox='0 0 {width} {height}' role='img' "
        f"aria-label='{_e(title)} 반복 측정'>{grid}{''.join(marks)}</svg>"
    )


def repeat_section(records: list[dict]) -> str:
    """반복 회차별 정확도 그래프와 표. 회차가 2 이상인 기록에서만 그린다."""
    rows = repeat_rows(records)
    if max((r["repeat"] for r in rows), default=0) < 2:
        return ""
    body = [
        [f"<td>{_e(r[k])}</td>" for k in ("model", "method", "origin", "repeat")]
        + [f"<td>{ratio(*r['stage'])}</td><td>{ratio(*r['cause'])}</td>"]
        for r in rows
    ]
    return "\n".join(
        [
            "<h2 class='sec'>반복 측정</h2>",
            "<p class='muted'>같은 시나리오를 다시 돌린 회차별 정확도. "
            "회차가 바뀌어도 값이 비슷하면 결과가 안정적이다.</p>",
            _repeat_chart(rows, "cause", "원인 정확도"),
            _repeat_chart(rows, "stage", "단계 정확도"),
            _html_table(["모델", "방식", "세트", "회차", "단계 정확도", "원인 정확도"], body),
        ]
    )


def render_html(records: list[dict], rows: list[dict] | None = None) -> str:
    """라운드 기록 전체를 하나의 HTML 페이지로 만든다."""
    rows = summarize(records) if rows is None else rows
    rejected = sum(r["status"] == Status.REJECTED for r in records)
    failed = sum(r["status"] == Status.PROPOSAL_FAILED for r in records)
    failure_note = f" · Breaker 제안 실패 {failed}개" if failed else ""
    table = [
        "<div class='wrap'><table><tr>"
        + "".join(f"<th>{_e(t)}</th>" for _, t in COLUMNS)
        + "</tr>"
    ]
    table += ["<tr>" + "".join(f"<td>{_e(r[k])}</td>" for k, _ in COLUMNS) + "</tr>" for r in rows]
    table.append("</table></div>")
    cards = "\n".join(_round_card(r) for r in records)
    return (
        f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>NetRunner 라운드 결과</title><style>{_CSS}</style></head><body>"
        f"<h1>NetRunner 라운드 결과</h1>"
        f"<p class='muted'>측정 위치: 클라이언트 컨테이너 · 라운드 {len(records) - failed}개 · "
        f"검증에서 거부된 제안 {rejected}개{failure_note}</p>"
        f"{''.join(table)}{misdiagnosis_section(records)}{repeat_section(records)}"
        f"<h2 class='sec'>라운드별 결과</h2>{cards}</body></html>"
    )
