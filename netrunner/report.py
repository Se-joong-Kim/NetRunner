"""라운드 기록(JSONL)으로 비교 지표와 결과 화면을 만든다.

비율은 설계안 9.3절대로 "23/30 (77%)"처럼 개수와 함께 적고, 모델·방식별로
나눈다.
"""

from __future__ import annotations

import html
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

VERDICT_KO = {
    "correct": "정답",
    "cause_wrong": "원인 틀림",
    "stage_wrong": "단계 틀림",
    "missed": "놓침",
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


def summarize(records: list[dict]) -> list[dict]:
    """모델·방식·세트별 지표.

    검증에서 거부된 제안(rejected)은 Diagnoser가 돌지 않았으므로 뺀다.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        if r["status"] != "rejected":
            groups[(r["model"], r["method"], r["origin"])].append(r)

    rows = []
    for (model, method, origin), rs in sorted(groups.items()):
        sc = [r["score"] for r in rs if r["status"] == "scored"]
        times = [s["elapsed_s"] for s in sc]
        tools = [s["tool_calls"] for s in sc]
        faults = sum(s["scored_faults"] for s in sc)
        fooled = sum(
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
                "false_pos": sum(len(s["false_positives"]) for s in sc),
                "evidence": ratio(
                    sum(s["evidence_valid"] for s in sc), sum(s["evidence_total"] for s in sc)
                ),
                "evidence_unverified": sum(s["evidence_unverified"] for s in sc),
                "tool_calls_med": statistics.median(tools) if tools else "-",
                "time_med_s": round(statistics.median(times), 2) if times else "-",
                "time_p95_s": round(p95(times), 2) if times else "-",
                "fooled": ratio(fooled, len(sc)),
                # 실험망을 쓴 라운드 중 주입 뒤 증상까지 확인된 비율(11주차 완료 기준)
                "repro": ratio(sum(_reproduced(r) for r in rs), len(rs)),
                "reset_ok": ratio(
                    sum(r["reset_verified"] is True for r in rs),
                    sum(r["reset_verified"] is not None for r in rs),
                ),
            }
        )
    return rows


COLUMNS = [
    ("model", "모델"),
    ("method", "방식"),
    ("origin", "세트"),
    ("rounds", "유효 라운드"),
    ("stage_acc", "단계 정확도"),
    ("cause_acc", "원인 정확도"),
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
        --ok:#1a7f37; --bad:#c62828; --warn:#9a6700; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141416; --fg:#ececf0; --muted:#9a9aa3; --line:#2c2c31; --card:#1c1c20;
          --ok:#4ac26b; --bad:#ff6b6b; --warn:#d4a72c; } }
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

    if r["status"] != "scored":
        title = "거부" if r["status"] == "rejected" else "무효"
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

        probes = {p["probe_id"]: p for p in r["diagnosis"]["trace"]}
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


def render_html(records: list[dict], rows: list[dict] | None = None) -> str:
    """라운드 기록 전체를 하나의 HTML 페이지로 만든다."""
    rows = summarize(records) if rows is None else rows
    rejected = sum(r["status"] == "rejected" for r in records)
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
        f"<p class='muted'>측정 위치: 클라이언트 컨테이너 · 라운드 {len(records)}개 · "
        f"검증에서 거부된 제안 {rejected}개</p>"
        f"{''.join(table)}{cards}</body></html>"
    )
