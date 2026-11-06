"""python -m netrunner {validate|demo|report}"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .contracts import Scenario, read_json
from .diagnoser import ReplayDiagnoser
from .lab import MockLab
from .referee import Referee
from .report import format_table, load_records, render_html, summarize
from .validate import LabInfo, validate

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LAB = ROOT / "config" / "lab.json"


def cmd_validate(args) -> int:
    lab = LabInfo.load(args.lab)
    bad = 0
    for p in args.files:
        v = validate(Scenario.from_dict(read_json(Path(p))), lab)
        print(f"{'OK ' if v.ok else 'NG '} {p}")
        for e in v.errors:
            print(f"     오류: {e}")
        for w in v.warnings:
            print(f"     주의: {w}")
        bad += not v.ok
    return 1 if bad else 0


def cmd_demo(args) -> int:
    """고정 입력(시나리오 + 저장된 진단)으로 모의 실험망에서 라운드를 끝까지 돌린다."""
    d = Path(args.dir)
    scenario_files = sorted(d.glob("*.scenario.json"))
    if not scenario_files:
        print(f"{d}에 *.scenario.json이 없음", file=sys.stderr)
        return 1
    out = Path(args.out)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    records_path = out / f"rounds-{d.name}-{stamp}.jsonl"
    referee = Referee(MockLab(), ReplayDiagnoser([]), LabInfo.load(args.lab), records_path=records_path)
    records = []
    for sf in scenario_files:
        # 검증에서 거부되는 시나리오는 진단 파일이 없어도 된다. 라운드마다 짝을 맞춰 재생한다.
        df = sf.with_name(sf.name.replace(".scenario.", ".diagnosis."))
        referee.diagnoser = ReplayDiagnoser([read_json(df)] if df.exists() else [])
        records.append(referee.run_round(Scenario.from_dict(read_json(sf))))

    rows = summarize(records)
    print(format_table(rows))
    html_path = out / f"report-{d.name}-{stamp}.html"
    html_path.write_text(render_html(records, rows), encoding="utf-8")
    print(f"\n기록: {records_path}\n결과 화면: {html_path}")
    return 0


def cmd_report(args) -> int:
    records = load_records(args.records)
    rows = summarize(records)
    print(format_table(rows))
    if args.html:
        Path(args.html).write_text(render_html(records, rows), encoding="utf-8")
        print(f"\n결과 화면: {args.html}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="netrunner", description="NetRunner 심판·통합 도구")
    ap.add_argument("--lab", default=str(DEFAULT_LAB), help="실험망 주소 정보 JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="시나리오 JSON을 심판 규칙으로 검사")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("demo", help="고정 입력으로 라운드를 모의 실행")
    p.add_argument("--dir", default=str(ROOT / "fixtures" / "first_integration"))
    p.add_argument("--out", default=str(ROOT / "runs"))
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("report", help="라운드 기록으로 지표와 결과 화면 생성")
    p.add_argument("records")
    p.add_argument("--html")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
