# NetRunner

[![CI](https://github.com/Se-joong-Kim/NetRunner/actions/workflows/ci.yml/badge.svg)](https://github.com/Se-joong-Kim/NetRunner/actions/workflows/ci.yml)

This repo is for a Computer Network project

Breaker(장애를 심는 LLM 에이전트)와 Diagnoser(증상만 듣고 원인을 찾는 LLM 에이전트)를 Docker 실험망에서 겨루게 하고, Python 심판이 정답과 근거를 대조해 채점한다.

| 담당 | 모듈 | 담당 파일 |
|---|---|---|
| 김승범 | Breaker | `netrunner/breaker.py`, `breaker_prompt.py`, `glm_client.py`, `config/opencode_sandbox.json`, `fixtures/d2_ip_classes/`, `tests/test_breaker.py`, `test_glm_client.py`, `test_propose.py` |
| 양시찬 | 실험망 | `netrunner/lab.py`, `docker_lab.py`, `config/lab.json`, `lab/`, `compose.yaml`, `tests/test_docker_lab.py`, `docs/plan-Yang.md`, `docs/work-summary-Yang.md` |
| 이현섭 | Diagnoser | `netrunner/diagnoser.py`, `http_lab.py`, `http_demo.py`, `labs/http/`, `config/http_lab.json`, `fixtures/http/`, `docs/http-lab.md`, `tests/test_http_diagnoser.py`, `test_http_lab.py` |
| 김세중 | 심판과 통합 | `netrunner/referee.py`, `validate.py`, `scoring.py`, `report.py`, `toolbox.py`, `records.py`, `rule_diagnoser.py`, `__main__.py`, `__init__.py`, `fixtures/first_integration/`, `fixtures/scoring_examples/`, `docs/superpowers/`, `tests/test_judge.py`, `test_run.py`, `test_toolbox.py`, `test_tools.py`, `test_records.py`, `test_match.py`, `test_methods.py`, `test_report.py`, `test_rule_diagnoser.py` |
| 계약 | 모듈 사이 데이터 형식 | `docs/interfaces.md`, `netrunner/contracts.py`, `faults.py`, `tools.py` |
| 공용 | 팀 공용 파일 | `CLAUDE.md`, `README.md`, `pyproject.toml`, `uv.lock`, `.github/`, `.gitignore`, `.gitattributes`, `docs/planning/` |

`main`에는 PR로만 합친다. CI(초록 체크)가 통과하면 PR을 연 사람이 합친다. 승인은 받지 않는다. 자세한 규칙은 [CLAUDE.md의 GitHub 협업](CLAUDE.md#github-협업)에 있다.

모듈 사이 데이터 형식과 장애 목록: [docs/interfaces.md](docs/interfaces.md)

기획 문서(설계안, 역할 분담과 개발 계획, 수업자료 링크): [docs/planning/](docs/planning/)

```
uv sync
uv run netrunner validate fixtures/first_integration/*.scenario.json
uv run netrunner demo        # 첫 통합 시연 두 라운드를 모의 실험망으로 실행 → ~/netrunner-private/runs에 기록과 결과 화면
uv run pytest
```

실제 실험망은 장애 16종을 단일 또는 지원되는 2~3개 조합으로 주입하고 복구한다(`config/lab.json`의 `enabled_faults`, #63, #106). Docker Desktop 또는 Docker Engine을 시작한 뒤 저장소 루트에서 실행한다.

```sh
docker build -t netrunner-lab ./lab
docker compose up -d --no-build
uv run netrunner run --impl docker --repeat 3   # 심판으로 시연 두 라운드(D2·T2)를 3번씩 반복
uv run netrunner lab-check --dir fixtures/fixed_set   # 심판 없이 고정 세트의 실험망 단계만 확인
uv run netrunner run --impl docker --dir fixtures/fixed_set --diagnoser rules   # 방식 A(규칙 기준선)로 진단
uv run netrunner run --impl docker --dir fixtures/fixed_set --diagnoser rules --diagnoser glm-b --diagnoser glm-c --diagnoser glm-c1 --diagnoser glm-c2 --repeat 3 --opencode <opencode 경로>   # 방식 A·B·C·C-1·C-2 비교
uv run netrunner match --rounds 3 --opencode <opencode 경로>   # Breaker 제안으로 대결(진단기 기본 none)
NETRUNNER_INTEGRATION=1 uv run pytest
docker compose down
```

`match`와 `breaker-propose`는 GLM을 opencode 경유로 부른다. 설치할 패키지는 `@opencode/cli` 2.3.0이다. 전역에 설치된 `opencode`(패키지 `opencode-ai`)는 다른 제품이라 쓰지 않는다.

```sh
npm install --prefix .venv-opencode --save-exact @opencode/cli@2.3.0
```

이 버전은 `--standalone`과 `--archived` 옵션을 제공한다. 실행 파일 경로는 `--opencode`로 넘기거나 `NETRUNNER_OPENCODE`에 적는다. 둘 다 없으면 PATH의 `opencode`를 쓰므로 전역 설치본이 잡힐 수 있다. Linux와 macOS는 `.venv-opencode/node_modules/.bin/opencode`, Windows는 `.venv-opencode\node_modules\.bin\opencode.cmd`다. 잠금 파일(`package-lock.json`)은 저장소에 없으므로 버전은 이 문서의 값을 따른다. 버전을 올릴 때는 CLAUDE.md의 opencode 절을 따른다.

설치한 뒤 한 번 `opencode auth login opencode-go`로 키를 opencode 자격 증명 저장소에 등록한다. Windows에서는 `.venv-opencode\node_modules\.bin\opencode.cmd auth login opencode-go`다. 키는 대화형으로 입력하고 명령 인자로 넘기지 않는다. 환경변수 `OPENCODE_API_KEY`는 허용 목록(`netrunner/child_env.py`)이 opencode에 넘기지 않으므로, 등록하지 않으면 `Model unavailable: opencode-go/...` 오류가 난다. 등록 여부는 `opencode auth list`로 본다(#40).

`run`은 고정 세트(`--dir`, 기본 `fixtures/first_integration`)를 심판으로 `--repeat`번 돌리고, 결과 표와 화면을 `--out`(기본 `~/netrunner-private/runs`, 저장소 밖)에 남긴다. 기본(`--diagnoser replay`)은 저장된 진단을 재생하므로 AI 진단 성능이 아니라 실험망의 재현·정상화와 심판 경로를 확인하는 용도다. `--diagnoser`로 진단기를 여럿 주면(예: 방식 A·B·C·C-1·C-2) 고정 세트를 진단기마다 돌려 방식별 지표를 비교한다. 방식 이름은 A `rules`, B `glm-b`, C `glm-c`, C-1 `glm-c1`, C-2 `glm-c2`다. 방식 A(`rules`)는 LLM 없이 정해진 순서(DNS 기본·보조 → HTTP → ping)로 검사하고 if-else 규칙으로 장애 목록의 원인을 고른다(`netrunner/rule_diagnoser.py`의 모듈 설명에 규칙 전체가 있다). 결과 표의 `증상 재현`과 `초기화 확인` 열이 완료 기준(반복 시도 중 재현·정상화 성공 건수)이다. 결과 화면에는 정답 장애별 판정 횟수와 오탐을 모은 오진 유형 표가 있고, `--repeat`가 2 이상이면 방식별 회차 정확도 그래프도 나온다. 정상화가 확인되지 않으면 그 자리에서 멈춘다. `--impl mock`은 모의 실험망으로 같은 과정을 돌리며, `demo`는 `run --impl mock --repeat 1`과 같다.

`match`는 Breaker가 GLM으로 낸 시나리오를 심판이 라운드마다 검증, 주입, 재현 확인, 진단, 채점, 정상화 확인까지 돌린다. 끝나면 진단 지표 표와 함께 Breaker 제안의 검증 거부, 증상 재현, 정상화 확인 수를 보여 준다. 진단기는 `--diagnoser`로 고르고, 실제 Diagnoser가 들어오기 전에는 원인을 답하지 않는 자리표시(`none`)를 쓴다.

`lab-check`는 각 장애의 정상 접속, 주입, 증상 확인, 복구, 정상화를 차례로 실행한다. 결과는 `--out` 폴더의 JSONL, 클라이언트 패킷 캡처는 `~/netrunner-private/captures/`의 pcap에 저장한다. 기존 캡처는 자동 삭제하지 않는다. `lab-check`는 AI 진단과 채점을 수행하지 않는다.

조합 시나리오는 `run`·`match`와 실험망 검증 전용 `lab-check --dir <폴더> --repeat 3`에서 같은 주입 순서와 장애별 증상 확인을 사용한다. 한 라운드 안에서 모든 장애를 주입한 뒤 관측 가능한 증상을 각각 확인한다. 선행 장애에 가려진 증상은 `validation.observable`에 따라 `masked`로 채점에서 제외하며, 나머지 증상과 복구가 확인되면 라운드는 유효하다.

N1+N2는 같은 장치의 큐 설정을 쓰므로 주입 전에 거부한다. D4와 D1~D3의 조합, N3·N4와 비경로 장애의 조합은 기존 증상 확인 조건과 충돌해 지원하지 않는다. B1·B2는 단일 장애만 지원한다. 이 제한은 `validate`가 `run`, `match`, `lab-check`에서 공통으로 검사한다.

실험망 하나에는 검사 명령을 한 번에 하나만 실행한다. 시작 전에 장애가 남아 있으면 초기화 후 정상 접속을 다시 확인한다. 캡처의 `output`에는 원본 pcap의 패킷 번호(`#1` 등), `packets`에는 총 개수, `file`에는 파일 경로를 기록한다. 명령이 시간 초과로 중단되어도 캡처 프로세스를 정리하며, 정리 실패는 정상 복구로 처리하지 않는다.

두 네트워크의 고정 대역은 `10.21.0.0/24`와 `10.20.0.0/24`다. 로컬 환경의 대역과 충돌하면 주소와 라우팅을 함께 변경해야 한다. 각 컨테이너는 시작 시 기본 외부 경로를 제거한다. Diagnoser의 검사는 심판의 도구 창구(`toolbox.py`)가 인자와 한도를 확인한 뒤 `DockerLab.run_probe`가 client 컨테이너에서 실행한다. 쓸 수 있는 도구는 `config/lab.json`의 `enabled_tools`(dig, curl, capture, openssl, ping, tracepath)다.
