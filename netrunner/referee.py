"""심판: 라운드 순서, 비공개 정답, 채점, 초기화 확인(설계안 5장).

검증 → 정상 확인 → 주입 → 재현 확인 → 진단 → 채점·정답 공개 → 복구·정상화 확인 → 기록
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from .breaker import Breaker
from .contracts import (
    BreakerFeedback,
    ContractError,
    FaultOutcome,
    LabResult,
    Limits,
    RoundBrief,
    Scenario,
)
from .diagnoser import Diagnoser
from .lab import Lab
from .scoring import score
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
    ):
        self.lab = lab
        self.diagnoser = diagnoser
        self.lab_info = lab_info
        self.limits = limits or Limits()
        self.records_path = Path(records_path) if records_path else None

    def run_round(self, scenario: Scenario) -> dict:
        """시나리오 하나를 검증부터 기록까지 한 라운드로 돌린다."""
        round_id = secrets.token_hex(4)  # 시나리오 ID와 무관한 중립 ID
        # 읽는 쪽이 키 유무를 따로 판단하지 않도록 모든 키를 처음에 둔다.
        rec: dict = {
            "schema": SCHEMA_VERSION,
            "round_id": round_id,
            "scenario_id": scenario.scenario_id,
            "origin": scenario.origin,
            "model": self.diagnoser.model,
            "method": self.diagnoser.method,
            # scored | invalid | rejected(검증 거부, 실험망 미사용)
            "status": "running",
            "invalid_reason": "",
            "validation": None,
            "lab_log": [],
            "reset_verified": None,  # None: 실험망을 건드리지 않음
            "answer": asdict(scenario),
            "brief": None,
            "diagnosis": None,
            "score": None,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "finished_at": None,
        }
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
                return end("rejected", "시나리오 검증 실패")

            # 2. 정상 확인. 아니면 한 번 초기화하고 다시 본다.
            if not step("health_check", self.lab.health_check, scenario.target_url):
                step("reset", self.lab.reset)
                if not step("health_check", self.lab.health_check, scenario.target_url):
                    return end("invalid", "라운드 시작 전 실험망이 정상이 아님")

            # 3. 주입과 재현 확인
            for f in scenario.faults:
                if not step(f"inject:{f.id}", self.lab.inject, f):
                    return end("invalid", f"{f.id} 주입 실패")
            if not step("verify_symptom", self.lab.verify_symptom, scenario):
                return end("invalid", "증상 재현 실패")

            # 4. 진단: Diagnoser는 brief만 본다.
            brief = RoundBrief(round_id, scenario.target_url, scenario.complaint, self.limits)
            rec["brief"] = asdict(brief)
            t0 = time.monotonic()
            try:
                diagnosis = self.diagnoser.diagnose(brief)
            except ContractError as e:
                return end("invalid", f"진단 출력 형식 오류: {e}")
            except Exception as e:  # Diagnoser 쪽 오류로 실험망 복구가 빠지면 안 된다
                return end("invalid", f"진단 실행 오류: {type(e).__name__}: {e}")
            elapsed = time.monotonic() - t0
            if diagnosis.round_id != round_id:
                return end("invalid", "진단의 round_id가 brief와 다름")
            rec["diagnosis"] = asdict(diagnosis)

            # 5. 채점. 이 시점부터 정답(answer)을 공개해도 된다.
            rec["score"] = asdict(score(scenario, v.observable, diagnosis, elapsed, self.limits))
            return end("scored", "")
        finally:
            # 6. 복구와 정상화 확인. 실험망을 건드린 라운드는 무효여도 반드시 실행한다.
            if log:
                reset_ok = step("reset", self.lab.reset)
                after_ok = step("health_check_after", self.lab.health_check, scenario.target_url)
                rec["reset_verified"] = reset_ok and after_ok
            rec["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._append(rec)

    def run_match(self, breaker: Breaker, rounds: int, max_retries: int = 3) -> list[dict]:
        """Breaker 제안 → 라운드 → 피드백을 반복한다.

        검증에서 거부되면 같은 라운드를 다시 제안받는다.
        """
        records: list[dict] = []
        history: list[BreakerFeedback] = []
        for _ in range(rounds):
            for _attempt in range(max_retries):
                # 세트 구분은 심판이 정한다. Breaker가 고정 세트로 위장하면 평가 지표가 오염된다.
                rec = self.run_round(replace(breaker.propose(history), origin="breaker"))
                records.append(rec)
                fb = feedback_for(rec)
                if fb:
                    history.append(fb)
                if rec["reset_verified"] is False:
                    return records  # 오염된 실험망에서 다음 라운드를 돌리지 않는다
                if rec["status"] != "rejected":
                    break
        return records

    def _append(self, rec: dict) -> None:
        if self.records_path is None:
            return
        self.records_path.parent.mkdir(parents=True, exist_ok=True)
        with self.records_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")


def feedback_for(rec: dict) -> BreakerFeedback | None:
    """심판 → Breaker. 고정 세트 라운드는 최종 평가용이므로 Breaker 입력에서 뺀다."""
    if rec["origin"] == "fixed":
        return None
    answer = Scenario.from_dict(rec["answer"])
    fb = BreakerFeedback(
        scenario_id=answer.scenario_id,
        faults=answer.faults,
        complaint=answer.complaint,
        valid=rec["status"] == "scored",
        invalid_reason=rec["invalid_reason"],
    )
    if rec["status"] == "rejected":
        fb.invalid_reason += ": " + "; ".join(rec["validation"]["errors"])
    if rec["score"]:
        s = rec["score"]
        fb.outcomes = [FaultOutcome(f["id"], f["label"], f["verdict"]) for f in s["faults"]]
        fb.false_positives = list(s["false_positives"])
    return fb
