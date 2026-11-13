# 검사 도구 계약 설계: 심판이 기록하는 도구 창구

작성: 김세중(심판과 통합) · 2026-11-13 · 상태: 김세중 승인, 양시찬·이현섭 리뷰 중

## 1. 왜 하는가

12주차 목표는 "실제 검사 도구와 Diagnoser를 연결"이다. 그러려면 Diagnoser가 실험망에서 dig, curl, 캡처 같은 검사를 어떻게 실행하는지 먼저 정해야 한다. 이게 정해지면 Diagnoser(이현섭)와 실험망 도구(양시찬)를 서로 기다리지 않고 따로 만들 수 있다.

지금 계약의 문제는 두 가지다.

- **근거 확인이 자기 신고에 기대고 있다.** Diagnoser가 `Diagnosis.trace`에 검사 출력을 직접 적고, 심판은 인용한 `probe:p1`이 그 trace 안에 있는지만 본다. 실행하지 않은 출력을 trace에 적어도 심판은 알 수 없다. 설계안 7.2절의 핵심 주장인 "근거를 확인해 환각을 걸러 낸다"가 여기서 약해진다.
- **검사 실행 방법이 실험망마다 다르다.** PR #8의 `HttpDiagnoser`는 HTTP 실험망에만 있는 `lab.probe()`를 부른다. `DockerLab`은 `_dns`, `_curl`, `_capture`를 갖고 있지만 내부 함수다. 두 실험망을 합치면 진단기가 쓸 공통 창구가 없다.

## 2. 목표

- **G1. 모든 검사는 심판이 만든 창구를 거친다.** 창구가 실제 출력을 기록하고, 채점은 이 기록으로만 한다. Diagnoser가 스스로 적은 trace는 채점에 쓰지 않는다.
- **G2. 도구 목록은 한 곳에서 정한다.** 이름, 인자, 허용 값이 `faults.py`처럼 단일 출처에 있다. LLM에 넘길 도구 설명(JSON 스키마)도 여기서 만든다.
- **G3. 실험망은 함수 하나로 검사를 처리한다.** 새 도구를 추가할 때 실험망은 그 이름 하나만 처리하면 되고, `Lab` 네 함수는 그대로 둔다.
- **G4. 설계안 9.2절 비교 방식에 맞춰 도구 묶음을 바꿀 수 있다.**
- **G5. 지금 있는 시연과 테스트는 계속 돈다.** `demo`, `run --impl mock`, `run --impl docker`, `fixtures/`가 깨지지 않는다.

**비목표(이번에 하지 않음)**

- LLM 연결부(opencode GLM 호출). 별도 작업이다.
- Diagnoser의 LLM 반복 구조와 캡처 요약기. 이현섭 담당이다.
- 부하 측정 도구, MCP 서버 노출.
- 방식 B의 "curl 에러 한 줄" 제공 방식(아래 9장).

**반목표(동작해도 이러면 실패다)**

- Diagnoser가 임의 명령을 실행할 수 있다. 셸 문자열이나 자유 인자를 받으면 안 된다.
- 도구 출력이나 오류 메시지에 장애 ID, 주입 내용, 시나리오 이름이 드러난다. Diagnoser는 클라이언트에서 보이는 것만 본다.
- Diagnoser가 창구를 거치지 않고 실험망을 직접 만질 수 있다(주입·복구 함수 포함).
- 검사가 실험망 상태를 바꾼다. 도구는 관측만 한다.

## 3. 제약

