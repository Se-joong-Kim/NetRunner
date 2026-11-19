# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트

컴퓨터 네트워크 팀 프로젝트. Breaker(장애를 심는 LLM)와 Diagnoser(증상만 듣고 원인을 찾는 LLM)를 Docker 실험망에서 겨루게 하고, Python 심판이 정답과 근거를 대조해 채점한다. 4인 팀이 모듈을 하나씩 맡고(README 표 참고), 이 저장소의 `netrunner/`는 심판·통합 뼈대와 모듈 사이 계약이다. 설계안과 개발 계획은 `docs/planning/`(docx)에 있다.

LLM은 팀 결정대로 GLM을 opencode 경유로 쓴다. Anthropic API 어댑터는 만들지 않는다.

## 명령

의존성은 uv로 관리한다. 런타임 의존성은 없고(표준 라이브러리만), 개발 의존성은 pytest와 ruff다. Python 3.10 이상.

```
uv sync
uv run pytest                                   # 전체 테스트 (실험망 테스트는 건너뜀)
uv run pytest tests/test_judge.py -k <이름>      # 단일 테스트
NETRUNNER_INTEGRATION=1 uv run pytest           # Docker 실험망 테스트까지 (실험망이 떠 있어야 함)
uv run netrunner validate <시나리오.json>...     # Breaker 출력이 계약을 지키는지 검사
uv run netrunner demo                           # 고정 입력 2라운드를 MockLab으로 실행. 기록은 기본값 ~/netrunner-private/runs(저장소 밖)
uv run netrunner run --impl docker --repeat 3   # 같은 고정 세트를 실제 Docker 실험망에서 심판으로 반복
uv run netrunner lab-check --repeat 3           # 심판 없이 실험망 단계만 확인 (AI 진단·채점 없음)
uv run netrunner report ~/netrunner-private/runs/<rounds.jsonl> --html ~/netrunner-private/report.html
uv run netrunner breaker-propose --n 3 --opencode <opencode 경로>   # Breaker 제안을 실험망 없이 n번 받아 검사 통과율을 셈
```

Docker 실험망은 저장소 루트에서 띄우고 내린다. 이미지를 먼저 빌드해야 한다.

```
docker build -t netrunner-lab ./lab
docker compose up -d --no-build
docker compose down
```

린터와 포매터는 ruff를 쓴다.

```
uv run ruff check
uv run ruff format
```

## 구조

한 라운드: 검증 → 정상 확인 → 주입·재현 확인 → 진단 → 채점·정답 공개 → 복구·정상화 확인 → JSONL 기록. 이 순서는 `referee.py`가 진행하고, 자세한 규칙과 JSON 형식은 `docs/interfaces.md`가 기준이다.

- `contracts.py`: 모듈 사이에 오가는 모든 데이터 클래스. 각 `from_dict`가 필수 키와 타입을 검사하고 `ContractError`를 낸다.
- `breaker.py`, `lab.py`, `diagnoser.py`: 담당자가 구현할 경계. 각각 `typing.Protocol`(`Breaker`, `Lab`, `Diagnoser`)과 그 자리를 대신하는 스탠드인(`FixedBreaker`, `MockLab`, `ReplayDiagnoser`)이 같이 있다. 실제 구현이 생기면 `Referee(lab=..., diagnoser=...)`에 같은 형태로 꽂는다.
- `breaker.py`의 `LLMBreaker`: 모델 호출 함수 `complete(prompt) -> str`을 인자로 받아 시나리오를 제안하고 검사한다. 호출 실패는 `CompletionError`로 올라오며 재시도 횟수에 센다. 모델 연결(진입점에서 `complete`를 만드는 일)은 아직 없다.
- `breaker_prompt.py`: `LLMBreaker`가 쓸 프롬프트 문자열을 만든다. 외부 호출은 하지 않는다.
- `docker_lab.py`: `Lab`의 실제 구현(Docker Compose 실험망, D2·T2). `__main__.py`의 `LAB_IMPLS`에 등록해 `run --impl`로 고른다.
- `tools.py`: Diagnoser가 쓰는 검사 도구 목록의 단일 출처(이름, 인자, 허용 값, LLM용 설명, 방식별 묶음).
- `toolbox.py`: 심판이 라운드마다 만들어 Diagnoser에 넘기는 도구 창구. 인자·한도를 검사하고 실제 출력을 기록하며, 채점은 이 기록으로만 한다. 실험망 쪽은 `lab.py`의 `ProbeRunner.run_probe` 하나를 구현한다.
- `faults.py`: 장애 16종의 단일 출처. 정답 라벨, 매개변수 범위, 관측 차단 여부, 근거 지지 판정용 신호를 가진다. 검증, 채점, 보고서가 모두 여기서 읽는다.
- `validate.py`(제안 검사), `scoring.py`(채점), `report.py`(지표와 HTML): `referee.py`가 조립한다.
- `records.py`: 정답이 든 기록의 기본 위치(`~/netrunner-private/runs`)와 권한(폴더 700, 파일 600). 기록을 쓰는 코드는 모두 이 모듈의 `ensure_private_dir`, `open_private`, `write_private_text`를 거친다.
- `config/lab.json`: 실험망 주소. 실험망 담당이 관리한다.
- `fixtures/first_integration`(시연 두 라운드)과 `fixtures/scoring_examples`(채점 사례)는 `<이름>.scenario.json`과 `<이름>.diagnosis.json` 짝으로 이루어지며, `demo`와 테스트가 이 이름 규칙에 의존한다.

