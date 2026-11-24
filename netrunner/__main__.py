"""python -m netrunner {validate|demo|run|report|lab-check|breaker-propose|match} 명령 진입점.

객체를 조립하는 가장 바깥 층이다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .breaker import BreakerError, LLMBreaker, ProposalStats
from .breaker_log import CallJournal
from .breaker_strategy import signature
from .contracts import BreakerFeedback, Diagnosis, RoundBrief, Scenario, read_json
from .diagnoser import Diagnoser, GlmDiagnoser, ReplayDiagnoser
from .docker_lab import DockerLab
from .glm_baseline import GlmBaselineDiagnoser
from .glm_client import DEFAULT_MODEL, OpencodeComplete
from .lab import Lab, MockLab, ReplayRunner
from .records import default_runs_dir, ensure_private_dir, open_private, write_private_text
from .referee import Referee, feedback_for
from .report import (
    format_breaker_summary,
    format_table,
    load_records,
    render_html,
    summarize,
    summarize_breaker,
)
from .rule_diagnoser import RuleDiagnoser
from .toolbox import Toolbox
from .validate import LabInfo, lab_url_error, validate

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LAB = ROOT / "config" / "lab.json"
DEFAULT_SANDBOX = ROOT / "config" / "opencode_sandbox.json"


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
# 진단의 검사 출력을 재생한다.
PROBES_FROM_LAB: frozenset[str] = frozenset({"docker"})


class NoDiagnosis:
    """원인을 하나도 답하지 않는 자리표시 진단기.

    실제 Diagnoser가 main에 들어오기 전에 match로 Breaker 쪽 숫자(검증 통과,
    재현, 정상화)를 재려고 둔다. 기록의 model이 "none"이라 진단 지표와
    섞이지 않는다. Diagnoser 담당 파일과 겹치지 않게 진입점에 둔다.
    """

    model = "none"
    method = "B"  # 검사 도구를 쓰지 않는다

    def diagnose(self, brief: RoundBrief, tools: Toolbox) -> Diagnosis:
        """빈 진단을 돌려준다."""
        return Diagnosis(brief.round_id, self.model, self.method, [])


# match와 run에 꽂을 진단기. 인자는 명령줄 인자다. 새 Diagnoser는 여기에 한 줄 추가한다.
# 설계안 9.2절의 방식: A는 rules, B·C·C-1·C-2는 GLM이다.
GLM_B = "glm-b"
GLM_C = "glm-c"
GLM_C1 = "glm-c1"
GLM_C2 = "glm-c2"
RULES = "rules"
DIAGNOSER_IMPLS: dict[str, Callable[[argparse.Namespace], Diagnoser]] = {
    "none": lambda _args: NoDiagnosis(),
    GLM_B: lambda args: GlmBaselineDiagnoser.from_opencode(args.opencode, model=args.model),
    GLM_C: lambda args: GlmDiagnoser.from_opencode(args.opencode, model=args.model),
    GLM_C1: lambda args: GlmDiagnoser.from_opencode(args.opencode, model=args.model, method="C-1"),
    GLM_C2: lambda args: GlmDiagnoser.from_opencode(args.opencode, model=args.model, method="C-2"),
    RULES: lambda _args: RuleDiagnoser(),
}
# 실험망이 검사를 실행해야 진단할 수 있는 진단기. B는 도구를 쓰지 않지만
# 심판이 입력으로 넣을 curl 오류 한 줄을 실험망에서 얻는다.
NEEDS_PROBES: frozenset[str] = frozenset({GLM_B, GLM_C, GLM_C1, GLM_C2, RULES})
# run에서 저장된 진단을 재생하는 모드의 이름. 다른 진단기와 섞지 않는다.
REPLAY = "replay"


def _diagnoser_needs_runner(names: list[str], impl: str) -> bool:
    needing = sorted(NEEDS_PROBES.intersection(names))
    if not needing or impl in PROBES_FROM_LAB:
        return False
    print(
        f"{', '.join(needing)}: {impl} 실험망에 검사 실행기가 연결되지 않았습니다.",
        file=sys.stderr,
    )
    return True


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


def run_method_rounds(
    referee: Referee, scenario_files: list[Path], diagnosers: list[Diagnoser], repeat: int = 1
) -> list[dict]:
    """고정 시나리오를 진단기(방식)마다 repeat번 돌린다.

    반복 → 시나리오 → 진단기 순서로 섞는다. 실험망 상태가 시간에 따라
    바뀌어도 한 방식에만 몰리지 않게 하려는 것이다. 정상화가 확인되지
    않으면 그 자리에서 멈춘다.
    """
    scenarios = [Scenario.from_dict(read_json(sf)) for sf in scenario_files]
    records: list[dict] = []
    for _ in range(repeat):
        for scenario in scenarios:
            for diagnoser in diagnosers:
                referee.diagnoser = diagnoser
                records.append(referee.run_round(scenario))
                if records[-1]["reset_verified"] is False:
                    return records
    return records


def cmd_run(args) -> int:
    """고정 세트를 고른 실험망에서 심판으로 반복 실행하고 지표를 낸다.

    --diagnoser가 없으면(replay) 저장된 진단을 재생한다. 이때는 AI 진단
    성능이 아니라 실험망의 재현·정상화와 심판 경로를 확인하는 용도다.
    --diagnoser로 진단기를 여럿 주면 고정 세트를 진단기(방식)마다 돌려
    방식별 지표를 낸다.
    """
    names = list(dict.fromkeys(args.diagnoser or [REPLAY]))
    if REPLAY in names and len(names) > 1:
        print("replay(저장된 진단 재생)는 다른 진단기와 함께 쓸 수 없음", file=sys.stderr)
        return 1
    if _diagnoser_needs_runner(names, args.impl):
        return 1
    d = Path(args.dir)
    scenario_files = sorted(d.glob("*.scenario.json"))
    if not scenario_files or args.repeat < 1:
        print(f"{d}에 *.scenario.json이 없거나 반복 횟수가 1보다 작음", file=sys.stderr)
        return 1
    out = ensure_private_dir(args.out)
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
    if names == [REPLAY]:
        records = run_fixed_rounds(
            referee, scenario_files, args.repeat, replay_probes=not probes_from_lab
        )
    else:
        diagnosers = [DIAGNOSER_IMPLS[n](args) for n in names]
        records = run_method_rounds(referee, scenario_files, diagnosers, args.repeat)
    stopped = records[-1]["reset_verified"] is False
    if stopped:
        print("정상화 미확인: 나머지 라운드를 중단함", file=sys.stderr)

    rows = summarize(records)
    print(format_table(rows))
    html_path = _save_report(records_path, records, rows)
    print(f"\n기록: {records_path}\n결과 화면: {html_path}")
    return 1 if stopped else 0


def _save_report(records_path: Path, records: list[dict], rows: list[dict]) -> Path:
    """라운드 기록 옆에 report-*.html 결과 화면을 쓰고 그 경로를 돌려준다."""
    html_path = records_path.with_name(records_path.stem.replace("rounds-", "report-") + ".html")
    write_private_text(html_path, render_html(records, rows))
    return html_path


def cmd_report(args) -> int:
    """라운드 기록으로 지표 표와 선택적으로 HTML 화면을 만든다."""
    records = load_records(args.records)
    rows = summarize(records)
    print(format_table(rows))
    if args.html:
        write_private_text(args.html, render_html(records, rows))
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
    output = ensure_private_dir(args.out)
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
                with open_private(path) as fp:
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


def format_proposal_summary(
    stats: ProposalStats, requested: int, accepted: int, distinct: int
) -> str:
    """제안 품질 지표를 사람이 읽을 줄로 만든다. 채택률은 채택 수를 호출 수로 나눈다."""
    calls = max(stats.calls, 1)
    return "\n".join(
        [
            f"제안 요청 {requested}회, 채택 {accepted}회, 서로 다른 조합 {distinct}개",
            f"모델 호출 {stats.calls}회(제안당 평균 {stats.calls / requested:.2f}회)",
            f"채택률(채택/호출) {stats.accepted / calls:.0%}, "
            f"형식 오류율 {stats.unparsable / calls:.0%}",
            f"거부: 형식 {stats.unparsable}, 중복 {stats.duplicate}, 검증 {stats.invalid}, "
            f"동시 장애 초과 {stats.over_limit}, "
            f"가려짐 {stats.masked}, API 오류 {stats.api_errors}",
        ]
    )


def _as_valid_round(sc: Scenario) -> BreakerFeedback:
    """채택한 제안을 판정 없는 유효 라운드로 바꾼다. 중복 판정과 프롬프트용이다."""
    return BreakerFeedback(
        scenario_id=sc.scenario_id, faults=sc.faults, complaint=sc.complaint, valid=True
    )


def cmd_breaker_propose(args) -> int:
    """GLM으로 시나리오를 n번 제안받아 검사 통과율을 센다. 실험망은 건드리지 않는다.

    모델 호출마다 원문과 판정을 breaker-<시각>.jsonl에 --out 폴더로 남긴다.
    채택된 시나리오는 하나씩 breaker-propose-<시각>-NN.scenario.json 파일로
    채택 즉시 저장한다(뒤의 호출이 실패해도 앞의 것은 남는다). 지난 라운드 기록
    (--history)이 있으면 그 피드백을 제안 프롬프트에 넣는다. 한 건도 채택되지
    않으면 1을 돌려준다. 채택한 제안은 유효 라운드로 보고 다음 제안의 history에
    넣는다. 그래서 같은 조합은 중복으로 거부되고, 실제 매치와 같은 조건에서 다양성을
    잴 수 있다.
    """
    if args.n < 1:
        print("--n은 1 이상이어야 함", file=sys.stderr)
        return 1
    probe = Scenario(scenario_id="", target_url=args.target, faults=[], complaint="")
    try:
        _ = (probe.domain, probe.port)
    except ValueError:
        print(f"대상 URL이 잘못됨: {args.target}", file=sys.stderr)
        return 1
    info = LabInfo.load(args.lab)
    history = []
    if args.history:
        history = [fb for rec in load_records(args.history) if (fb := feedback_for(rec))]
    out = ensure_private_dir(args.out)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    journal = out / f"breaker-{stamp}.jsonl"
    complete = OpencodeComplete(
        binary=args.opencode, root=ROOT, config=DEFAULT_SANDBOX, model=args.model
    )
    breaker = LLMBreaker(
        complete, info, args.target, max_faults=args.max_faults, record=CallJournal(journal)
    )
    accepted: list[Scenario] = []
    stem = f"breaker-propose-{stamp}"
    for _ in range(args.n):
        try:
            sc = breaker.propose(history)
        except BreakerError as e:
            print(f"제안 실패: {e}", file=sys.stderr)
            continue
        accepted.append(sc)
        history.append(_as_valid_round(sc))
        path = out / f"{stem}-{len(accepted):02d}.scenario.json"
        write_private_text(path, json.dumps(asdict(sc), ensure_ascii=False, indent=2) + "\n")
    distinct = len({signature(sc.faults, sc.domain) for sc in accepted})
    print(format_proposal_summary(breaker.stats, args.n, len(accepted), distinct))
    print(f"\n호출 기록: {journal}")
    if not accepted:
        return 1
    print(
        f"채택 시나리오: {out / stem}-*.scenario.json ({len(accepted)}개, "
        "netrunner validate로 검사 가능)"
    )
    return 0


def cmd_match(args) -> int:
    """Breaker 제안을 심판으로 돌린다(Breaker 대결).

    라운드마다 Breaker가 GLM으로 시나리오를 내고, 심판이 검증, 주입, 재현
    확인, 진단, 채점, 정상화 확인을 한다. 진단기는 --diagnoser로 고른다.
    Breaker가 제안에 실패하거나 정상화가 확인되지 않으면 그 자리에서
    끝내고 1을 돌려준다.
    """
    if args.rounds < 1:
        print("--rounds는 1 이상이어야 함", file=sys.stderr)
        return 1
    if _diagnoser_needs_runner([args.diagnoser], args.impl):
        return 1
    info = LabInfo.load(args.lab)
    url_err = lab_url_error(args.target, info)
    if url_err:
        print(f"대상 URL이 잘못됨: {url_err}", file=sys.stderr)
        return 1
    out = ensure_private_dir(args.out)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    records_path = out / f"rounds-match-{args.impl}-{stamp}.jsonl"
    journal = out / f"breaker-{stamp}.jsonl"
    complete = OpencodeComplete(
        binary=args.opencode, root=ROOT, config=DEFAULT_SANDBOX, model=args.model
    )
    breaker = LLMBreaker(
        complete, info, args.target, max_faults=args.max_faults, record=CallJournal(journal)
    )
    lab = make_lab(args.impl, args.lab)
    referee = Referee(
        lab,
        DIAGNOSER_IMPLS[args.diagnoser](args),
        info,
        records_path=records_path,
        runner=lab if args.impl in PROBES_FROM_LAB else None,
    )
    records = referee.run_match(breaker, args.rounds)

    rows = summarize(records)
    print(format_table(rows))
    stats = summarize_breaker(records)
    print("\n" + format_breaker_summary(stats))
    html_path = _save_report(records_path, records, rows)
    print(f"\n기록: {records_path}\nBreaker 호출 기록: {journal}\n결과 화면: {html_path}")
    stopped = any(r["reset_verified"] is False for r in records)
    if stopped:
        print("정상화 미확인: 나머지 라운드를 중단함", file=sys.stderr)
    return 1 if stopped or stats["proposal_failed"] else 0


def build_parser() -> argparse.ArgumentParser:
    """명령줄 해석기를 만든다. 기록 폴더 기본값은 저장소 밖이다(#21)."""
    runs = str(default_runs_dir())
    ap = argparse.ArgumentParser(prog="netrunner", description="NetRunner 심판·통합 도구")
    ap.add_argument("--lab", default=str(DEFAULT_LAB), help="실험망 주소 정보 JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="시나리오 JSON을 심판 규칙으로 검사")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("demo", help="고정 입력으로 라운드를 모의 실행(run --impl mock과 같음)")
    p.add_argument("--dir", default=str(ROOT / "fixtures" / "first_integration"))
    p.add_argument("--out", default=runs, help="기록 폴더(기본: ~/netrunner-private/runs)")
    p.set_defaults(func=cmd_run, impl="mock", repeat=1, diagnoser=None)

    p = sub.add_parser("run", help="고정 세트를 고른 실험망에서 심판으로 반복 실행")
    p.add_argument("--impl", choices=sorted(LAB_IMPLS), default="docker", help="실험망 구현")
    p.add_argument("--dir", default=str(ROOT / "fixtures" / "first_integration"))
    p.add_argument("--out", default=runs, help="기록 폴더(기본: ~/netrunner-private/runs)")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument(
        "--diagnoser",
        action="append",
        choices=[REPLAY, *sorted(DIAGNOSER_IMPLS)],
        help="진단기. 여러 번 주면 방식별로 돈다(기본 replay: 저장된 진단 재생)",
    )
    _add_glm_args(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="라운드 기록으로 지표와 결과 화면 생성")
    p.add_argument("records")
    p.add_argument("--html", help="결과 화면 파일(정답이 들어 있으니 저장소 밖에 둔다)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("lab-check", help="실제 실험망의 장애 재현과 복구 확인")
    p.add_argument("--dir", default=str(ROOT / "fixtures" / "first_integration"))
    p.add_argument("--out", default=runs, help="기록 폴더(기본: ~/netrunner-private/runs)")
    p.add_argument("--repeat", type=int, default=1)
    p.set_defaults(func=cmd_lab_check)

    p = sub.add_parser(
        "breaker-propose", help="GLM으로 시나리오를 제안받아 검사 통과율을 셈(실험망 미사용)"
    )
    p.add_argument("--n", type=int, default=1, help="제안을 받을 횟수")
    p.add_argument("--history", help="지난 라운드 기록 JSONL(없으면 첫 제안)")
    _add_breaker_args(p)
    p.add_argument(
        "--out",
        default=runs,
        help="호출 기록과 채택 시나리오 폴더(기본: ~/netrunner-private/runs)",
    )
    p.set_defaults(func=cmd_breaker_propose)

    p = sub.add_parser("match", help="Breaker 제안을 심판과 실험망으로 돌려 대결")
    p.add_argument("--rounds", type=int, default=3, help="대결 라운드 수")
    p.add_argument("--impl", choices=sorted(LAB_IMPLS), default="docker", help="실험망 구현")
    p.add_argument(
        "--diagnoser",
        choices=sorted(DIAGNOSER_IMPLS),
        default="none",
        help="진단기(기본 none: 원인을 답하지 않는 자리표시)",
    )
    _add_breaker_args(p)
    p.add_argument("--out", default=runs, help="기록 폴더(기본: ~/netrunner-private/runs)")
    p.set_defaults(func=cmd_match)
    return ap


def _add_breaker_args(p: argparse.ArgumentParser) -> None:
    """Breaker 제안에 쓰는 공통 인자: 대상, 동시 장애 수, GLM 모델과 실행 파일."""
    p.add_argument("--target", default="http://shop.lab/", help="대상 URL")
    p.add_argument("--max-faults", type=int, default=1, help="실험망이 한 라운드에 켤 장애 수")
    _add_glm_args(p)


def _add_glm_args(p: argparse.ArgumentParser) -> None:
    """GLM을 부르는 쪽(Breaker, GLM 진단기)이 쓰는 인자: 모델과 opencode 실행 파일."""
    p.add_argument("--model", default=DEFAULT_MODEL, help="opencode 모델 ID")
    p.add_argument(
        "--opencode",
        default=os.environ.get("NETRUNNER_OPENCODE", "opencode"),
        help="opencode 실행 파일 경로(기본: NETRUNNER_OPENCODE 또는 PATH의 opencode)",
    )


def main(argv: list[str] | None = None) -> int:
    """명령줄 인자를 해석해 하위 명령을 실행한다."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
