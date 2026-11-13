# 모듈 입출력 계약 v0.1 (10주차 합의안)

작성: 김세중(팀장 · 심판과 통합) · 2026-11-04

10주차 목표는 네 모듈의 입출력과 장애 목록을 합의하는 것이다. 이 문서의 형식은 `netrunner/contracts.py`에 코드로 들어 있다. 각자 만든 JSON이 아래 명령을 통과하면 형식을 지킨 것이다.

```
python -m netrunner validate <시나리오.json>   # Breaker 출력 검사
python -m netrunner demo                        # 첫 통합 시연 두 라운드를 모의 실행
python -m pytest tests                          # 심판 테스트
```

필요한 것은 Python 3.10 이상뿐이다(표준 라이브러리만 사용, 테스트에만 pytest).

## 한 라운드의 순서

심판(`netrunner/referee.py`)이 진행한다.

1. **검증**: 허용 목록, 매개변수 범위, 증상 설명의 원인 단서, 관측 가능성을 검사한다. 거부되면 Breaker가 다시 제안한다(최대 3회). 거부된 제안은 실험망을 건드리지 않는다.
2. **정상 확인**: 실험망 `health_check`. 실패하면 `reset` 후 한 번 더 확인하고, 그래도 실패하면 무효 라운드로 처리한다. 실험망 함수가 예외를 내도 무효 라운드로 기록한다.
3. **주입·재현 확인**: 장애마다 `inject`를 부른 뒤 `verify_symptom`을 부른다. 증상이 나타나지 않으면 무효 라운드다.
4. **진단**: Diagnoser에게는 `RoundBrief`와 도구 창구(`Toolbox`)만 넘긴다. 검사는 창구로만 하고, 창구가 실제 출력을 기록한다. 시나리오 ID, 장애, 주입 로그, 정답은 넘기지 않는다.
5. **채점·정답 공개**
6. **복구·정상화 확인**: `reset` 후 `health_check`를 다시 부른다. 무효 라운드여도 반드시 실행하며, 정상화가 확인되지 않으면 다음 라운드를 돌리지 않는다.
7. **기록**: 라운드마다 JSONL 한 줄을 남긴다. 결과 화면과 Breaker의 기억은 이 기록으로 만든다.

## 1. Breaker → 심판: 시나리오

```json
{
  "scenario_id": "FI-01",
  "target_url": "http://shop.lab/",
  "faults": [{"id": "D2", "params": {"domain": "shop.lab", "ip": "10.20.0.99"}}],
  "complaint": "사이트가 열리지 않아요.",
  "origin": "breaker",
  "note": "사람용 메모. Diagnoser에게는 절대 넘어가지 않음"
}
```

- 장애는 1~3개이며, `faults[].id`는 아래 장애 목록의 ID만 쓸 수 있다.
- `origin`: `fixed`는 고정 세트(사람이 만든 30개, 13주차 동결)이고, `breaker`는 대결 세트다. Breaker가 낸 시나리오는 심판이 항상 `breaker`로 바꿔 기록한다.
- 심판은 다음 경우에 시나리오를 거부한다.
  - 허용 목록 밖의 장애이거나, 정의되지 않은 매개변수가 있거나, 필수 매개변수가 빠졌거나, 범위를 벗어난 경우
  - 함께 걸 수 없는 조합: `{D1,D2,D3,B2}`, `{T1,T2}`, `{S1,S2}`, `{H1,H2}`
  - 관측할 수 없는 대상: `domain`이 대상 URL의 도메인과 다르거나, `port`가 대상 URL의 포트와 다른 경우
  - 증상 설명에 원인 단서가 들어간 경우: DNS, TCP, TLS, MTU, IP, 포트, 인증서, 패킷, 손실, 리다이렉트, 차단, 502 등(`validate.py`의 `CLUE_WORDS`)