### 지켜야 할 경계

- Diagnoser에게는 `RoundBrief`(라운드 ID, URL, 증상, 한도)와 도구 창구만 넘긴다. 시나리오 ID, 장애, 주입 로그, 정답, `note`는 넘기지 않는다. `round_id`는 시나리오와 무관한 난수다.
- 라운드가 무효여도 `reset`과 정상화 확인은 반드시 실행한다. 정상화가 확인되지 않으면 다음 라운드를 돌리지 않는다.
- 실험망 함수가 예외를 내도 라운드 기록이 깨지지 않게 실패로 기록한다.
- Breaker가 낸 시나리오는 `origin`을 항상 `breaker`로 바꿔 기록하고, 고정 세트(`fixed`) 라운드는 Breaker의 입력 기록에서 뺀다.
- 거부된 제안은 실험망을 건드리지 않는다.
- `LLMBreaker`의 중복 판정은 채점이 끝난 유효 라운드(`valid=True`)의 장애 조합만 본다. 검증에서 거부된 라운드는 넣지 않아, 증상만 고친 재제안을 막지 않는다.
- `Referee.run_match`는 Breaker가 `BreakerError`를 내면 `proposal_failed`를 기록하고 매치를 종료한다. 완료한 라운드와 실패 기록을 함께 반환하며, 제안 실패는 진단 지표에서 제외한다.

### 역할별 권한

Breaker, Diagnoser, 실험망, 심판은 각자 일에 필요한 입력과 파일만 본다. 기본은 거부이고, 필요한 것만 연다. 다른 역할이 정답을 읽을 수 있으면 대결이 깨진다. 위의 전달 경계는 코드가 넘기는 것만 막으므로, 에이전트가 파일을 직접 읽는 경로도 같이 제한한다.

- 정답과 장애 정보(시나리오의 `faults`와 `note`, 주입 로그, 정답 라벨, 채점 결과)는 Diagnoser 쪽 에이전트가 읽지 못하게 권한으로 막는다. 막는 경로는 `fixtures/**/*.scenario.json`과 기록 폴더(`runs/`, `~/netrunner-private/`)다(아래 opencode 절). Diagnoser는 도구를 아예 열지 않는다.
- 라운드 기록(`rounds-*.jsonl`)과 Breaker 호출 기록(`breaker-*.jsonl`, 프롬프트 해시와 원문 응답)은 Breaker가 낸 장애 조합을 담는다. 심판과 Breaker만 본다.
- Breaker는 지난 라운드를 `BreakerFeedback`으로만 받는다. 과거 진단 파일이나 다른 매치의 기록을 직접 열지 않는다.
- 에이전트 샌드박스 설정은 허용 목록으로 쓴다. 저장소 전체를 여는 `**` 허용은 정답과 기록 경로를 거부하는 규칙과 함께 쓰고, 그 거부가 실제로 먹는지 테스트한다.
- 새 파일이나 새 경로를 만들 때 정답이나 기록이 들어가는지 먼저 본다. 권한을 바꾸는 PR은 본문에 역할별로 읽고 쓰는 경로를 적는다.

