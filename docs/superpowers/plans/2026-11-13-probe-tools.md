# 검사 도구 계약 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Diagnoser의 모든 검사를 심판이 만든 `Toolbox`로 실행하게 하고, 채점은 창구 기록으로만 한다.

**Architecture:** 도구 목록과 인자 검사는 `tools.py`(데이터, `faults.py`와 같은 방식)에 둔다. 라운드별 창구 `Toolbox`(`toolbox.py`)가 검사·한도·기록을 맡는다. 실험망은 `ProbeRunner.run_probe(tool, args)` 하나로 실행한다. 저장된 진단은 `ReplayDiagnoser`가 창구를 거쳐 다시 요청하고, `ReplayRunner`가 저장 출력을 돌려준다.

**Tech Stack:** Python 3.10+ 표준 라이브러리, pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-11-13-probe-tools-design.md`

## Global Constraints

- 표준 라이브러리만 쓴다. Python 3.10 이상(`uv run --python 3.10 pytest`로도 통과).
- `uv run ruff check`와 `uv run ruff format --check`를 통과한다. 코드는 99자, 주석과 docstring은 72자로 줄바꿈한다. 공개 모듈·클래스·함수에는 한국어 한 줄 요약 docstring을 둔다.
- `netrunner/docker_lab.py`(양시찬)와 `netrunner/http_lab.py`(이현섭)는 고치지 않는다. `HttpDiagnoser`는 Task 7에서 함수 모양만 고친다.
- 작업 브랜치는 `Sejoong`이고 PR은 #9(draft)다. 커밋 메시지는 한국어로 쓰고 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`를 붙인다.
- 출력 길이 한도는 `MAX_OUTPUT_CHARS = 20_000`이다. 방식별 허용 도구는 다음과 같다.
  - A, C, C-2: 전체
  - B: 없음
  - C-1: `capture` 제외
- 도구 이름은 `dig`, `curl`, `capture`, `openssl`, `ping`, `tracepath` 여섯 개다.
- `config/lab.json`의 `enabled_tools`는 `["dig", "curl", "capture"]`로 둔다. `LabInfo.enabled_tools`의 기본값은 `[]`(도구 없음)이다.

## Review Focus

1. **LLM이 깨진 인자를 낼 때:** `args`가 dict가 아니거나(JSON 문자열, `None`), 도구 이름이 문자열이 아니면 예외 없이 거부해야 한다. Task 3의 `test_garbage_call_is_rejected_not_raised`가 이 경우를 고정한다.
2. **정수 칸에 bool이 올 때:** `port: true`, `count: false`처럼 bool이 오면 정수로 받지 않고 거부한다. Task 2의 `test_bool_is_not_an_int`.
3. **같은 검사를 두 번 할 때:** 재생에서 같은 (도구, 인자)를 두 번 부르면 저장된 순서대로 돌려준다. 저장된 수보다 많이 부르면 실패를 돌려준다. Task 4의 `test_replay_runner_returns_saved_outputs_in_order`.
4. **검사 몇 번 뒤 Diagnoser가 예외를 낼 때:** 이미 실행된 검사는 `rec["probes"]`에 남고, 복구와 정상화 확인도 실행된다. Task 5의 `test_probes_kept_when_diagnoser_raises`.
5. **라운드가 끝난 뒤 창구를 다시 부를 때:** Diagnoser가 창구를 들고 있다가 복구 중이나 다음 라운드에 부르면 실험망이 불리면 안 된다. 심판이 `close()`를 부른다. Task 3의 `test_closed_toolbox_rejects_calls`, Task 5의 `test_toolbox_closed_after_round`.

---

### Task 1: 계약 자료형과 실험망 정보

**Files:**
- Modify: `netrunner/contracts.py` (`Probe` 아래)
- Modify: `netrunner/validate.py` (`LabInfo`)
- Modify: `config/lab.json`
- Test: `tests/test_tools.py` (신설)

**Interfaces:**
- Produces:
  - `contracts.ProbeResult(ok: bool, output: str = "", elapsed_ms: int = 0, packets: int | None = None, file: str | None = None, error: str = "")`: 실험망 → 창구.
  - `contracts.ProbeView(probe_id: str | None, output: str = "", packets: int | None = None, file: str | None = None, error: str = "")`: 창구 → Diagnoser. `probe_id`는 실행된 검사에만 붙는다.
  - `LabInfo.hosts: dict[str, str]`(기본 `{}`), `LabInfo.enabled_tools: list[str]`(기본 `[]`). `LabInfo.load`가 `hosts`와 `enabled_tools`를 있으면 읽는다.

