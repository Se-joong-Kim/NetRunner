"""propose 명령 테스트. GLM 호출은 가짜로 바꿔 끼운다."""

from __future__ import annotations

import json

from netrunner import __main__ as cli
from netrunner.breaker import CompletionError

GOOD = json.dumps(
    {"faults": [{"id": "T2", "params": {"port": 80}}], "complaint": "사이트가 열리지 않는다"},
    ensure_ascii=False,
)


def _use_fake(monkeypatch, reply):
    """OpencodeComplete 자리에 가짜 호출을 넣고, 받은 프롬프트 목록을 돌려준다."""
    prompts: list[str] = []

    def fake(prompt: str) -> str:
        prompts.append(prompt)
        return reply(prompt)

    monkeypatch.setattr(cli, "OpencodeComplete", lambda **_kwargs: fake)
    return prompts


def test_propose_prints_checked_scenario(monkeypatch, capsys):
    prompts = _use_fake(monkeypatch, lambda _p: GOOD)
    assert cli.main(["propose"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["origin"] == "breaker"
    assert out["target_url"] == "http://shop.lab/"
    assert [f["id"] for f in out["faults"]] == ["T2"]
    assert len(prompts) == 1


def test_propose_gives_up_after_repeated_completion_errors(monkeypatch, capsys):
    def fail(_prompt):
        raise CompletionError("연결 실패")

    _use_fake(monkeypatch, fail)
    assert cli.main(["propose"]) == 1
    assert "제안 실패" in capsys.readouterr().err


def test_fixed_history_rounds_do_not_change_the_prompt(monkeypatch, tmp_path):
    prompts = _use_fake(monkeypatch, lambda _p: GOOD)
    assert cli.main(["propose"]) == 0
    hist = tmp_path / "history.jsonl"
    hist.write_text(json.dumps({"origin": "fixed"}) + "\n", encoding="utf-8")
    assert cli.main(["propose", "--history", str(hist)]) == 0
    assert prompts[0] == prompts[1]