- **관측 불가 표시**: 앞 단계에서 요청을 끝내는 장애가 있으면 뒤 단계의 장애는 클라이언트에서 볼 수 없다. 예를 들어 D2가 엉뚱한 IP를 주면 정상 서버에 건 T2는 보이지 않는다. 이런 장애는 거부하지 않고 `observable: false`로 기록해 채점에서 뺀다. 경로 장애(N*)는 ping과 tracepath로 서버 IP에서 직접 볼 수 있으므로 가려지지 않는다.

## 2. 심판 ↔ 실험망: `Lab` 인터페이스

`netrunner/lab.py`의 `Lab`을 만족하는 `netrunner/docker_lab.py`의 `DockerLab`은 D2와 T2를 지원한다. `MockLab`은 모의 실행에 사용한다.

| 함수 | 하는 일 | 반환 |
|---|---|---|
| `health_check(target_url)` | 클라이언트 컨테이너에서 대상 URL로 정상 접속되는지 확인 | `LabResult` |
| `inject(fault)` | 장애 하나 주입. `observations`에 실제로 실행한 명령을 남긴다 | `LabResult` |
| `verify_symptom(scenario)` | 주입 후 클라이언트에서 증상이 나타나는지 확인 | `LabResult` (`ok=True`면 재현됨) |
| `reset()` | 모든 규칙을 지우고 서버 설정을 원래대로 되돌림 | `LabResult` |

`LabResult`는 `{"ok": bool, "detail": str, "observations": {...}}` 형태이며, 측정값과 캡처 파일 경로는 `observations`에 넣는다.

**검사 실행: `ProbeRunner`.** Diagnoser의 검사를 실행하는 쪽은 `run_probe(tool, args) -> ProbeResult` 하나를 구현한다(`netrunner/lab.py`). 인자는 심판의 도구 창구가 미리 검사해서 넘긴다. 실험망은 클라이언트 컨테이너에서만 실행하고, 출력과 `error`에 장애 정보를 넣지 않는다. 인자는 셸 문자열로 합치지 말고 argv로 넘기며, curl에는 `--globoff`를 붙인다(URL 규칙은 `validate.lab_url_error`). `ProbeResult`의 필드는 `ok`, `output`, `elapsed_ms`, `packets`, `file`, `error`다. `ok`는 실험망이 명령을 실행했는지를 뜻한다. 연결 거부처럼 네트워크가 실패한 결과도 `ok=True`이고, 실패 내용은 출력에 담긴다. 실행할 수 있는 도구 목록은 `config/lab.json`의 `enabled_tools`에 적는다. `hosts`의 `dns`, `alt_dns` IP는 `dig`의 `server` 인자 검사에 쓴다. 모의 실행에서는 `ReplayRunner`가 저장된 진단의 검사 출력을 돌려준다.

실험망 주소와 구현된 장애 목록(`enabled_faults`)은 `config/lab.json`에 있다. 클라이언트 대역은 `10.21.0.0/24`, 서버 대역은 `10.20.0.0/24`다. 첫 구현은 단일 장애와 HTTP 80번 포트만 재현한다. 실행 방법과 기록 위치는 README를 참고한다.

`DockerLab.verify_symptom`의 `observations.capture`에는 원본 `stdout`, 패킷 번호를 표시한 `output`, 총 패킷 수 `packets`, 파일 경로 `file`을 넣는다. `lab-check`는 실험망 검증 전용이며 이 값을 Diagnoser의 Probe로 자동 제출하지 않는다. 캡처 종료를 확인하지 못하면 초기화 성공으로 처리하지 않는다.

## 3. 심판 → Diagnoser: `RoundBrief`

```json
{
  "round_id": "3f9a1c0e",
  "target_url": "http://shop.lab/",
  "complaint": "사이트가 열리지 않아요.",
  "limits": {"max_tool_calls": 20, "max_seconds": 300, "max_llm_calls": 15}
}
```

`round_id`는 시나리오와 무관한 난수다. Diagnoser 담당은 `netrunner/diagnoser.py`의 `diagnose(brief, tools) -> Diagnosis`를 구현하고, 클래스에 `model`과 `method` 속성을 둔다. 진단이 실패한 라운드도 그 모델의 실패로 집계하기 위해서다. 시간 한도는 심판이 잰 시간으로 검사한다.