- [ ] **Step 1:** `tests/test_tools.py`에 실패하는 테스트를 쓴다.
  - `test_lab_info_reads_hosts_and_enabled_tools`: `LabInfo.load(config/lab.json)`의 `hosts["alt_dns"] == "10.20.0.54"`이고 `enabled_tools == ["dig", "curl", "capture"]`.
  - `test_lab_info_defaults_to_no_tools`: `tmp_path`에 `{"domains": ["nginx"], "web_ip": "127.0.0.1", "subnet": "127.0.0.0/8"}`를 쓰고 읽으면 `enabled_tools == []`, `hosts == {}`.
- [ ] **Step 2:** `uv run pytest tests/test_tools.py -v`를 실행한다. 기대: FAIL(AttributeError).
- [ ] **Step 3:** 두 dataclass와 `LabInfo` 필드를 추가하고, `config/lab.json`에 `"enabled_tools": ["dig", "curl", "capture"]`를 넣는다.
- [ ] **Step 4:** `uv run pytest -q`를 실행한다. 기대: 전부 통과.
- [ ] **Step 5:** 커밋한다. `계약: ProbeResult·ProbeView와 LabInfo.hosts·enabled_tools 추가`

### Task 2: 도구 목록 `tools.py`

**Files:**
- Create: `netrunner/tools.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `LabInfo`(Task 1).
- Produces:
  - `Arg(name: str, kind: str, required: bool = False, description: str = "")`: frozen. `kind`는 `"domain" | "dns_server" | "url" | "lab_ip" | "host" | "port" | "count"` 중 하나.
  - `Tool(name: str, description: str, args: tuple[Arg, ...], must_include: tuple[str, ...])`: frozen. `must_include`는 정규식 목록이다.
  - `TOOLS: dict[str, Tool]`, `METHOD_TOOLS: dict[str, frozenset[str]]`.
  - `tools_for_method(method: str) -> frozenset[str] | None`: 모르는 방식이면 `None`.
  - `check_args(tool: str, args: object, lab: LabInfo) -> str | None`: 오류 문구, 통과하면 `None`.
  - `specs(names: Iterable[str]) -> list[dict]`: OpenAI `tools` 형식이고 이름순이다. `{"type": "function", "function": {"name", "description", "parameters": {"type": "object", "properties", "required", "additionalProperties": False}}}`.
  - `missing_parts(tool: str, output: str) -> list[str]`: 출력에 없는 `must_include` 패턴 목록. 실험망 테스트용이다.

인자 규칙(설계 5.2절):

| 도구 | 인자 |
|---|---|
| dig | domain(domain, 필수), server(dns_server) |
| curl | url(url, 필수), resolve_ip(lab_ip) |
| capture | url(url, 필수) |
| openssl | host(domain, 필수), port(port), servername(domain) |
| ping | host(host, 필수), count(count) |
| tracepath | host(host, 필수) |

kind 판정:

| kind | 허용 값 |
|---|---|
| domain | `lab.domains`에 있는 값 |
| dns_server | `"dns"`, `"alt_dns"`, 또는 `lab.hosts`의 그 두 IP |
| url | 문자열, 200자 이하, 공백·제어문자 없음, scheme http/https, hostname이 실험망 도메인, 사용자정보 없음, 포트 없음 또는 1–65535 |
| lab_ip | `lab.subnet` 안의 IPv4 문자열 |
| host | domain 또는 lab_ip |
| port | bool이 아닌 int 1–65535 |
| count | bool이 아닌 int 1–10 |

- 모르는 키, 필수 누락, 모르는 도구, dict가 아닌 `args`는 오류다.

`must_include`:

| 도구 | 패턴 |
|---|---|
| dig | `r"ANSWER: \d+"`, `r"Query time: \d+ msec"` |
| curl | `r"exit_code=\d+"`, `r"http_code=\d+"`, `r"time_namelookup="`, `r"time_connect="`, `r"time_total="` |
| capture | `r"(?m)^#\d+ "` |
| openssl | `r"notAfter=|Verify return code"` |
| ping | `r"packet loss"` |
| tracepath | `r"pmtu \d+"` |

- 도구 설명 문구에 장애 이름이나 원인 단서(`validate.find_clues`에 걸리는 말)를 넣지 않는다.

- [ ] **Step 1:** 실패하는 테스트를 쓴다.
  - `test_method_tool_sets`: `tools_for_method("B") == frozenset()`, `"capture" not in tools_for_method("C-1")`, `tools_for_method("C") == frozenset(TOOLS)`, `tools_for_method("Z") is None`.
  - `test_check_args_accepts_lab_values` (parametrize):
    - `("dig", {"domain": "shop.lab", "server": "alt_dns"})`
    - `("dig", {"domain": "shop.lab", "server": "10.20.0.54"})`
    - `("curl", {"url": "http://shop.lab/", "resolve_ip": "10.20.0.10"})`
    - `("capture", {"url": "https://news.lab:8443/a?b=1"})`
    - `("ping", {"host": "10.20.0.10", "count": 3})`
    - 모두 `check_args(...) is None`.
  - `test_check_args_rejects` (parametrize):
    - 모르는 도구 `("rm", {})`
    - 밖 도메인 `("dig", {"domain": "example.com"})`
    - 모르는 키 `("dig", {"domain": "shop.lab", "flags": "+trace"})`
    - 필수 누락 `("curl", {})`
    - 셸 문자 `("curl", {"url": "http://shop.lab/ ; id"})`
    - ftp `("curl", {"url": "ftp://shop.lab/"})`
    - 대역 밖 `("curl", {"url": "http://shop.lab/", "resolve_ip": "8.8.8.8"})`
    - DNS 서버 아님 `("dig", {"domain": "shop.lab", "server": "10.20.0.10"})`
    - dict 아님 `("dig", '{"domain": "shop.lab"}')`
    - 모두 오류 문구(`str`, 비어 있지 않음).
  - `test_bool_is_not_an_int`: `("openssl", {"host": "shop.lab", "port": True})`와 `("ping", {"host": "shop.lab", "count": False})`가 오류.
  - `test_specs_shape_and_no_clues`: `specs(["dig", "curl"])`가 두 개이고 이름순이다. `dig`의 `required == ["domain"]`이고 `additionalProperties is False`. 모든 description에 `find_clues(...) == []`.
  - `test_missing_parts`: `missing_parts("dig", "ANSWER: 1 ...\n;; Query time: 3 msec") == []`이고 `missing_parts("capture", "1 IP ...")`는 비어 있지 않다.
- [ ] **Step 2:** `uv run pytest tests/test_tools.py -v`를 실행한다. 기대: FAIL(ImportError).
- [ ] **Step 3:** `netrunner/tools.py`를 구현한다. kind별 판정은 kind → 판정 함수 dict로 둔다. 새 kind는 dict에 한 줄 추가로 끝나게 한다.
- [ ] **Step 4:** `uv run pytest -q`를 실행한다. 기대: 통과.
- [ ] **Step 5:** 커밋한다. `도구 목록 tools.py: 이름·인자·허용 값과 LLM용 설명`

### Task 3: 도구 창구 `Toolbox`

**Files:**
- Create: `netrunner/toolbox.py`
- Modify: `netrunner/lab.py`: `ProbeRunner` Protocol만 이 Task에서 추가한다. `class ProbeRunner(Protocol): def run_probe(self, tool: str, args: dict) -> ProbeResult`
- Test: `tests/test_toolbox.py` (신설)

**Interfaces:**
- Consumes: `ProbeResult`, `ProbeView`, `Probe`, `Limits`(contracts), `LabInfo`, `tools.check_args`, `tools.specs`, `tools.TOOLS`, `ProbeRunner`.
- Produces:
  - `MAX_OUTPUT_CHARS = 20_000`
  - `RejectedCall(tool: object, args: object, reason: str)`: dataclass.
  - `Toolbox(runner: ProbeRunner | None, lab: LabInfo, limits: Limits, allowed: frozenset[str], clock: Callable[[], float] = time.monotonic)`
  - 속성: `record: list[Probe]`, `rejected: list[RejectedCall]`
  - `specs() -> list[dict]`: 사용 가능 = `allowed ∩ set(lab.enabled_tools) ∩ set(TOOLS)`.
  - `call(name: str, args: dict) -> ProbeView`
  - `remaining() -> int`
  - `close() -> None`

`call`의 검사 순서와 결과:
1. 닫힘
2. 이름이 문자열이고 사용 가능 목록에 있음
3. `check_args`
4. 실행 횟수 < `limits.max_tool_calls`
5. `clock() - 시작 < limits.max_seconds`(시작은 생성 시각)
6. runner가 있음
7. `runner.run_probe` 호출. 예외는 `"실험망 오류: <예외 이름>"`으로 거부.
8. `result.ok`가 False면 `result.error`로 거부.
9. 통과하면 `p<n>`(1부터)을 붙여 `Probe(probe_id, tool, args, output, elapsed_ms, packets, file)`로 기록하고, 같은 값의 `ProbeView`를 돌려준다.

- 출력이 `MAX_OUTPUT_CHARS`를 넘으면 앞부분만 남기고 `"\n…(출력 잘림: 원래 N자)"`를 붙인다. 기록과 반환 모두 같은 잘린 값이다.
- 거부는 `ProbeView(probe_id=None, error=이유)`로 돌려주고 `rejected`에 남긴다. 거부된 호출에서는 runner를 부르지 않고 횟수에도 넣지 않는다.

- [ ] **Step 1:** 실패하는 테스트를 쓴다. 테스트 안에 `FakeRunner`를 둔다. 받은 호출을 `calls`에 쌓고 정해진 `ProbeResult`를 돌려준다.
  - `test_call_runs_and_records_with_ids`: dig 두 번 → `probe_id`가 `"p1"`, `"p2"`이고, `record`의 output이 runner 출력과 같다.
  - `test_rejections_never_reach_runner` (parametrize):
    - 모르는 도구
    - 허용 방식 밖(`allowed=frozenset()`)
    - `enabled_tools` 밖(`openssl`)
    - 잘못된 인자
    - 각각 `probe_id is None`, `error`가 있음, `runner.calls == []`, `len(rejected) == 1`.
  - `test_garbage_call_is_rejected_not_raised`: `call(None, None)`과 `call("dig", '{"domain":"shop.lab"}')`가 예외 없이 거부된다.
  - `test_tool_limit`: `Limits(max_tool_calls=1)`에서 두 번째 호출이 거부되고 `remaining() == 0`.
  - `test_time_limit`: 가짜 clock(0 → 301)과 `Limits(max_seconds=300)`에서 거부.
  - `test_runner_failure_and_exception_are_rejections`: `ok=False`인 결과와 `RuntimeError`가 모두 거부되고 `record == []`.
  - `test_no_runner_rejects`: `Toolbox(None, ...)`이면 거부.
  - `test_output_truncated`: 출력 25,000자 → 기록 output이 20,000자 + 표시이고 `packets`는 그대로.
  - `test_closed_toolbox_rejects_calls`: `close()` 뒤 호출하면 runner를 부르지 않는다.
  - `test_specs_lists_only_usable_tools`: `allowed`가 전체이고 `enabled_tools`가 `["dig"]`이면 specs 이름은 `["dig"]`.
- [ ] **Step 2:** `uv run pytest tests/test_toolbox.py -v`를 실행한다. 기대: FAIL(ImportError).
- [ ] **Step 3:** `Toolbox`와 `ProbeRunner`를 구현한다.
- [ ] **Step 4:** `uv run pytest -q`를 실행한다. 기대: 통과.
- [ ] **Step 5:** 커밋한다. `심판 쪽 도구 창구 Toolbox: 검사·한도·기록`

### Task 4: 재생 실행기와 재생 진단기

**Files:**
- Modify: `netrunner/lab.py`(`ReplayRunner`), `netrunner/diagnoser.py`(Protocol, `ReplayDiagnoser`)
- Test: `tests/test_toolbox.py`

**Interfaces:**
- Consumes: `Toolbox`(Task 3), `Probe.from_dict`, `EVIDENCE_REF`.
- Produces:
  - `ReplayRunner(saved: list[Probe])`, `ReplayRunner.from_diagnoses(diagnoses: list[dict]) -> ReplayRunner`(각 진단 dict의 `trace`), `run_probe(tool, args) -> ProbeResult`.
    - (도구, 인자 dict)가 정확히 같고 아직 쓰지 않은 첫 저장 검사를 돌려준다(`ok=True`, output·elapsed_ms·packets·file 그대로).
    - 없으면 `ProbeResult(False, error="저장된 출력 없음")`.
  - `Diagnoser.diagnose(self, brief: RoundBrief, tools: Toolbox) -> Diagnosis`
  - `ReplayDiagnoser.diagnose(brief, tools)`:
    1. 저장 dict를 복사해 `round_id`를 바꾸고 `Diagnosis.from_dict`로 형식을 검사한다(`ContractError`는 그대로 낸다).
    2. 저장 `trace`를 순서대로 `tools.call(p.tool, p.args)`로 요청해 옛 id → 새 id 표를 만든다.
    3. 모든 인용의 id를 바꾼다(`probe:<id>`, `pcap:<id>#…`). 표에 없는 id는 `unrun-<id>`로 바꿔 새 번호와 겹치지 않게 한다.
    4. `trace=[]`로 돌려준다.

