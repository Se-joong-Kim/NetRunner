# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트

컴퓨터 네트워크 팀 프로젝트. Breaker(장애를 심는 LLM)와 Diagnoser(증상만 듣고 원인을 찾는 LLM)를 Docker 실험망에서 겨루게 하고, Python 심판이 정답과 근거를 대조해 채점한다. 4인 팀이 모듈을 하나씩 맡고(README 표 참고), 이 저장소의 `netrunner/`는 심판·통합 뼈대와 모듈 사이 계약이다. 설계안과 개발 계획은 `docs/planning/`(docx)에 있다.

## 명령

의존성은 uv로 관리한다. 런타임 의존성은 없고(표준 라이브러리만), 개발 의존성은 pytest뿐이다. Python 3.10 이상.

```
uv sync
uv run pytest                                   # 전체 테스트
uv run pytest tests/test_judge.py -k <이름>      # 단일 테스트
uv run netrunner validate <시나리오.json>...     # Breaker 출력이 계약을 지키는지 검사
uv run netrunner demo                           # 고정 입력 2라운드를 MockLab으로 실행 → runs/ (git 제외)
uv run netrunner report <rounds.jsonl> --html out.html
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
- `faults.py`: 장애 16종의 단일 출처. 정답 라벨, 매개변수 범위, 관측 차단 여부, 근거 지지 판정용 신호를 가진다. 검증, 채점, 보고서가 모두 여기서 읽는다.
- `validate.py`(제안 검사), `scoring.py`(채점), `report.py`(지표와 HTML): `referee.py`가 조립한다.
- `config/lab.json`: 실험망 주소. 실험망 담당이 관리한다.
- `fixtures/first_integration`(시연 두 라운드)과 `fixtures/scoring_examples`(채점 사례)는 `<이름>.scenario.json`과 `<이름>.diagnosis.json` 짝으로 이루어지며, `demo`와 테스트가 이 이름 규칙에 의존한다.

### 지켜야 할 경계

- Diagnoser에게는 `RoundBrief`(라운드 ID, URL, 증상, 한도)만 넘긴다. 시나리오 ID, 장애, 주입 로그, 정답, `note`는 넘기지 않는다. `round_id`는 시나리오와 무관한 난수다.
- 라운드가 무효여도 `reset`과 정상화 확인은 반드시 실행한다. 정상화가 확인되지 않으면 다음 라운드를 돌리지 않는다.
- 실험망 함수가 예외를 내도 라운드 기록이 깨지지 않게 실패로 기록한다.
- Breaker가 낸 시나리오는 `origin`을 항상 `breaker`로 바꿔 기록하고, 고정 세트(`fixed`) 라운드는 Breaker의 입력 기록에서 뺀다.
- 거부된 제안은 실험망을 건드리지 않는다.

## 작업 기준

- 작업을 시작하기 전에 사용자 홈의 `.claude/projects/<프로젝트 식별자>/memory/` 디렉터리(Claude Code가 프로젝트마다 두는 메모리)에서 `type: user` 항목을 찾아 지금 대화하는 사람이 누구인지 확인한다. 식별자는 프로젝트 경로에서 Claude Code가 정한 이름이므로 직접 짓지 않는다.
- 확인한 사람의 역할, 담당 모듈, 선호를 기준으로 구현한다.
- 메모리에 그런 항목이 없으면 이름을 추측하지 말고 사용자에게 누구인지 묻는다.
- 구현하기 전에 열린 PR(`gh pr list`)과 원격 브랜치(`git branch -r`)를 확인해, 같은 파일을 고치는 작업이 있으면 먼저 조율한다.

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

저장소: `Se-joong-Kim/NetRunner`, 기본 브랜치 `main`. 팀원마다 자기 담당 브랜치 하나에서 작업하고, PR로 `main`에 합친다. `main`에는 직접 푸시하지 않는다.

- **브랜치**: 각자 담당 브랜치에서만 작업한다. 작업마다 새 브랜치(번호나 주제를 붙인 이름 포함)를 만들지 않는다. 담당 브랜치가 뒤처졌으면 `main`을 가져와 맞추고, 강제 푸시로 기록을 바꾸지 않는다. 한 PR에는 한 주제만 담고, 담당 모듈과 무관한 변경은 섞지 않는다. 오래 두지 말고 짧게 끝내 합친다.
- **PR**: 작게 올려 빨리 리뷰받는다. 작업 중 피드백이 필요하면 draft로 올린다. 본문은 한국어로 쓰고, 먼저 읽을 것, 담당별 할 일, 확인 방법, 알아 둘 점 순으로 적는다.
- **계약 변경은 합의 후**: `docs/interfaces.md`, `contracts.py`, `faults.py`를 바꾸는 PR은 영향을 받는 담당자의 승인을 받은 뒤 합친다. 담당자는 README 표를 따른다.
- **합치기 전 확인**: `uv run pytest`와 `uv run ruff check`가 통과해야 한다.
- **리뷰 요청과 승인은 사람이 한다**: PR 병합, 브랜치 삭제, 강제 푸시는 사용자가 시킬 때만 한다.
