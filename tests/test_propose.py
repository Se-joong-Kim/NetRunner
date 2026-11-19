"""breaker-propose 명령 테스트. GLM 호출은 가짜로 바꿔 끼운다.

모든 테스트는 --out을 임시 폴더로 준다. 그래야 runs/에 기록이 남지 않는다.
"""

from __future__ import annotations

import json

from netrunner import __main__ as cli
from netrunner.breaker import CompletionError

GOOD = json.dumps(
    {"faults": [{"id": "T2", "params": {"port": 80}}], "complaint": "사이트가 열리지 않는다"},
    ensure_ascii=False,
)


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
    (chosen,) = tmp_path.glob("breaker-propose-*.jsonl")
    scenario = json.loads(chosen.read_text(encoding="utf-8").splitlines()[0])
    assert scenario["origin"] == "breaker"
    assert scenario["target_url"] == "http://shop.lab/"
    assert [f["id"] for f in scenario["faults"]] == ["T2"]
    (journal,) = tmp_path.glob("breaker-[0-9]*.jsonl")
    record = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert record["verdict"] == "채택"
    assert len(prompts) == 1


def test_n_proposals_each_get_a_call_and_all_are_counted(monkeypatch, capsys, tmp_path):
    prompts, _ = _use_fake(monkeypatch, lambda _p: GOOD)
    assert _run(tmp_path, "--n", "3") == 0
    assert len(prompts) == 3
    assert "제안 요청 3회, 채택 3회" in capsys.readouterr().out
    (chosen,) = tmp_path.glob("breaker-propose-*.jsonl")
    assert len(chosen.read_text(encoding="utf-8").splitlines()) == 3


def test_failed_proposals_are_reported_and_give_exit_code_one(monkeypatch, capsys, tmp_path):
    def fail(_prompt):
        raise CompletionError("연결 실패")

    _use_fake(monkeypatch, fail)
    assert _run(tmp_path, "--n", "2") == 1
    captured = capsys.readouterr()
    assert captured.err.count("제안 실패") == 2
    assert "제안 요청 2회, 채택 0회" in captured.out
    assert not list(tmp_path.glob("breaker-propose-*.jsonl"))


def test_n_must_be_at_least_one(monkeypatch, capsys, tmp_path):
    _use_fake(monkeypatch, lambda _p: GOOD)
    assert _run(tmp_path, "--n", "0") == 1
    assert "--n" in capsys.readouterr().err


def test_model_flag_reaches_the_opencode_call(monkeypatch, tmp_path):
    _, built = _use_fake(monkeypatch, lambda _p: GOOD)
    assert _run(tmp_path, "--model", "opencode-go/test-model") == 0
    assert built[0]["model"] == "opencode-go/test-model"


def test_fixed_history_rounds_do_not_change_the_prompt(monkeypatch, tmp_path):
    prompts, _ = _use_fake(monkeypatch, lambda _p: GOOD)
    assert _run(tmp_path) == 0
    hist = tmp_path / "history.jsonl"
    hist.write_text(json.dumps({"origin": "fixed"}) + "\n", encoding="utf-8")
    assert _run(tmp_path, "--history", str(hist)) == 0
    assert prompts[0] == prompts[1]