- [ ] **Step 1:** 실패하는 테스트를 쓴다.
  - `test_replay_runner_returns_saved_outputs_in_order`: 같은 (dig, 인자)가 두 번 저장되면 첫 호출과 둘째 호출이 각 출력을 돌려주고, 셋째는 `ok=False`.
  - `test_replay_diagnoser_remaps_ids`: 저장 trace가 `p2`, `p7`이고 인용이 `probe:p7`, `pcap:p2#1`이면 결과 인용은 `probe:p2`, `pcap:p1#1`이다(창구가 p1, p2를 붙임).
  - `test_unrun_refs_stay_missing`: 인용 `probe:p9`(trace에 없음) → `probe:unrun-p9`.
  - `test_malformed_saved_diagnosis_still_contract_error`: 기존 형식 오류 사례 하나가 `ContractError`를 낸다.
- [ ] **Step 2:** `uv run pytest tests/test_toolbox.py -v`를 실행한다. 기대: FAIL.
- [ ] **Step 3:** `ReplayRunner`와 새 `ReplayDiagnoser`를 구현하고 Protocol 시그니처를 바꾼다. 인용 id 변환은 `diagnoser.py`의 내부 함수 `_remap_ref(ref: str, ids: dict[str, str]) -> str`로 하고, `EVIDENCE_REF`를 쓴다.
- [ ] **Step 4:** `uv run pytest tests/test_toolbox.py -q`를 실행한다. 기대: 통과. 심판이 아직 `diagnose(brief)`로 부르므로, 이 커밋에서는 `tests/test_judge.py`, `tests/test_run.py`, `tests/test_breaker.py`의 라운드 테스트가 깨진다(진단 실행 오류로 무효 처리). Task 5까지 이어서 하고, 이 상태로 푸시하지 않는다.
- [ ] **Step 5:** 커밋한다. `재생 실행기·재생 진단기를 창구 경유로 전환`

