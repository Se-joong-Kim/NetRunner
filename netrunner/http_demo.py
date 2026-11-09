"""python -m netrunner.http_demo: 실제 HTTP Lab과 심판 연결 확인(LLM 없음)."""

from __future__ import annotations

from datetime import datetime

from .contracts import Diagnosis, Evidence, Finding, Probe, Scenario, read_json
from .http_lab import ROOT, HttpDockerLab
from .referee import Referee
from .report import render_html, summarize
from .validate import LabInfo


class HttpSmokeDiagnoser:
    """brief의 URL에 실제 요청한다. 장애 정답과 주입 설정은 입력받지 않는다."""

    model = "rules-http-smoke"
    method = "A"

    def __init__(self, probe):
        self.probe = probe

    def diagnose(self, brief):
        """실제 응답이 502일 때만 원본 검사 근거와 함께 진단한다."""
        observation = self.probe(brief.target_url)
        trace = [
            Probe(
                "http-1",
                "python-http",
                {"url": brief.target_url, "location": "client container"},
                observation["raw_http"],
                observation["elapsed_ms"],
            )
        ]
        findings = []
        if observation["http_code"] == 502:
            findings.append(
                Finding("HTTP", "서버 오류", [Evidence("probe:http-1", "HTTP 502 응답 관측")])
            )
        return Diagnosis(brief.round_id, self.model, self.method, findings, trace)


def main():
    """실제 HTTP 실험망에서 심판 한 라운드를 실행하고 결과를 저장한다."""
    lab = HttpDockerLab()
    lab.start()
    scenario = Scenario.from_dict(read_json(ROOT / "fixtures/http/h1.scenario.json"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    records = ROOT / "runs" / f"http-{stamp}.jsonl"
    referee = Referee(
        lab,
        HttpSmokeDiagnoser(lab.probe),
        LabInfo.load(ROOT / "config/http_lab.json"),
        records_path=records,
    )
    record = referee.run_round(scenario)
    for step in record["lab_log"]:
        print(f"{step['step']}: {step['detail']} (ok={step['ok']})")
    print(f"라운드: {record['status']}, 복구 확인: {record['reset_verified']}")
    if record["score"]:
        for fault in record["score"]["faults"]:
            print(f"채점: {fault['id']} / {fault['verdict']}")
    if record["invalid_reason"]:
        print(f"실패 이유: {record['invalid_reason']}")
    html = records.with_suffix(".html")
    html.write_text(render_html([record], summarize([record])), encoding="utf-8")
    print(f"기록: {records}\n결과 화면: {html}")
    return 0 if record["status"] == "scored" and record["reset_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
