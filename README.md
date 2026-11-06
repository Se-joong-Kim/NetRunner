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

```
python -m netrunner validate fixtures/first_integration/*.scenario.json
python -m netrunner demo        # 첫 통합 시연 두 라운드를 모의 실험망으로 실행 → runs/에 기록과 결과 화면
python -m pytest tests
```
