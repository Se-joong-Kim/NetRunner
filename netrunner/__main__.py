"""python -m netrunner {validate|demo|report} 명령 진입점.

객체를 조립하는 가장 바깥 층이다.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .contracts import Scenario, read_json
from .diagnoser import ReplayDiagnoser
from .docker_lab import DockerLab
from .lab import MockLab
from .referee import Referee
from .report import format_table, load_records, render_html, summarize
from .validate import LabInfo, validate

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LAB = ROOT / "config" / "lab.json"


def cmd_validate(args) -> int:
    """시나리오 파일들을 검사하고 오류가 있으면 1을 돌려준다."""
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
    referee = Referee(
        MockLab(), ReplayDiagnoser([]), LabInfo.load(args.lab), records_path=records_path
    )
    records = []
    for sf in scenario_files:
        # 검증에서 거부되는 시나리오는 진단 파일이 없어도 된다. 라운드마다 짝을 맞춰 재생한다.
        df = sf.with_name(sf.name.replace(".scenario.", ".diagnosis."))
        referee.diagnoser = ReplayDiagnoser([read_json(df)] if df.exists() else [])
        records.append(referee.run_round(Scenario.from_dict(read_json(sf))))
        # 정상화 미확인이면 다음 라운드를 돌리지 않는다(referee.run_match와 같은 규칙).
        if records[-1]["reset_verified"] is False:
            print("정상화 미확인: 나머지 시나리오를 중단함", file=sys.stderr)
            break

    rows = summarize(records)
    print(format_table(rows))
    html_path = out / f"report-{d.name}-{stamp}.html"
    html_path.write_text(render_html(records, rows), encoding="utf-8")
    print(f"\n기록: {records_path}\n결과 화면: {html_path}")
    return 1 if records[-1]["reset_verified"] is False else 0


def cmd_report(args) -> int:
    """라운드 기록으로 지표 표와 선택적으로 HTML 화면을 만든다."""
    records = load_records(args.records)
    rows = summarize(records)
    print(format_table(rows))
    if args.html:
        Path(args.html).write_text(render_html(records, rows), encoding="utf-8")
        print(f"\n결과 화면: {args.html}")
    return 0


def cmd_lab_check(args) -> int:
    """실제 실험망만 검사한다. 저장된 진단 결과를 실제 진단으로 기록하지 않는다."""
    info = LabInfo.load(args.lab)
    lab = DockerLab(args.lab)
    files = sorted(Path(args.dir).glob("*.scenario.json"))
    if not files or args.repeat < 1:
        print("시나리오 파일이 없거나 반복 횟수가 1보다 작음", file=sys.stderr)
        return 1
    scenarios = [Scenario.from_dict(read_json(p)) for p in files]
    for scenario in scenarios:
        result = validate(scenario, info)
        if not result.ok or len(scenario.faults) != 1:
            print(
                f"{scenario.scenario_id}: 시나리오 오류 {result.errors} (단일 장애만 지원)",
                file=sys.stderr,
            )
            return 1
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"lab-check-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.jsonl"
    for _ in range(args.repeat):
        for scenario in scenarios:
            steps = {}
            try:
                initial = lab.health_check(scenario.target_url)
                if not initial.ok:
                    steps["health_check_initial"] = asdict(initial)
                    before = lab.reset()
                    steps["reset_before"] = asdict(before)
                    if before.ok:
                        initial = lab.health_check(scenario.target_url)
                steps["health_check"] = asdict(initial)
                if initial.ok:
                    injection = lab.inject(scenario.faults[0])
                    steps["inject"] = asdict(injection)
                    if injection.ok:
                        steps["verify_symptom"] = asdict(lab.verify_symptom(scenario))
            finally:
                steps["reset"] = asdict(lab.reset())
                steps["health_check_after"] = asdict(lab.health_check(scenario.target_url))
                with path.open("a", encoding="utf-8") as fp:
                    fp.write(
                        json.dumps(
                            {"scenario_id": scenario.scenario_id, "steps": steps},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            ok = all(
                steps.get(name, {}).get("ok", False)
                for name in (
                    "health_check",
                    "inject",
                    "verify_symptom",
                    "reset",
                    "health_check_after",
                )
            )
            print(f"{scenario.scenario_id}: {'성공' if ok else '실패'}")
            if not ok:
                print(f"기록: {path}")
                return 1
    print(f"기록: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """명령줄 인자를 해석해 하위 명령을 실행한다."""
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

    p = sub.add_parser("lab-check", help="실제 실험망의 장애 재현과 복구 확인")
    p.add_argument("--dir", default=str(ROOT / "fixtures" / "first_integration"))
    p.add_argument("--out", default=str(ROOT / "runs"))
    p.add_argument("--repeat", type=int, default=1)
    p.set_defaults(func=cmd_lab_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