### Task 5: 심판·채점·결과 화면을 창구 기록으로 전환

**Files:**
- Modify: `netrunner/referee.py`, `netrunner/scoring.py`, `netrunner/report.py`
- Modify: `fixtures/first_integration/*.diagnosis.json`, `fixtures/scoring_examples/*.diagnosis.json`: trace 인자 형식만 고친다. 출력은 그대로 둔다.
- Modify: `tests/test_judge.py`
  - `run_one(lab=None, diag=None, sc=None, runner=None)`: 기본은 `ReplayDiagnoser([R2_DIAG])` + `ReplayRunner.from_diagnoses([R2_DIAG])`.
  - Spy와 Boom의 `diagnose(self, brief, tools)`.
  - `score_of`가 `probes=Diagnosis.from_dict(diag).trace`를 넘긴다.
  - `Referee(...)`를 직접 만드는 곳에 맞는 `runner`를 준다.

**Interfaces:**
- Consumes: Task 2~4 전부.
- Produces:
  - `Referee(lab, diagnoser, lab_info, limits=None, records_path=None, runner: ProbeRunner | None = None)`: 속성 `runner`는 진입점이 라운드마다 바꿀 수 있다.
  - 라운드 기록에 `"probes": []`, `"rejected_calls": []` 키를 처음부터 둔다.
  - `score(scenario, observable, diagnosis, elapsed_s, limits=None, *, probes: list[Probe]) -> Score`: `diagnosis.trace`를 읽지 않는다. `tool_calls == len(probes)`이고, 검사 횟수 위반 검사는 뺀다(창구가 막는다).