## 3-1. Diagnoser ↔ 심판: 검사 도구 창구 `Toolbox`

설계: `docs/superpowers/specs/2026-11-13-probe-tools-design.md`

Diagnoser는 검사를 **심판이 라운드마다 만들어 넘기는 도구 창구로만** 실행한다(`diagnose(brief, tools)`). 창구가 실험망이 실제로 낸 출력을 기록하고, 채점은 이 기록으로만 한다. Diagnoser가 스스로 적은 `trace`는 근거로 인정하지 않는다.

| 함수 | 하는 일 |
|---|---|
| `tools.specs()` | 이 라운드에 쓸 수 있는 도구의 LLM용 설명(OpenAI `tools` 형식) |
| `tools.call(name, args)` | 검사 한 번. `ProbeView(probe_id, output, packets, file, error)`를 돌려준다 |
| `tools.remaining()` | 남은 검사 횟수 |

- Diagnoser가 쓰는 함수는 위 세 개뿐이다. 기록은 심판만 사본(`probes()`)으로 읽는다.
- 실행된 검사에만 `probe_id`(`p1`, `p2`…)가 붙는다. 근거는 이 번호로 인용한다(`probe:p1`, `pcap:p3#2`).
- 다음 호출은 실험망을 부르지 않고 `probe_id=None`과 `error`로 거부한다. 거부는 검사 횟수에 세지 않는다.
  - 모르는 도구
  - 이 방식에서 허용되지 않은 도구
  - `enabled_tools`에 없는 도구
  - 인자 오류
  - 검사·시간 한도 초과
  - 실험망 미지원·오류
  - 라운드가 끝난 뒤 호출
- 출력이 20,000자를 넘으면 앞부분만 남기고 잘렸다고 표시한다.

도구 목록은 `netrunner/tools.py`가 단일 출처다.

| 도구 | 인자(굵게는 필수) | 허용 값 |
|---|---|---|
| `dig` | **domain**, server | 실험망 도메인 / `dns`, `alt_dns` 또는 그 IP |
| `curl` | **url**, resolve_ip | http·https이고 호스트가 실험망 도메인 / 실험망 대역 IP |
| `capture` | **url** | curl과 같음. 캡처하는 동안 요청 한 번, 패킷마다 `#<n>` |
| `openssl` | **host**, port, servername | 실험망 도메인 / 1–65535 / 실험망 도메인 |
| `ping` | **host**, count | 실험망 도메인 또는 대역 IP / 1–10 |
| `tracepath` | **host** | ping과 같음 |

방식별 허용 도구(설계안 9.2절):

| 방식 | 허용 도구 |
|---|---|
| A, C, C-2 | 전체 |
| B | 없음 |
| C-1 | `capture` 제외 |

모르는 방식은 무효 라운드다. 도구마다 출력에 반드시 들어갈 내용(`must_include`)이 있고, 실험망 테스트에서 `tools.missing_parts(tool, output)`로 확인한다. curl은 종료 코드, HTTP 상태, 단계별 시간 다섯 개(`time_namelookup`, `time_connect`, `time_appconnect`, `time_starttransfer`, `time_total`)가 출력에 있어야 한다. `-v` 진행 기록은 curl 버전마다 형식이 달라 출력으로 확인하지 않고, 실험망의 `run_probe` 테스트에서 argv에 `-v`가 들어가는지 확인한다.

## 4. Diagnoser → 심판: `Diagnosis`

