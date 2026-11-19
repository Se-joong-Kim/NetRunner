"""breaker-propose 명령 테스트. GLM 호출은 가짜로 바꿔 끼운다.

모든 테스트는 --out을 임시 폴더로 준다. 그래야 runs/에 기록이 남지 않는다.
"""

from __future__ import annotations

import json

import pytest

from netrunner import __main__ as cli
from netrunner.breaker import CompletionError

GOOD = json.dumps(
    {"faults": [{"id": "T2", "params": {"port": 80}}], "complaint": "사이트가 열리지 않는다"},
    ensure_ascii=False,
)


def _d2(i):
    """서로 다른 D2 제안 하나. 대역 안의 주소 번호만 바꾼다."""
    params = {"domain": "shop.lab", "ip": f"10.20.0.{100 + i}"}
    return json.dumps(
        {"faults": [{"id": "D2", "params": params}], "complaint": "사이트가 열리지 않는다"},
        ensure_ascii=False,
    )


def _replies(*items):
    """준비한 응답을 호출 순서대로 돌려준다."""
    queue = list(items)
    return lambda _prompt: queue.pop(0)


def _use_fake(monkeypatch, reply):
    """OpencodeComplete 자리에 가짜 호출을 넣는다. 받은 프롬프트와 생성 인자를 돌려준다."""
    prompts: list[str] = []
    built: list[dict] = []

    def fake(prompt: str) -> str:
        prompts.append(prompt)
        return reply(prompt)

    def factory(**kwargs):
        built.append(kwargs)
        return fake

    monkeypatch.setattr(cli, "OpencodeComplete", factory)
    return prompts, built


def _run(tmp_path, *extra):
    return cli.main(["breaker-propose", "--out", str(tmp_path), *extra])


def test_breaker_propose_writes_the_accepted_scenario_and_the_call_journal(
    monkeypatch, capsys, tmp_path
):
    prompts, _ = _use_fake(monkeypatch, lambda _p: GOOD)
    assert _run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "제안 요청 1회, 채택 1회" in out
    assert "채택률(채택/호출) 100%" in out
    assert "서로 다른 조합 1개" in out
    (chosen,) = tmp_path.glob("breaker-propose-*-01.scenario.json")
    scenario = json.loads(chosen.read_text(encoding="utf-8"))
    assert scenario["origin"] == "breaker"
    assert scenario["target_url"] == "http://shop.lab/"
    assert [f["id"] for f in scenario["faults"]] == ["T2"]
    (journal,) = tmp_path.glob("breaker-[0-9]*.jsonl")
    record = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert record["verdict"] == "채택"
    assert len(prompts) == 1


def test_each_accepted_scenario_gets_its_own_file_that_validate_can_read(monkeypatch, tmp_path):
    _use_fake(monkeypatch, _replies(_d2(1), _d2(2), _d2(3)))
    assert _run(tmp_path, "--n", "3") == 0
    files = sorted(tmp_path.glob("breaker-propose-*.scenario.json"))
    assert [f.name.endswith(f"-{i:02d}.scenario.json") for i, f in enumerate(files, 1)] == [
        True,
        True,
        True,
    ]
    for f in files:
        assert json.loads(f.read_text(encoding="utf-8"))["origin"] == "breaker"


def test_n_proposals_each_get_a_call_and_all_are_counted(monkeypatch, capsys, tmp_path):
    prompts, _ = _use_fake(monkeypatch, _replies(_d2(1), _d2(2), _d2(3)))
    assert _run(tmp_path, "--n", "3") == 0
    assert len(prompts) == 3
    assert "제안 요청 3회, 채택 3회" in capsys.readouterr().out


def test_failed_proposals_are_reported_and_give_exit_code_one(monkeypatch, capsys, tmp_path):
    def fail(_prompt):
        raise CompletionError("연결 실패")

    _use_fake(monkeypatch, fail)
    assert _run(tmp_path, "--n", "2") == 1
    captured = capsys.readouterr()
    assert captured.err.count("제안 실패") == 2
    assert "제안 요청 2회, 채택 0회" in captured.out
    assert not list(tmp_path.glob("breaker-propose-*.scenario.json"))


def test_accepted_scenarios_are_saved_before_a_later_crash(monkeypatch, tmp_path):
    calls: list[int] = []

    def reply(_prompt):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("중간에 끊김")
        return GOOD

    _use_fake(monkeypatch, reply)
    with pytest.raises(RuntimeError):
        _run(tmp_path, "--n", "3")
    assert len(list(tmp_path.glob("breaker-propose-*.scenario.json"))) == 1


def test_invalid_target_url_is_rejected_before_any_call(monkeypatch, capsys, tmp_path):
    prompts, _ = _use_fake(monkeypatch, lambda _p: GOOD)
    assert _run(tmp_path, "--target", "http://shop.lab:99999/") == 1
    assert "대상 URL이 잘못됨" in capsys.readouterr().err
    assert prompts == []


def test_n_must_be_at_least_one(monkeypatch, capsys, tmp_path):
    _use_fake(monkeypatch, lambda _p: GOOD)
    assert _run(tmp_path, "--n", "0") == 1
    assert "--n" in capsys.readouterr().err


def test_model_flag_reaches_the_opencode_call(monkeypatch, tmp_path):
    _, built = _use_fake(monkeypatch, lambda _p: GOOD)
    assert _run(tmp_path, "--model", "opencode-go/test-model") == 0
    assert built[0]["model"] == "opencode-go/test-model"


def test_an_accepted_proposal_is_not_repeated_later_in_the_same_run(monkeypatch, capsys, tmp_path):
    prompts, _ = _use_fake(monkeypatch, lambda _p: GOOD)
    assert _run(tmp_path, "--n", "2") == 0
    out = capsys.readouterr().out
    assert "제안 요청 2회, 채택 1회" in out
    assert "중복 3" in out  # 두 번째 제안은 세 번 모두 같은 조합이라 거부된다
    assert '- T2 {"port": 80}' in prompts[1]


def test_fixed_history_rounds_do_not_change_the_prompt(monkeypatch, tmp_path):
    prompts, _ = _use_fake(monkeypatch, lambda _p: GOOD)
    assert _run(tmp_path) == 0
    hist = tmp_path / "history.jsonl"
    hist.write_text(json.dumps({"origin": "fixed"}) + "\n", encoding="utf-8")
    assert _run(tmp_path, "--history", str(hist)) == 0
    assert prompts[0] == prompts[1]