심판 4단계:
1. `allowed = tools_for_method(self.diagnoser.method)`. `None`이면 `end("invalid", "알 수 없는 진단 방식: <method>")`.
2. `toolbox = Toolbox(self.runner, self.lab_info, self.limits, allowed)`
3. `diagnose(brief, toolbox)`
4. `finally`에서 `toolbox.close()`를 부르고, `rec["probes"]`와 `rec["rejected_calls"]`를 `asdict`로 채운다. Diagnoser가 예외를 내도 채운다.
5. `score(..., probes=toolbox.record)`

`report.py`는 `r["probes"]`가 있으면 그것을, 없으면(옛 기록) `r["diagnosis"]["trace"]`를 쓴다.

fixtures 인자 변환:

| 원래 | 바뀐 형식 |
|---|---|
| `dig {"name": X}` | `{"domain": X}` |
| `dig {"name": X, "server": IP}` | `{"domain": X, "server": IP}` |
| `curl {"url": U, "timeout_s": …}` | `{"url": U}` |
| `curl {"url": U, "resolve": "shop.lab:80:10.20.0.10"}` | `{"url": U, "resolve_ip": "10.20.0.10"}` |
| `tcpdump {...}` | `tool: "capture"`, `args: {"url": "http://shop.lab/"}`. `packets`는 그대로 |