### opencode 권한과 샌드박스

opencode는 GLM 호출(Breaker)과 개발 에이전트에 쓴다. 원칙은 허용 목록(whitelist)이다. 환경변수, 도구 권한, 샌드박스 모두 목록에 없는 것은 거부하고, 필요한 것만 목록에 넣는다. 역할별 경로 규칙은 위 "역할별 권한"을 따른다.

- Breaker 에이전트(`netrunner`)는 도구를 쓰지 않는다. `config/opencode_sandbox.json`의 모든 권한을 `deny`로 둔다. 허용을 더하려면 이유를 PR 본문에 적고 `tests/test_sandbox_config.py`를 함께 고친다.
- Diagnoser 에이전트(`diagnoser`)는 `config/opencode_diagnoser.json`의 모든 권한을 `deny`로 둔다(셸과 검색 포함). Diagnoser는 검사를 심판의 도구 창구로만 하므로 opencode 도구가 필요 없다. 도구를 열면 정답 경로 거부를 함께 두고 `tests/test_sandbox_config.py`를 고친다. 2026-11-19에 아래 카나리 확인으로 이 설정이 실제로 막는 것을 확인했다. 설정을 바꾸면 다시 확인한다.
- 실행은 `netrunner/glm_client.py` 한 곳에서만 한다: `opencode run --standalone --agent netrunner -m <모델> --format json --archived`. `--auto`, `--yolo`, `--dangerously-skip-permissions`(모두 묻지 않고 승인)는 쓰지 않는다. 에이전트와 설정 경로를 다른 곳에서 바꾸지 않는다.
- 자식 프로세스(opencode, Docker Compose, HTTP 실험망 도구)에는 허용 목록의 환경변수만 넘긴다. 목록과 비밀 표지는 `netrunner/child_env.py` 한 곳에만 둔다. CLAUDE.md에는 목록을 옮겨 적지 않는다. 목록을 늘리려면 이유를 PR 본문에 적는다.
- 2026-11-19에 Windows 시스템 변수 묶음(`child_env.py`의 `WINDOWS_NAMES`)을 더했다(#40). Node.js가 `SYSTEMROOT` 없이 종료 코드 134로 죽어 GLM 호출이 모두 실패했기 때문이다. 이 묶음은 `os.name == "nt"`일 때만 넘긴다. 사용자 경로 변수(`USERPROFILE` 등)는 Windows에서 실제 GLM 호출로 필요한 것이 확인될 때 같은 방식으로 더한다.
- 이 저장소의 기본 에이전트(build, plan)는 `opencode.json`으로 기본 거부(`*: deny`)다. 목록에 있는 것만 연다. 읽기, 검색, 편집, 셸은 `ask`(묻고 승인), 질문은 `allow`다. 정답 경로(`fixtures/**/*.scenario.json`, `runs/`, `captures/`)는 읽기와 편집을 거부한다. read와 glob은 패턴 문자열로 규칙을 맞추므로 폴더 이름(`runs`, `captures`)을 직접 가리키는 호출도 거부한다. 검색(grep)은 권한 대상이 검색 문자열이라 경로 규칙이 걸리지 않으므로 전면 거부한다. 셸(`shell`)은 경로 규칙을 거치지 않아 승인할 때 명령을 보고 판단한다. 외부 경로, 웹 가져오기와 검색, 브라우저, 스킬은 `deny`다. 전역 설정(`~/.config/opencode/`)에 권한을 풀어 두지 않는다.
- 샌드박스가 실제로 막는지는 기록으로 확인한다. 방법: 저장소 `runs/`에 카나리 파일(확인용 문자열 한 줄)을 두고, `OPENCODE_CONFIG_CONTENT`로 읽기·검색·편집·셸을 허용한 대조군과 실제 설정에 같은 요청을 보낸다. `opencode run --format json` 출력의 도구 호출 결과에 카나리가 나오는지 본다. 대조군에서는 나오고 실제 설정에서는 나오지 않아야 한다. 카나리 파일은 확인 뒤 지운다. 카나리 실행 기록(정답이 아니라 확인용 문자열만 든다)은 `~/netrunner-private/runs/control/`에 둔다. "거부합니다"라는 대답만으로는 증거가 아니다.
- 인증과 기록 파일(`~/.local/share/opencode/auth.json`, `~/.config/opencode/service.json`, `~/.local/share/opencode/opencode.db`와 `-wal`·`-shm`, `~/netrunner-private/`의 파일)은 600으로 둔다. 폴더는 `~/netrunner-private/`와 그 안 `runs/`만 700이다. 기록 폴더와 파일은 코드(`records.py`)가 새로 만들 때 700과 600으로 만든다. 이미 있던 폴더의 권한은 바꾸지 않으므로, 예전에 만든 `~/netrunner-private/`는 사람이 `chmod 700`으로 맞춘다. opencode 인증·설정 파일도 사람이 맞춘다(셸 기본 umask는 0002라 새 파일은 664가 된다). 키와 비밀번호 값은 출력하거나 저장소에 넣지 않는다. 확인이 필요하면 이름과 권한만 본다.
- 토큰이나 키를 검색하지 않는다. 환경변수는 이름 목록만 보고 값은 보지 않는다.
- 정답이 든 기록(라운드 기록, Breaker 호출 기록, 제안 시나리오, `lab-check` 기록, `report`의 HTML)은 저장소 밖 `~/netrunner-private/`(700)에 둔다. `demo`, `run`, `lab-check`, `breaker-propose`의 `--out` 기본값이 `~/netrunner-private/runs`이고, `report`는 `--html ~/netrunner-private/...`로 쓴다. 저장소의 `runs/`에 두지 않는다. 이 폴더는 opencode 에이전트의 `external_directory` 거부로 에이전트가 읽지 못한다. `http_demo`의 기본값은 담당자(이현섭)가 #22에서 바꾼다.
- 시연과 테스트용 `fixtures/`의 시나리오(정답 키 포함)는 git에 추적해도 된다(#23, 2026-11-20 결정). 막아야 하는 것은 Diagnoser와 Breaker 같은 에이전트가 그 파일을 읽는 경로다. 그래서 위 opencode 절의 권한 거부(`fixtures/**/*.scenario.json`)를 유지하고, 샌드박스를 바꿀 때 이 거부가 남아 있는지 `tests/test_sandbox_config.py`로 확인한다.
- 실험망 캡처(`captures/`의 pcap)는 주입 중 DNS와 TCP 흔적을 담아 정답 단서가 될 수 있다. 지금은 저장소의 `captures/`(root 소유, git 제외)에 쌓인다. 옮기는 결정은 실험망 담당이 한다.
- opencode 세션 데이터베이스(`~/.local/share/opencode/opencode.db`)에는 정답 시나리오가 평문으로 남는다. opencode 서비스가 이 파일을 쓰는 중이라 옮기지 않는다. 정리(보존 기간, 세션 삭제, 서비스 중지)는 사용자가 정한다. 정할 때까지 이 파일을 공유하거나 커밋하지 않는다.
- 실험망 컨테이너는 `privileged`, `network_mode: host`, 장치 마운트, Docker 소켓 마운트를 쓰지 않는다. `NET_ADMIN`과 `NET_RAW`는 필요한 컨테이너에만 준다. 쓰지 않을 때는 `docker compose down`으로 내린다.
- 이 계정은 `docker` 그룹이라 Docker 데몬 접근이 root 권한과 같다. 모델 에이전트(opencode)가 도는 경로에서는 Docker를 부르지 않는다. 실험망 코드는 사람이 실행한 명령에서만 Docker를 부른다.
- `--opencode`와 `NETRUNNER_OPENCODE`로 고르는 실행 파일 경로는 신뢰하는 것만 쓴다. PATH에서 찾은 실행 파일도 같은 기준이다.
- `@opencode/cli`는 로컬 설치(`.venv-opencode/`, git 제외)에 있고 그 안의 `package-lock.json`으로 버전을 잠근다. npm 잠금 파일은 저장소에 없으므로(`uv.lock`은 Python 의존성용이다) 팀원은 같은 버전을 직접 맞춘다. 버전을 올릴 때는 `npm audit signatures`로 서명을 확인하고, 설치 스크립트(`postinstall.mjs`)를 검토한다.

## 작업 기준

- 작업을 시작하기 전에 사용자 홈의 `.claude/projects/<프로젝트 식별자>/memory/` 디렉터리(Claude Code가 프로젝트마다 두는 메모리)에서 `type: user` 항목을 찾아 지금 대화하는 사람이 누구인지 확인한다. 식별자는 프로젝트 경로에서 Claude Code가 정한 이름이므로 직접 짓지 않는다.
- 확인한 사람의 역할, 담당 모듈, 선호를 기준으로 구현한다.
- 메모리에 그런 항목이 없으면 이름을 추측하지 말고 사용자에게 누구인지 묻는다.
- 구현하기 전에 `git fetch origin` 후 `main`과 원격 브랜치(`git branch -r`)를 확인해, 같은 파일을 고치는 작업이 있으면 먼저 조율한다.
- 일정은 주차에 맞추지 않는다. 지금 구현할 수 있는 것부터 빠르게 구현해 `main`에 올리고, 완료 기준은 기능마다 정한다. 순서는 `docs/plan-Yang.md`의 표를 따른다.

## 코딩 스타일

### 파이썬 스타일 (PEP 8, PEP 257)

- 이름: 함수·변수·모듈은 `snake_case`, 클래스는 `PascalCase`, 상수는 `UPPER_SNAKE_CASE`.
- 줄 길이: 코드는 99자 이내, 주석과 docstring은 72자로 줄바꿈한다.
- docstring 형식은 PEP 257을 따른다. 공개 모듈, 클래스, 함수에는 무엇을 하는지 한 줄 요약을 둔다.
- 코드는 `ruff check`와 `ruff format`을 통과해야 한다.

### 모듈화

- 모듈 하나는 목적 하나를 가진다(높은 응집도). 모듈끼리의 의존은 최소로 둔다(낮은 결합도).
- `utils.py`나 `common.py` 같은 잡동사니 모듈을 만들지 않는다. 서로 무관한 코드가 모이면 중복과 순환 import가 같이 생긴다.
- 순환 import는 설계 문제의 증상이다. 함수 안 import로 우회하지 말고, 공통 의존을 별도 모듈로 빼거나 의존 방향을 한쪽으로 정리한다.
- 객체의 조립(어떤 구현을 꽂을지)은 가장 바깥(진입점)에서 하고, 안쪽 모듈은 필요한 것을 인자로 받는다. 안쪽 모듈이 서로를 직접 만들면 결합도가 높아진다.
- 같은 규칙이나 값을 두 곳에 적지 않는다. 한 곳에서 정의하고 다른 곳은 그것을 참조한다.
- 복잡한 패키지는 진입점만 얇게 노출한다(파사드).

### SOLID

- **단일 책임**: 클래스와 함수는 바뀌는 이유가 하나여야 한다. 검증, 계산, 출력, 파일 I/O가 한 함수에 섞이면 나눈다.
- **개방·폐쇄**: 새 종류(새 장애, 새 방식)를 더할 때 기존 분기문을 고치지 않고, 데이터나 새 구현을 추가하는 것으로 끝나게 한다.
- **리스코프 치환**: 같은 인터페이스를 구현한 객체는 호출하는 쪽이 구분하지 않고 바꿔 쓸 수 있어야 한다. 구현이 인터페이스보다 강한 전제를 요구하거나 약속한 반환 형태를 바꾸지 않는다.
- **인터페이스 분리**: 호출하는 쪽이 쓰지 않는 메서드를 요구하는 큰 인터페이스를 만들지 않는다. 작은 인터페이스 여럿이 낫다.
- **의존 역전**: 상위 로직은 구체 구현이 아니라 추상에 의존한다. 파이썬에서는 `typing.Protocol`이 덕 타이핑을 유지하면서 이 역할을 한다. 구현 클래스가 Protocol을 상속하거나 import하지 않아도 되고, 정적 타입 검사기가 구조를 확인한다. 추상 기반 클래스(ABC)는 꼭 필요할 때만 쓴다.
- 구현이 하나뿐이고 바뀔 이유도 없는 곳에 추상을 미리 만들지 않는다.

## GitHub 협업

저장소: `Se-joong-Kim/NetRunner`, 기본 브랜치 `main`. 팀원마다 자기 담당 브랜치 하나에서 작업하고, PR로 `main`에 합친다. `main`에는 직접 푸시하지 않는다. 리뷰를 기다리는 시간을 줄이려고, 자기 담당 파일만 바꾼 PR은 CI만 통과하면 바로 합친다. 담당 파일은 README 표를 따른다.

- **작업 순서**: 작업을 시작하기 전에 `git fetch origin`으로 `main`을 확인하고, 담당 브랜치를 `main`에 맞춘다(뒤처졌으면 `main`을 가져와 합친다). 담당 브랜치에서 작업하고 커밋한 뒤 담당 브랜치를 푸시하고, `main`으로 PR을 연다.
- **누가, 언제 합치나**: PR을 연 사람이 CI가 초록이면 합친다. 승인은 받지 않는다. 담당 파일과 계약 파일 여부와 상관없이 누구나 어떤 파일이든 바꿀 수 있다.
- **CI**: PR과 `main` 푸시마다 GitHub Actions(`.github/workflows/ci.yml`)가 Python 3.10과 3.14에서 `ruff check`, `ruff format --check`, `pytest`를 돌린다. 초록 체크가 아니면 합치지 않는다. Docker 실험망 테스트와 실제 GLM 호출 테스트는 CI에서 건너뛰므로, 이 부분을 바꾼 PR은 로컬에서 돌린 결과(`NETRUNNER_INTEGRATION=1 uv run pytest` 등)를 본문에 적는다.
- **작업 전 확인**: 작업을 시작하기 전과 PR을 합치기 전에 다음을 확인한다. (1) `git fetch origin` 후 `main`과 원격 브랜치 상태(`git branch -r`, `git log origin/main -5`). (2) 열린 PR(`gh pr list`)과 PR의 리뷰·댓글(`gh api repos/Se-joong-Kim/NetRunner/pulls/<번호>/reviews`, `.../issues/<번호>/comments`), 체크 결과(`gh pr checks <번호>`, 체크가 있을 때만). (3) 열린 이슈(`gh issue list`)와 이슈 댓글. (4) 다른 담당자가 같은 파일을 고치는지. 확인 결과가 작업에 영향을 주면 먼저 알리고 조율한다. 확인은 현재 `gh` 계정으로 한다. 계정 전환은 사용자가 요청할 때만 한다.
- **브랜치**: 각자 담당 브랜치에서만 작업한다. 작업마다 새 브랜치(번호나 주제를 붙인 이름 포함)를 만들지 않는다. 한 PR에는 한 주제만 담고, 담당 모듈과 무관한 변경은 섞지 않는다. 오래 두지 말고 짧게 끝내 합친다.
- **PR 본문과 커밋 메시지**: 무엇을, 왜 바꿨는지와 확인 방법을 한국어로 짧게 적는다. 작업 중 피드백이 필요하면 draft로 올린다.
- **올리기 전 확인**: PR을 올리기 전에 로컬에서 `uv run pytest`와 `uv run ruff check`를 돌려 본다. CI는 이를 다시 확인할 뿐이다.
- **강제 푸시와 브랜치 삭제 금지**: 기록을 바꾸는 강제 푸시와 브랜치 삭제는 하지 않는다.
- **에이전트의 병합과 삭제**: Claude 같은 에이전트는 PR 병합과 브랜치 삭제를 사용자가 시킬 때만 한다.
- **CLAUDE.md 수정**: 팀원 누구나 고칠 수 있다. 모두가 관리자이므로 고쳐진 내용은 그대로 따른다.
- **후속 작업과 결정 항목은 이슈로 남긴다**: 이 대화나 메모리에만 두지 않는다. 다음 경우에 에이전트가 이슈를 만든다.
  - 다른 담당자의 파일이나 결정이 필요한 문제(예: 실험망 네트워크 설정, 검사 함수의 오류 문구)
  - 사용자가 나중에 이어서 하기로 한 작업(예: 제안 프롬프트 보강, 목표 미달 항목)
  - 외부 조건을 기다리는 작업(예: 공유 자원의 정리 정책)
- 이슈 본문에는 현상, 재현 방법(명령과 기록 위치, 정답 값은 빼고), 근거, 영향, 제안하는 해결, 결정할 담당(README 표 기준), 기다리는 작업을 적는다. 정답 파일과 기록 파일의 내용은 붙이지 않는다.
- 제목은 `담당 모듈: 현상`으로 짧게 쓴다. 만들기 전에 `gh issue list --search`로 같은 내용의 열린 이슈가 있는지 확인하고, 있으면 새로 만들지 않고 댓글을 단다.