- 표준 라이브러리만 쓴다. Python 3.10 이상. `ruff check`와 `ruff format`을 통과해야 한다.
- `contracts.py`, `docs/interfaces.md`, `lab.py`의 규약을 바꾸는 계약 변경이다. 양시찬·이현섭 승인 뒤에 합친다.
- PR #8(이현섭)이 `diagnoser.py`를 고치는 중이다. Diagnoser 규약 변경도 같은 파일이라, PR #8이 먼저 합쳐진 뒤 구현한다(6장).
- PR #8을 먼저 합치므로, 규약 변경 PR은 `HttpDiagnoser`의 함수 모양만 최소한으로 맞춘다. 이현섭을 리뷰어로 지정하고, PR 본문 맨 앞에 바꾼 부분을 밝힌다. 검사 방식 전환은 이현섭이 한다.
- 다른 사람의 브랜치를 가져오지 않는다. 각 담당자가 자기 브랜치에서 PR을 올린다.

## 4. 검토한 방식

**누가 검사 기록을 갖나**

| 방식 | 내용 | 결과 |
|---|---|---|
| 심판이 기록 | 심판이 만든 창구로만 검사하고, 창구가 출력을 기록 | **채택**. 지어낸 근거를 잡을 수 있다 |
| Diagnoser가 기록(지금) | 도구 목록만 정하고 실행과 기록은 각자 | 기각. 바뀌는 게 적지만 거짓 trace를 못 잡는다 |

**코드를 어떻게 나누나**

| 방식 | 내용 | 결과 |
|---|---|---|
| A. 실험망에 `run_probe` 하나 + 심판 쪽 창구 | 도구 목록은 `tools.py`, 실행은 실험망의 `run_probe(tool, args)`, 검사·한도·기록은 `Toolbox` | **채택**. 도구 추가가 목록 한 줄과 실험망의 이름 하나로 끝난다 |
| B. `Lab`에 도구별 함수 추가 | `dig()`, `curl()` 등을 규약에 추가 | 기각. 도구마다 모든 실험망과 모의 실험망을 고쳐야 하고, LLM 도구 설명을 따로 맞춰야 한다 |
| MCP 서버로 노출 | 도구를 별도 프로세스 서버로 제공 | 기각. 기존 에이전트를 붙이기는 쉽지만, 심판 기록과 연결하기가 복잡하고 이번 범위를 넘는다 |

## 5. 설계

### 5.1 구성과 흐름

```
Referee ──라운드마다 생성──▶ Toolbox(brief.limits, 방식별 허용 도구, LabInfo, runner)
   │                            │  call(name, args)
   │ diagnose(brief, toolbox)   ├─ 1) tools.py 목록으로 이름·인자 검사
   ▼                            ├─ 2) 방식 허용 여부, 횟수·시간 한도 확인
Diagnoser ──toolbox.call()────▶ ├─ 3) runner.run_probe(tool, args) 실행 (클라이언트 컨테이너)
                                ├─ 4) probe_id 부여, 실제 출력을 기록
                                └─ 5) ProbeView(probe_id, output, ...) 반환
Referee ── 채점: toolbox.record로 인용 존재·지지 판정 ── 기록: rec["probes"]
```

- `runner`는 `ProbeRunner` 규약을 따르는 객체다. `DockerLab`은 `Lab`과 `ProbeRunner`를 모두 구현한다. 조립은 진입점(`__main__.py`)에서 한다.
- Diagnoser는 `Toolbox`만 받는다. `Lab`이나 `runner`에는 접근할 수 없다.

### 5.2 도구 목록 (`netrunner/tools.py`, 신설)

도구마다 이름, 설명, 인자(이름, 종류, 필수 여부, 허용 값 규칙), 출력에 반드시 들어갈 내용을 데이터로 둔다. 허용 값은 `LabInfo`(도메인, 호스트, 대역)로 판정한다. 첫 버전 목록:

| 도구 | 인자 | 허용 값 | 출력에 반드시 포함 | 첫 구현 |
|---|---|---|---|---|
| `dig` | `domain`(필수), `server`(선택) | domain: 실험망 도메인 / server: `dns`, `alt_dns` 또는 그 IP. 생략하면 기본 DNS | ANSWER 섹션과 `Query time` (짧은 형식 금지) | 12주차 |
| `curl` | `url`(필수), `resolve_ip`(선택) | url: http/https, 호스트가 실험망 도메인 / resolve_ip: 실험망 대역 IP | 종료 코드, `-v` 진행 기록, 단계별 시간(namelookup, connect, appconnect, starttransfer, total), HTTP 상태 | 12주차 |
| `capture` | `url`(필수) | curl과 같음 | 캡처하는 동안 요청 한 번. 패킷마다 줄 머리 `#<n>`. `packets`(총 수), `file`(pcap 경로) | 12주차 |
| `openssl` | `host`(필수), `port`(선택, 기본 443), `servername`(선택) | host·servername: 실험망 도메인 / port: 1–65535 | 인증서 주체·만료일, 검증 결과, alert | 12주차(TLS) |
| `ping` | `host`(필수), `count`(선택, 1–10, 기본 4) | 실험망 도메인 또는 실험망 대역 IP | 왕복 시간, 손실률 | 12주차(경로) |
| `tracepath` | `host`(필수) | ping과 같음 | 홉 목록, 경로 MTU | 12주차(경로) |

- 인자는 정해진 키만 받는다. 모르는 키나 허용 범위를 벗어난 값이 오면 거부한다.
- `tools.py`가 이 목록으로 LLM용 도구 설명(OpenAI `tools` 형식의 JSON 스키마)을 만든다. 설명 문구에 장애 이름이나 원인 단서를 넣지 않는다.
- 출력 형식(어떤 플래그를 쓰는지)은 실험망 담당이 정한다. 목록의 "반드시 포함" 칸만 지키면 된다. 그 신호를 `faults.py`의 `signals`가 판정에 쓴다.
- **"반드시 포함"은 창구가 실행 중에 검사하지 않는다(김세중 판단).** 실험망 담당이 지킬 명세다. `tools.py`가 확인용 함수를 제공하고, 실험망 테스트에서 이 함수로 확인한다. 실행 중에 검사하면 도구마다 출력 검사기를 따로 만들어야 하고, 저장된 진단 출력도 모두 고쳐야 하기 때문이다.
- **실험망이 실행할 수 있는 도구는 `config/lab.json`의 `enabled_tools`에 적는다(김세중 판단).** `enabled_faults`와 같은 방식이다. `specs()`는 "방식별 허용 도구 ∩ `enabled_tools`"만 LLM에 보여 준다. 그래서 아직 구현되지 않은 도구에 LLM 호출을 낭비하지 않고, C와 C-1 비교도 왜곡되지 않는다. 목록에 없는 도구를 부르면 "실험망 미지원"으로 거부한다.

### 5.3 실험망 쪽 규약: `ProbeRunner` (`lab.py`) 와 `ProbeResult` (`contracts.py`)

```python
class ProbeRunner(Protocol):
    def run_probe(self, tool: str, args: dict) -> ProbeResult: ...


@dataclass
class ProbeResult:
    ok: bool  # 실험망이 명령을 실행했는가 (네트워크 결과의 성패가 아님)
    output: str  # 클라이언트 컨테이너에서 나온 원본 출력
    elapsed_ms: int = 0
    packets: int | None = None  # capture일 때만
    file: str | None = None  # capture일 때만
    error: str = ""  # ok=False일 때 이유. 장애 정보를 넣지 않는다
```

- `run_probe`는 인자가 이미 검사된 상태로 불린다. 그래도 실험망은 클라이언트 컨테이너 밖에서 실행하지 않는다.
- 연결 거부처럼 네트워크가 실패한 결과는 `ok=True`이고 출력에 그 실패가 담긴다. `ok=False`는 실험망이 실행하지 못한 경우(지원하지 않는 도구, docker 오류 등)다.
- 예외를 내도 창구가 받아 `ok=False`로 처리한다.

### 5.4 심판 쪽 창구: `Toolbox` (`netrunner/toolbox.py`, 신설)

Diagnoser가 쓰는 함수:

- `specs() -> list[dict]`: 이 라운드에 허용된 도구의 LLM용 설명.
- `call(name, args) -> ProbeView`: 검사 한 번. `ProbeView`는 `probe_id`(실행된 경우), `output`, `packets`, `file`, `error`를 가진다.
  - `file`은 capture일 때 pcap 경로다. 캡처 요약기는 Diagnoser 쪽에 있어서(C·C-2 비교), 요약기가 pcap을 직접 읽을 수 있게 넘긴다(김세중 판단). 파일은 저장소의 `captures/`에 있어서 Diagnoser 프로세스에서 읽을 수 있다.
- `remaining() -> int`: 남은 검사 횟수.

창구가 하는 일:

1. `tools.py`로 이름과 인자를 검사하고, 방식별 허용 도구인지 확인한다.
2. 검사 횟수(`limits.max_tool_calls`)와 시간(`limits.max_seconds`, 라운드 진단 시작 기준)을 확인한다.
3. 통과하면 `runner.run_probe`를 부르고 `p1`, `p2` 순서로 `probe_id`를 붙여 `Probe`로 기록한다.
4. 거부된 호출은 `probe_id` 없이 이유를 돌려주고 별도 목록에 남긴다.

**횟수 규칙(김세중 판단):** 실험망이 실행한 검사만 한도에 센다. 연결 거부처럼 네트워크가 실패한 결과도 실행된 검사로 센다. 거부된 호출(모르는 도구, 잘못된 인자, 허용 안 됨, 한도 초과, 실험망 미지원)은 세지 않는다. 대신 거부 횟수도 기록에 남겨 분석할 수 있게 한다.

**출력 길이(김세중 판단):** 한 검사의 출력이 20,000자를 넘으면 앞부분만 남기고 잘렸다고 표시한다. `packets`는 원래 수를 유지한다.

### 5.5 Diagnoser 규약 변경 (`diagnoser.py`)

```python
def diagnose(self, brief: RoundBrief, tools: Toolbox) -> Diagnosis: ...
```

- `Diagnosis.trace`는 남겨 두지만 채점에 쓰지 않는다. 이전 기록과 호환하려고 둔다. 새 구현은 비워도 된다.
- 근거 인용 형식(`probe:<id>`, `pcap:<id>#n-m`)은 그대로다. `<id>`는 창구가 붙인 번호다.

### 5.6 채점·기록·결과 화면

- 라운드 기록에 `probes`(창구 기록, `Probe` 목록)와 `rejected_calls`(거부된 호출)를 추가한다.
- `scoring.score`는 `diagnosis.trace` 대신 창구 기록으로 인용 존재·지지를 판정한다. `tool_calls`는 실행된 검사 수다.
- `report.py`는 `rec["probes"]`의 출력을 보여 준다. `probes`가 없는 옛 기록은 `diagnosis.trace`로 대신 보여 준다(호환).

### 5.7 방식별 도구 묶음 (`tools.py`)

| 방식 | 허용 도구 |
|---|---|
| A 규칙 기반 | 전체 |
| B 도구 없는 LLM | 없음 |
| C 전체 | 전체 |
| C-1 캡처 제외 | `capture` 제외 전체 |
| C-2 요약기 제외 | 전체(요약기 적용 여부는 Diagnoser 쪽 일) |

심판은 `diagnoser.method`로 묶음을 고른다. 모르는 방식이면 라운드를 무효로 기록한다.

### 5.8 저장된 진단 재생: 지금 시연과 테스트 유지