- [ ] **Step 1:** 실패하는 테스트를 `tests/test_judge.py`에 쓴다.
  - `test_self_reported_trace_is_not_evidence`: 도구를 부르지 않고 `trace`에 `probe_id="p1"`인 출력을 직접 적어 `probe:p1`을 인용하는 진단기 → `rec["score"]["evidence"][0]["exists"] is False`.
  - `test_probes_recorded_from_toolbox`: 기본 `run_one()` → `[p["probe_id"] for p in rec["probes"]] == ["p1", "p2", "p3"]`이고 `rec["score"]["cause_correct"] == 1`.
  - `test_unknown_method_is_invalid`: `method = "Z"` → `rec["status"] == "invalid"`이고 `reset_verified`가 True.
  - `test_method_b_gets_no_tools`: `method = "B"`인 진단기가 받은 `tools.specs() == []`.
  - `test_probes_kept_when_diagnoser_raises`: dig 한 번 부른 뒤 예외 → `len(rec["probes"]) == 1`, `status == "invalid"`, `reset_verified`가 True.
  - `test_toolbox_closed_after_round`: 진단기가 받은 창구를 보관했다가 라운드 뒤 `call` → `probe_id is None`.
  - `test_report_falls_back_to_old_trace`: `probes` 키를 지운 옛 기록으로 `render_html`을 부르면 인용된 원본 출력이 화면에 있다.
- [ ] **Step 2:** `uv run pytest tests/test_judge.py -v`를 실행한다. 기대: 새 테스트가 FAIL.
- [ ] **Step 3:** 심판·채점·보고를 고치고, fixtures 인자를 변환하고, 기존 테스트 도우미를 위 Files대로 맞춘다.
- [ ] **Step 4:** `uv run pytest -q`를 실행한다. 기대: 전부 통과. `test_breaker.py`와 `test_docker_lab.py`는 고치지 않는다. `runner` 없이 만든 심판은 도구 없이 라운드를 돌기 때문에 두 파일이 기대하는 상태와 정상화 결과는 그대로다.
- [ ] **Step 5:** 커밋한다. `심판·채점·결과 화면을 창구 기록 기준으로 전환, fixtures 인자 형식 정리`

### Task 6: 진입점 조립, 시연 수치 유지, 문서

**Files:**
- Modify: `netrunner/__main__.py`, `docs/interfaces.md`, `CLAUDE.md`(구조 목록에 `tools.py`, `toolbox.py` 한 줄씩)
- Test: `tests/test_run.py`

