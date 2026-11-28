"""심판: 라운드 순서, 비공개 정답, 채점, 초기화 확인(설계안 5장).

검증 → 정상 확인 → 주입 → 재현 확인 → 진단 → 채점·정답 공개 → 복구·정상화 확인 → 기록
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from .breaker import Breaker, BreakerError
from .contracts import (
    BaselineBrief,
    BreakerFeedback,
    ContractError,
    FaultOutcome,
    LabResult,
    Limits,
    RoundBrief,
    Scenario,
    Status,
)
from .diagnoser import Diagnoser
from .lab import Lab, ProbeRunner, order_faults_for_injection
from .records import ensure_private_dir, open_private
from .scoring import score
from .toolbox import Toolbox
from .tools import tools_for_method
from .validate import LabInfo, validate


class Referee:
    """라운드를 순서대로 돌리고 결과를 기록하는 심판."""

    def __init__(
        self,
        lab: Lab,
        diagnoser: Diagnoser,
        lab_info: LabInfo,
        limits: Limits | None = None,
        records_path: str | Path | None = None,
        runner: ProbeRunner | None = None,
    ):
        self.lab = lab
        # 검사 도구를 실행할 쪽. 진입점이 라운드마다 바꿀 수 있다.
        # 없으면 도구 없이 진단한다.
        self.runner = runner
        self.diagnoser = diagnoser
        self.lab_info = lab_info
        self.limits = limits or Limits()
        self.records_path = Path(records_path) if records_path else None

    def _new_record(self, scenario: Scenario | None) -> dict:
        """라운드 또는 시나리오가 없는 제안 실패의 기록을 만든다."""
        # 읽는 쪽이 키 유무를 따로 판단하지 않도록 모든 키를 처음에 둔다.
        return {
            "schema": SCHEMA_VERSION,
            "round_id": secrets.token_hex(4),  # 시나리오와 무관한 중립 ID
            "scenario_id": scenario.scenario_id if scenario else None,
            "origin": scenario.origin if scenario else "breaker",
            "model": self.diagnoser.model,
            "method": self.diagnoser.method,
            "status": Status.RUNNING,
            "invalid_reason": "",
            "validation": None,
            "lab_log": [],
            "reset_verified": None,  # None: 실험망을 건드리지 않음
            "answer": asdict(scenario) if scenario else None,
            "brief": None,
            "diagnosis": None,
            # 도구 창구가 기록한 실제 검사(채점 근거)와 거부된 호출
            "probes": [],
            "rejected_calls": [],
            # 검사 출력을 낸 쪽. ReplayRunner면 저장된 출력을 재생한 것이다.
            "probe_runner": type(self.runner).__name__ if scenario and self.runner else None,
            "score": None,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "finished_at": None,
        }

    def run_round(self, scenario: Scenario) -> dict:
        """시나리오 하나를 검증부터 기록까지 한 라운드로 돌린다."""
        rec = self._new_record(scenario)
        round_id = rec["round_id"]
        log = rec["lab_log"]

        def step(name: str, fn, *args) -> bool:
            # 실험망 함수의 예외도 실패로 기록한다. 예외가 새면 라운드 기록과 복구가 깨진다.
            try:
                result = fn(*args)
            except Exception as e:
                result = LabResult(False, f"{type(e).__name__}: {e}")
            log.append({"step": name, **asdict(result)})
            return result.ok

        def end(status: str, reason: str) -> dict:
            rec["status"] = status
            rec["invalid_reason"] = reason
            return rec

        try:
            # 1. 검증. 실험망을 건드리기 전에 한다(검증 안 된 URL로 접속하지 않는다).
            v = validate(scenario, self.lab_info)
            rec["validation"] = {
                "errors": v.errors,
                "warnings": v.warnings,
                "observable": v.observable,
            }
            if not v.ok:
                return end(Status.REJECTED, "시나리오 검증 실패")

            # 2. 정상 확인. 아니면 한 번 초기화하고 다시 본다.
            if not step("health_check", self.lab.health_check, scenario.target_url):
                step("reset", self.lab.reset)
                if not step("health_check", self.lab.health_check, scenario.target_url):
                    return end(Status.INVALID, "라운드 시작 전 실험망이 정상이 아님")

            # 3. 주입과 재현 확인
            for f in order_faults_for_injection(scenario.faults):
                if not step(f"inject:{f.id}", self.lab.inject, f):
                    return end(Status.INVALID, f"{f.id} 주입 실패")
            if not step("verify_symptom", self.lab.verify_symptom, scenario):
                return end(Status.INVALID, "증상 재현 실패")

            # 4. 진단: Diagnoser는 brief와 도구 창구만 받는다.
            allowed = tools_for_method(self.diagnoser.method)
            if allowed is None:
                return end(Status.INVALID, f"알 수 없는 진단 방식: {self.diagnoser.method}")
            brief_args = (round_id, scenario.target_url, scenario.complaint, self.limits)
            if self.diagnoser.method == "B" and getattr(self.diagnoser, "needs_curl_brief", False):
                curl_error = self._baseline_curl_error(scenario.target_url)
            else:
                curl_error = None
            brief = (
                BaselineBrief(*brief_args, curl_error=curl_error)
                if curl_error is not None
                else RoundBrief(*brief_args)
            )
            rec["brief"] = asdict(brief)
            toolbox = Toolbox(self.runner, self.lab_info, self.limits, allowed)
            t0 = time.monotonic()
            try:
                diagnosis = self.diagnoser.diagnose(brief, toolbox)
            except ContractError as e:
                return end(Status.INVALID, f"진단 출력 형식 오류: {e}")
            except Exception as e:  # Diagnoser 쪽 오류로 실험망 복구가 빠지면 안 된다
                return end(Status.INVALID, f"진단 실행 오류: {type(e).__name__}: {e}")
            finally:
                # 라운드 뒤에는 창구로 실험망을 만질 수 없게 닫고,
                # 실행된 검사는 기록에 남긴다.
                toolbox.close()
                rec["probes"] = [asdict(p) for p in toolbox.probes()]
                rec["rejected_calls"] = [asdict(c) for c in toolbox.rejected_calls()]
            elapsed = time.monotonic() - t0
            if diagnosis.round_id != round_id:
                return end(Status.INVALID, "진단의 round_id가 brief와 다름")
            rec["diagnosis"] = asdict(diagnosis)

            # 5. 채점. 이 시점부터 정답(answer)을 공개해도 된다.
            rec["score"] = asdict(
                score(
                    scenario,
                    v.observable,
                    diagnosis,
                    elapsed,
                    self.limits,
                    probes=toolbox.probes(),
                )
            )
            return end(Status.SCORED, "")
        finally:
            # 6. 복구와 정상화 확인. 실험망을 건드린 라운드는 무효여도 반드시 실행한다.
            if log:
                reset_ok = step("reset", self.lab.reset)
                after_ok = step("health_check_after", self.lab.health_check, scenario.target_url)
                rec["reset_verified"] = reset_ok and after_ok
            rec["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._append(rec)

    def _baseline_curl_error(self, url: str) -> str | None:
        """방식 B의 입력 준비용 curl 한 번에서 오류 한 줄을 골라낸다."""
        input_toolbox = Toolbox(self.runner, self.lab_info, self.limits, frozenset({"curl"}))
        try:
            view = input_toolbox.call("curl", {"url": url})
        finally:
            input_toolbox.close()
        if view.probe_id is None:
            return None

        lines = view.output.splitlines()
        for line in lines:
            if re.match(r"^curl: \(\d+\) ", line):
                return line[:300]
        for line in lines:
            if status := re.fullmatch(r"http_code=([45]\d{2})", line):
                return f"HTTP {status[1]}"
        return None

    def run_match(self, breaker: Breaker, rounds: int, max_retries: int = 3) -> list[dict]:
        """Breaker 제안 → 라운드 → 피드백을 반복한다.

        검증에서 거부되면 다시 제안받고, Breaker가 제안에 실패하면
        실패를 기록한 뒤 완료된 라운드와 함께 반환한다.
        """
        records: list[dict] = []
        history: list[BreakerFeedback] = []
        for _ in range(rounds):
            for _attempt in range(max_retries):
                started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                try:
                    scenario = breaker.propose(history)
                except BreakerError:
                    rec = self._new_record(None)
                    # 진단이 없으므로 모델과 방식은 비운다
                    rec["model"] = None
                    rec["method"] = None
                    rec["status"] = Status.PROPOSAL_FAILED
                    # 모델 응답이나 API 오류 원문은 공유 결과에 기록하지 않는다.
                    rec["invalid_reason"] = "유효한 Breaker 제안이 없어 매치를 종료했습니다."
                    rec["started_at"] = started_at
                    rec["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    self._append(rec)
                    records.append(rec)
                    return records
                # 세트 구분은 심판이 정한다. Breaker가 고정 세트로 위장하면 평가 지표가 오염된다.
                rec = self.run_round(replace(scenario, origin="breaker"))
                records.append(rec)
                fb = feedback_for(rec)
                if fb:
                    history.append(fb)
                if rec["reset_verified"] is False:
                    return records  # 오염된 실험망에서 다음 라운드를 돌리지 않는다
                if rec["status"] != Status.REJECTED:
                    break
        return records

    def _append(self, rec: dict) -> None:
        if self.records_path is None:
            return
        # 정답이 든 기록이라 폴더 700, 파일 600으로 쓴다(records.py).
        ensure_private_dir(self.records_path.parent)
        with open_private(self.records_path) as fp:
            # 거부된 호출의 인자는 Diagnoser가 낸 그대로라 JSON이 아닐 수 있다.
            fp.write(json.dumps(rec, ensure_ascii=False, default=repr) + "\n")


def feedback_for(rec: dict) -> BreakerFeedback | None:
    """대결용 시나리오를 실제 제안한 기록만 Breaker에 돌려준다."""
    if rec["origin"] == "fixed" or rec["status"] == Status.PROPOSAL_FAILED:
        return None
    answer = Scenario.from_dict(rec["answer"])
    fb = BreakerFeedback(
        scenario_id=answer.scenario_id,
        faults=answer.faults,
        complaint=answer.complaint,
        valid=rec["status"] == Status.SCORED,
        invalid_reason=rec["invalid_reason"],
    )
    if rec["status"] == Status.REJECTED:
        fb.invalid_reason += ": " + "; ".join(rec["validation"]["errors"])
    if rec["score"]:
        s = rec["score"]
        fb.outcomes = [FaultOutcome(f["id"], f["label"], f["verdict"]) for f in s["faults"]]
        fb.false_positives = list(s["false_positives"])
    return fb
