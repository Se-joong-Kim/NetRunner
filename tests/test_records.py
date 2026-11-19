"""정답이 든 기록의 저장 위치와 권한(#21, #24)."""

import os
import stat
from pathlib import Path

import pytest

from netrunner import records
from netrunner.__main__ import ROOT, build_parser, main
from netrunner.breaker_log import CallJournal

posix_only = pytest.mark.skipif(os.name != "posix", reason="권한 비트는 POSIX에서만 의미가 있음")


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Path.home()이 임시 폴더를 가리키게 한다(POSIX는 HOME, Windows는 USERPROFILE)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


@pytest.fixture
def loose_umask():
    """셸 기본값과 같은 umask 0002에서 확인한다. 이 값이면 새 파일이 664가 된다."""
    old = os.umask(0o002)
    yield
    os.umask(old)


def test_default_runs_dir_is_private_and_outside_repo(home):
    d = records.default_runs_dir()
    assert d == home / "netrunner-private" / "runs"
    assert ROOT not in d.parents


@pytest.mark.parametrize("cmd", ["demo", "run", "lab-check", "breaker-propose", "match"])
def test_commands_write_to_private_runs_by_default(home, cmd):
    assert Path(build_parser().parse_args([cmd]).out) == home / "netrunner-private" / "runs"


def test_write_private_text_replaces_content(tmp_path):
    p = tmp_path / "a.json"
    records.write_private_text(p, "첫째")
    records.write_private_text(p, "둘째")
    assert p.read_text(encoding="utf-8") == "둘째"


def test_open_private_appends(tmp_path):
    p = tmp_path / "a.jsonl"
    for line in ("1\n", "2\n"):
        with records.open_private(p) as fp:
            fp.write(line)
    assert p.read_text(encoding="utf-8") == "1\n2\n"


@posix_only
def test_missing_dirs_are_created_as_700(tmp_path, loose_umask):
    d = records.ensure_private_dir(tmp_path / "private" / "runs")
    assert d.is_dir()
    assert _mode(tmp_path / "private") == 0o700 and _mode(d) == 0o700


@posix_only
def test_existing_dirs_keep_their_mode(tmp_path):
    # 사용자가 --out으로 준 기존 폴더(예: 홈)의 권한은 바꾸지 않는다.
    os.chmod(tmp_path, 0o755)
    records.ensure_private_dir(tmp_path)
    assert _mode(tmp_path) == 0o755


@posix_only
def test_files_are_600_even_when_they_existed(tmp_path, loose_umask):
    new = tmp_path / "new.jsonl"
    records.write_private_text(new, "x")
    old = tmp_path / "old.jsonl"
    old.write_text("y", encoding="utf-8")
    os.chmod(old, 0o664)
    with records.open_private(old) as fp:
        fp.write("z")
    assert _mode(new) == 0o600 and _mode(old) == 0o600
    assert old.read_text(encoding="utf-8") == "yz"


@posix_only
def test_round_records_and_report_are_private(tmp_path, loose_umask):
    out = tmp_path / "private" / "runs"
    assert main(["demo", "--out", str(out)]) == 0
    files = list(out.iterdir())
    assert {p.suffix for p in files} == {".jsonl", ".html"}
    assert _mode(out) == 0o700
    assert all(_mode(p) == 0o600 for p in files)


@posix_only
def test_report_html_is_private(tmp_path, loose_umask):
    out = tmp_path / "runs"
    main(["demo", "--out", str(out)])
    (rounds,) = out.glob("rounds-*.jsonl")
    html = tmp_path / "report.html"
    assert main(["report", str(rounds), "--html", str(html)]) == 0
    assert _mode(html) == 0o600


@posix_only
def test_breaker_call_journal_is_private(tmp_path, loose_umask):
    path = tmp_path / "private" / "breaker.jsonl"
    CallJournal(path)({"response": "x"})
    assert _mode(path.parent) == 0o700 and _mode(path) == 0o600
