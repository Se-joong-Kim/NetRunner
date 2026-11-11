# NetRunner
This repo is for a Computer Network project

Breaker(장애를 심는 LLM 에이전트)와 Diagnoser(증상만 듣고 원인을 찾는 LLM 에이전트)를 Docker 실험망에서 겨루게 하고, Python 심판이 정답과 근거를 대조해 채점한다.

| 담당 | 모듈 |
|---|---|
| 김승범 | Breaker — `netrunner/breaker.py`의 `Breaker` 구현 |
| 양시찬 | 실험망 — `netrunner/lab.py`의 `Lab` 구현, `config/lab.json` |
| 이현섭 | Diagnoser — `netrunner/diagnoser.py`의 `Diagnoser` 구현 |
| 김세중 | 심판과 통합 — `referee.py`, `validate.py`, `scoring.py`, `report.py` |

모듈 사이 데이터 형식과 장애 목록: [docs/interfaces.md](docs/interfaces.md)

기획 문서(설계안, 역할 분담과 개발 계획, 수업자료 링크): [docs/planning/](docs/planning/)

```
uv sync
uv run netrunner validate fixtures/first_integration/*.scenario.json
uv run netrunner demo        # 첫 통합 시연 두 라운드를 모의 실험망으로 실행 → runs/에 기록과 결과 화면
uv run pytest
```

첫 실제 실험망은 D2(잘못된 DNS 주소)와 T2(TCP 연결 거부)를 지원한다. Docker Desktop 또는 Docker Engine을 시작한 뒤 저장소 루트에서 실행한다.

```sh
docker build -t netrunner-lab ./lab
docker compose up -d --no-build
python -m netrunner lab-check --repeat 3
NETRUNNER_INTEGRATION=1 python -m pytest tests
docker compose down
```

`lab-check`는 각 장애의 정상 접속, 주입, 증상 확인, 복구, 정상화를 차례로 실행한다. 결과는 `runs/`의 JSONL, 클라이언트 패킷 캡처는 `captures/`의 pcap에 저장한다. `demo`는 여전히 저장된 진단을 사용하는 모의 실행이고, `lab-check`는 AI 진단과 채점을 수행하지 않는다.

실험망 하나에는 검사 명령을 한 번에 하나만 실행한다. 시작 전에 장애가 남아 있으면 초기화 후 정상 접속을 다시 확인한다. 캡처의 `output`에는 원본 pcap의 패킷 번호(`#1` 등), `packets`에는 총 개수, `file`에는 파일 경로를 기록한다. 명령이 시간 초과로 중단되어도 캡처 프로세스를 정리하며, 정리 실패는 정상 복구로 처리하지 않는다.

두 네트워크의 고정 대역은 `10.21.0.0/24`와 `10.20.0.0/24`다. 로컬 환경의 대역과 충돌하면 주소와 라우팅을 함께 변경해야 한다. 각 컨테이너는 시작 시 기본 외부 경로를 제거한다. 현재 구현은 단일 장애만 검증하며, 실제 Diagnoser에서 실행할 검사 도구의 접근 제한은 통합 시 별도로 구현해야 한다.