- **`ReplayDiagnoser`:** 저장된 진단의 `trace`에 있는 검사를 그 순서대로 `tools.call`로 다시 요청한다. 그런 다음 저장된 인용의 옛 번호를 창구가 붙인 새 번호로 바꿔서 내보낸다. 저장된 trace에 없는 번호를 가리키는 인용(일부러 넣은 가짜 근거)은 바꾸지 않아서 계속 "존재하지 않음"으로 판정된다.
- **`ReplayRunner`(`lab.py`, 모의용):** 저장된 trace의 (도구, 인자)에 맞는 저장 출력을 돌려주는 `ProbeRunner`다. `MockLab`과 함께 `demo`와 `run --impl mock`에 쓴다.
- **`fixtures/`:** 저장된 진단의 trace 인자를 5.2 목록 형식으로 고친다. 예를 들어 `dig {"name": …}`은 `{"domain": …}`로, `tcpdump`는 `capture`로 바꾼다.
- **`run --impl docker`:** `DockerLab.run_probe`가 생기기 전까지는 `ReplayRunner`를 쓴다. 그 사이 기록의 모델 이름에 재생임을 남긴다. `run_probe`가 생기면 진입점의 조립만 바꾼다.

## 6. 담당과 순서

| 순서 | 담당 | 브랜치·PR | 내용 |
|---|---|---|---|
| 0 | 전원 | - | 이 문서 합의(양시찬·이현섭 승인) |
| 1 | 이현섭 | Hyeonseop, PR #8 | 테스트 추가와 docstring 되돌리기를 반영한 뒤 병합 |
| 2 | 김세중 | Sejoong | `contracts.py`(`ProbeResult`), `lab.py`(`ProbeRunner`, `ReplayRunner`), `tools.py`, `toolbox.py`, `config/lab.json`(`enabled_tools`), Diagnoser 규약 변경과 `ReplayDiagnoser` 이행, 심판·채점·보고, `fixtures` 인자 형식, `docs/interfaces.md`, 테스트. `HttpDiagnoser`는 `diagnose(brief, tools)` 모양만 맞춘다(이현섭 리뷰) |
| 3 | 양시찬 | Sichan | `DockerLab.run_probe`: `dig`, `curl`, `capture`(기존 `_dns`, `_curl`, `_capture` 활용). 실제 실험망 통합 테스트 |
| 4 | 이현섭 | Hyeonseop | `HttpDiagnoser`를 `tools.call("curl", …)`로 전환. HTTP 실험망의 `lab.probe` 제거. H1을 공용 실험망으로 옮기는 일은 양시찬과 조율 |

3과 4는 2가 합쳐진 뒤 서로 기다리지 않고 진행할 수 있다.

## 7. 완료 확인

- 모의 실험망: `uv run netrunner demo`와 `run --impl mock --repeat 3`의 지표가 지금과 같다. 증상 재현·초기화는 6/6, 근거 유효율은 지금과 같은 값이다.
- 거짓 근거: 저장된 trace에 없는 출력을 인용한 진단은 "존재하지 않음"으로 판정된다. `scoring_examples/x1`.
- 창구 경계: 모르는 도구, 허용 밖 도메인, 셸 문자 섞인 인자, 한도 초과, 방식 B의 도구 호출이 모두 거부되고, 실험망이 불리지 않는다.
- 실제 실험망(3 이후): `run --impl docker`에서 `ReplayRunner` 대신 `DockerLab.run_probe`로 같은 근거가 실제 출력에서 지지된다.

## 8. 빌더에게 맡기는 것

- `ProbeView`, `ProbeResult`의 정확한 필드 이름과 위치. 위 의미만 지키면 된다.
- 거부 사유 문구, 출력 잘림 표시 형식.
- `ReplayRunner`가 (도구, 인자)를 맞추는 방식. 정확히 일치를 기본으로 하고, 못 찾으면 `ok=False`.
- 옛 번호를 새 번호로 바꿀 때 번호 충돌을 피하는 방식.
- 테스트 구성. 위 완료 확인을 덮으면 된다.

## 9. 남은 질문(이 문서 범위 밖)

- 방식 B의 "증상 설명과 curl 에러 한 줄"을 누가 어떻게 제공할지: 심판이 미리 한 번 검사해 brief에 넣을지, B용 도구 하나를 허용할지. 이현섭과 정한다.
- 캡처 요약기의 출력 형식(패킷 번호 유지 방식). 이현섭 담당(인터페이스 문서 회의 항목 5).