**Interfaces:**
- Consumes: `Referee.runner`, `ReplayRunner.from_diagnoses`.
- Produces:
  - `PROBES_FROM_LAB: frozenset[str] = frozenset()`: 실험망이 `run_probe`를 구현한 구현 이름들. `DockerLab.run_probe`가 생기면 양시찬이 `"docker"`를 넣는다.
  - `run_fixed_rounds(referee, scenario_files, repeat=1, replay_probes=True)`: `replay_probes`면 라운드마다 `referee.runner = ReplayRunner.from_diagnoses([저장 진단] 또는 [])`.
  - `cmd_run`은 `runner = lab if args.impl in PROBES_FROM_LAB else None`, `replay_probes = args.impl not in PROBES_FROM_LAB`.

- [ ] **Step 1:** 실패하는 테스트를 `tests/test_run.py`에 쓴다.
  - `test_mock_run_keeps_metrics`: `main(["run", "--impl", "mock", "--repeat", "3", "--out", tmp])`의 기록을 `summarize`한 첫 행이 다음과 같다.
    - `rounds == "6/6 (100%)"`, `cause_acc == "6/6 (100%)"`
    - `evidence == "9/15 (60%)"`, `evidence_unverified == 6`
    - `tool_calls_med == 3.5`
    - `repro == "6/6 (100%)"`, `reset_ok == "6/6 (100%)"`
    - 모든 기록의 `probes`가 비어 있지 않다.
- [ ] **Step 2:** `uv run pytest tests/test_run.py -v`를 실행한다. 기대: FAIL. `probes`가 비어 있거나 근거 수치가 다르다.
- [ ] **Step 3:** 진입점을 조립한다. `docs/interfaces.md`를 고친다.
  - 3·4절: `diagnose(brief, tools)`, `trace`를 채점에 쓰지 않음.
  - "검사 도구" 절 신설: 5.2절 표, `specs()`/`call()`/`ProbeView`, 방식별 묶음, 거부 규칙.
  - 2절: `ProbeRunner`, `enabled_tools`, `hosts`.
- [ ] **Step 4:** 다음을 모두 통과시킨다.
  - `uv run pytest -q`
  - `uv run --python 3.10 --isolated --with pytest pytest -q -p no:cacheprovider`
  - `uv run ruff check`
  - `uv run ruff format --check`
  - `uv run netrunner demo`가 표의 증상 재현 2/2, 초기화 2/2, 근거 3/5를 출력한다.
  - Docker가 켜져 있으면 `NETRUNNER_INTEGRATION=1 uv run pytest -q`도 통과한다.
- [ ] **Step 5:** 커밋한다. `진입점에서 재생 실행기 조립, 인터페이스 문서에 검사 도구 계약 반영`

### Task 7 (조건부): PR #8이 main에 합쳐진 경우 `HttpDiagnoser` 모양 맞추기

PR #8이 아직 합쳐지지 않았으면 건너뛴다. PR #9 본문 "알아 둘 점"에 "PR #8 병합 뒤 반영"이라고 적는다.

**Files:**
- Modify: `netrunner/diagnoser.py`(`HttpDiagnoser.diagnose` 시그니처만)
- Modify: PR #8이 추가한 HttpDiagnoser 테스트의 호출부(인자 하나 추가)만

**Interfaces:**
- Consumes: `Toolbox`(Task 3).
- Produces: `HttpDiagnoser.diagnose(self, brief: RoundBrief, tools: Toolbox) -> Diagnosis`. `tools`는 쓰지 않는다. 검사는 지금처럼 `self._probe`로 한다(이현섭이 4번 작업에서 전환).

- [ ] **Step 1:** `git merge origin/main`으로 Sejoong을 main에 맞춘다.
- [ ] **Step 2:** `uv run pytest -q`를 실행한다. 기대: HttpDiagnoser 관련 테스트가 TypeError로 FAIL.
- [ ] **Step 3:** 시그니처를 바꾸고, 테스트 호출부에 `Toolbox(None, LAB, Limits(), frozenset())`를 넘긴다.
- [ ] **Step 4:** `uv run pytest -q`와 `uv run ruff check`를 실행한다. 기대: 통과.
- [ ] **Step 5:** 커밋하고, PR #9 본문 맨 앞에 "이현섭 코드 변경: `HttpDiagnoser.diagnose` 시그니처만"을 적는다.
