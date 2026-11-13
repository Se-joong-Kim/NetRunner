"""python -m netrunner {validate|demo|run|report|lab-check} 명령 진입점.

객체를 조립하는 가장 바깥 층이다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .contracts import Scenario, read_json
from .diagnoser import ReplayDiagnoser
from .docker_lab import DockerLab
from .lab import Lab, MockLab, ReplayRunner
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


# 심판에 꽂을 실험망 구현. 새 실험망은 여기에 한 줄 추가한다.
LAB_IMPLS: dict[str, Callable[[Path], Lab]] = {
    "mock": lambda _config: MockLab(),
    "docker": DockerLab,
}


# 실험망 자체가 검사 도구(run_probe)를 실행하는 구현. 여기 없는 구현은 저장된
# 진단의 검사 출력을 재생한다. DockerLab.run_probe가 생기면 "docker"를 넣는다.
PROBES_FROM_LAB: frozenset[str] = frozenset()


def make_lab(impl: str, config: str | Path) -> Lab:
    """이름으로 실험망 구현을 만든다. config는 실험망 주소 JSON 경로다."""
    return LAB_IMPLS[impl](Path(config))


def run_fixed_rounds(
    referee: Referee, scenario_files: list[Path], repeat: int = 1, replay_probes: bool = True
) -> list[dict]:
    """고정 시나리오를 저장된 진단과 짝지어 repeat번 돌린다.

    replay_probes면 라운드마다 그 진단의 저장 검사 출력을 검사 실행기로
    꽂는다. 정상화가 확인되지 않으면 오염된 실험망이므로 그 자리에서
    멈춘다(referee.run_match와 같은 규칙).
    """
    records: list[dict] = []
    for _ in range(repeat):
        for sf in scenario_files:
            # 검증에서 거부되는 시나리오는 진단 파일이 없어도 된다.
            df = sf.with_name(sf.name.replace(".scenario.", ".diagnosis."))
            saved = [read_json(df)] if df.exists() else []
            referee.diagnoser = ReplayDiagnoser(saved)
            if replay_probes:
                referee.runner = ReplayRunner.from_diagnoses(saved)
            records.append(referee.run_round(Scenario.from_dict(read_json(sf))))
            if records[-1]["reset_verified"] is False:
                return records
    return records


def cmd_run(args) -> int:
    """고정 세트를 고른 실험망에서 심판으로 반복 실행하고 지표를 낸다.

    진단은 저장된 결과를 재생하므로 AI 진단 성능이 아니라 실험망의
    재현·정상화와 심판 경로를 확인하는 용도다.
    """
    d = Path(args.dir)
    scenario_files = sorted(d.glob("*.scenario.json"))
    if not scenario_files or args.repeat < 1:
        print(f"{d}에 *.scenario.json이 없거나 반복 횟수가 1보다 작음", file=sys.stderr)
        return 1
    out = Path(args.out)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    records_path = out / f"rounds-{d.name}-{args.impl}-{stamp}.jsonl"
    lab = make_lab(args.impl, args.lab)
    probes_from_lab = args.impl in PROBES_FROM_LAB
    referee = Referee(
        lab,
        ReplayDiagnoser([]),
        LabInfo.load(args.lab),
        records_path=records_path,
        runner=lab if probes_from_lab else None,
    )
    records = run_fixed_rounds(
        referee, scenario_files, args.repeat, replay_probes=not probes_from_lab
    )
    stopped = records[-1]["reset_verified"] is False
    if stopped:
        print("정상화 미확인: 나머지 라운드를 중단함", file=sys.stderr)

    rows = summarize(records)
    print(format_table(rows))
    html_path = records_path.with_name(records_path.stem.replace("rounds-", "report-") + ".html")
    html_path.write_text(render_html(records, rows), encoding="utf-8")
    print(f"\n기록: {records_path}\n결과 화면: {html_path}")
    return 1 if stopped else 0


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

    p = sub.add_parser("demo", help="고정 입력으로 라운드를 모의 실행(run --impl mock과 같음)")
    p.add_argument("--dir", default=str(ROOT / "fixtures" / "first_integration"))
    p.add_argument("--out", default=str(ROOT / "runs"))
    p.set_defaults(func=cmd_run, impl="mock", repeat=1)

    p = sub.add_parser("run", help="고정 세트를 고른 실험망에서 심판으로 반복 실행")
    p.add_argument("--impl", choices=sorted(LAB_IMPLS), default="docker", help="실험망 구현")
    p.add_argument("--dir", default=str(ROOT / "fixtures" / "first_integration"))
    p.add_argument("--out", default=str(ROOT / "runs"))
    p.add_argument("--repeat", type=int, default=1)
    p.set_defaults(func=cmd_run)

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