```json
{
  "round_id": "3f9a1c0e",
  "model": "glm-4.5",
  "method": "C",
  "findings": [
    {"stage": "TCP", "cause": "연결 거부",
     "evidence": [{"ref": "probe:p1", "claim": "curl이 Connection refused로 즉시 실패"},
                  {"ref": "pcap:p3#2", "claim": "SYN 직후 서버가 RST,ACK"}]}
  ],
  "trace": [
    {"probe_id": "p1", "tool": "curl", "args": {"url": "http://shop.lab/"},
     "output": "curl: (7) Failed to connect ... Connection refused", "elapsed_ms": 4},
    {"probe_id": "p3", "tool": "tcpdump", "args": {"filter": "tcp port 80"},
     "output": "<캡처 요약기 출력>", "elapsed_ms": 1200, "packets": 2, "file": "captures/x.pcap"}
  ],
  "llm_calls": 3, "tokens": 2600, "elapsed_s": 14.7
}
```

- `stage`는 `DNS | 경로 | TCP | TLS | HTTP | 차단` 중 하나이고, `cause`는 장애 목록의 원인 이름을 쓴다. 영어 별칭(`path`, `refused`, `packet loss` 등)도 받는다.
- `method`는 설계안 9.2절의 비교 대상이다: `A` 규칙 기반, `B` 도구 없는 LLM, `C` 전체, `C-1` 캡처 제외, `C-2` 요약기 제외.
- `model`은 반드시 채운다. Solar와 GLM의 숫자가 섞이지 않게 하기 위해서다.
- **근거 참조 형식**: `probe:<probe_id>`는 명령 출력 전체를, `pcap:<probe_id>#<n>` 또는 `#<n>-<m>`은 캡처 안의 패킷 번호를 가리킨다. 번호는 도구 창구가 붙인 것이다. `trace`는 옛 형식과 호환하려고 남긴 칸이며 채점에 쓰지 않는다. 근거의 존재와 지지는 창구 기록(라운드 기록의 `probes`)으로 판정한다.

## 5. 심판 → Breaker: `BreakerFeedback`

```json
{
  "scenario_id": "EX-01", "faults": [...], "complaint": "...", "valid": true, "invalid_reason": "",
  "outcomes": [{"id": "T2", "label": "TCP / 연결 거부", "verdict": "cause_wrong"}],
  "false_positives": ["DNS / 지연"]
}
```

- 채점이 끝난 대결 세트 라운드만 보낸다. 고정 세트 라운드는 최종 평가용이므로 Breaker 입력에서 뺀다.
- 검증에서 거부된 제안도 이유와 함께 보낸다. 다음 제안에서 같은 실수를 하지 않게 하려는 것이다.
- `verdict`는 다음 다섯 가지 중 하나다.

| verdict | 뜻 |
|---|---|
| `correct` | 정답 |
| `cause_wrong` | 단계는 맞고 원인이 틀림 |
| `stage_wrong` | 그 단계 진단이 없고 다른 단계를 지목함 |
| `missed` | 아무것도 지목하지 않음 |
| `masked` | 관측 불가로 채점에서 제외 |

## 채점 규칙

- 심은 장애마다 진단 하나를 짝짓는다. 단계와 원인이 모두 맞는 짝을 먼저 정하고, 남은 것끼리 단계만 맞는 짝을 정한다. 진단 하나가 두 장애를 동시에 맞힐 수는 없다.
- 짝이 없는 진단은 **오탐**이다. 단, 관측 불가 장애와 짝지어진 진단은 오탐이 아니다.
- **근거 판정**은 인용 하나마다 따로 한다.
  - **존재**: 해당 검사가 도구 창구 기록(`probes`)에 있는지, 패킷 번호가 캡처의 패킷 수 범위 안에 있는지 본다.
  - **지지**: 인용된 출력에 *진단이 주장한 원인*의 신호가 있는지 본다. 예를 들어 연결 거부라면 `Connection refused`나 `RST`다. 신호 목록은 `faults.py`의 `signals`에 있다. 자동으로 판정하지 못한 인용은 **수동 확인 필요(?)**로 표시하고 유효율 분자에 넣지 않는다.
- 지표는 단계 정확도, 원인 정확도, 오탐, 근거 유효율, 검사 수, 진단 시간(심판 측정, 중앙값과 p95), LLM 호출 수와 토큰이다. 비율은 `23/30 (77%)`처럼 개수와 함께 적는다.

## 장애 목록

설계안 6장과 같다. 매개변수 범위는 첫 제안이므로 회의에서 조정한다.

| ID | 정답 라벨 | 매개변수 (범위) | 앞 단계에서 요청을 끝냄 |
|---|---|---|---|
| D1 | DNS / 이름 없음 | domain | O |
| D2 | DNS / 잘못된 주소 | domain, ip(실험망 대역, 정상 IP 아님) | O |
| D3 | DNS / 서버 무응답 | – | O |
| D4 | DNS / 지연 | domain(선택), delay_ms 100–3000 | |
| T1 | TCP / 무응답 차단 | port(=대상 포트) | O |
| T2 | TCP / 연결 거부 | port(=대상 포트) | O |
| N1 | 경로 / 지연 | delay_ms 20–2000, jitter_ms 0–500(선택) | |
| N2 | 경로 / 손실·재전송 | loss_pct 1–30 | |
| N3 | 경로 / MTU 블랙홀 | min_size 576–1500 | |
| N4 | 경로 / 큐잉 지연 | rate_kbit 256–10000 | |
| S1 | TLS / 인증서 만료 | – | |
| S2 | TLS / 이름 불일치 | cert_domain(대상과 다른 실험망 도메인) | |
| H1 | HTTP / 서버 오류 | – | |
| H2 | HTTP / 리다이렉트 순환 | – | |
| B1 | 차단 / SNI 기반 | domain | O |
| B2 | 차단 / DNS 변조 | domain, block_ip | O |

구현 순서: 11주차에는 D2, T2(첫 통합)부터 시작해 D1, D3, N1, S1, H1을 추가한다. 12주차에 TLS, HTTP, 경로 장애로 넓히고, 13주차에 전체 목록을 구현한다.

## 회의에서 정할 것

| # | 결정할 것 | 담당 | 제안 |
|---|---|---|---|
| 1 | 실험망 도메인과 IP | 양시찬 | `shop.lab`, `news.lab` / 웹 서버 `10.20.0.10` / 예전 IP `10.20.0.99` / 보조 DNS `10.20.0.54` |
| 2 | 첫 통합의 대상 URL이 http인지 https인지 | 양시찬 | 첫 통합은 `http://shop.lab/`(포트 80)로 하고, TLS 장애는 12주차에 https로 추가 |
| 3 | 매개변수 범위 | 김승범 | 위 표 |
| 4 | 검사 한도 | 이현섭 | 검사 20회, LLM 호출 15회, 300초 |
| 5 | 캡처 요약기 출력에 패킷 번호를 넣는 형식 | 이현섭 | `#<n>` 줄 머리, 캡처 검사의 `packets`에 총 패킷 수 |
| 6 | 원인 단서 금지어 목록 | 김승범 | `CLUE_WORDS` |
| 7 | 라운드 기록에 남길 모델 이름 표기 | 이현섭 | API의 model 문자열 그대로 |

## 서로 기다리지 않고 시작하기

- **Breaker(김승범)**: `fixtures/scoring_examples/`의 기록으로 만든 `BreakerFeedback` 목록을 입력으로 쓰고, 출력은 `python -m netrunner validate`로 확인한다. 무효 제안 비율은 심판 기록의 `invalid_reason`으로 센다.
- **실험망(양시찬)**: `MockLab`과 같은 네 함수를 구현해 `Referee(lab=...)`에 넣는다. `fixtures/first_integration`의 시나리오 두 개가 첫 목표다.
- **Diagnoser(이현섭)**: `fixtures/first_integration/*.diagnosis.json`의 형식으로 출력하고, `ReplayDiagnoser` 자리에 실제 구현을 넣는다.
- **심판(김세중)**: 모의 실험망과 저장된 진단으로 `python -m netrunner demo`를 돌려 결과 화면을 만든다. 실제 모듈이 준비되면 같은 자리에 교체한다.
