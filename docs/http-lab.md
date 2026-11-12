# HTTP H1 통합 실습

저장소 루트에서 실행한다. Python 3.10 이상, 실행 중인 Docker Desktop이 필요하다.

```powershell
python -m netrunner.http_demo
```

처음 실행하면 이미지를 내려받고 별도 Compose 프로젝트 `netrunner-http`를 시작한다.
기존 `h1-lab` 실습과 별개이며, PC에서 확인할 주소는 http://localhost:8081/ 이다.
심판과 진단기가 사용하는 대상은 클라이언트 컨테이너에서 접근하는 `http://nginx/`이다.

## 구현한 범위

- `netrunner/http_lab.py`의 `HttpDockerLab`은 공통 `Lab` 인터페이스를 따른다.
- `health_check(target_url)`는 client 컨테이너에서 HTTP 200을 확인한다.
- `inject(FaultSpec("H1"))`는 upstream 포트를 8000에서 미사용 포트 8001로 변경한다.
- `verify_symptom(scenario)`는 동일 위치에서 실제 HTTP 502 응답을 확인한다.
- `reset()`은 정상 설정을 복구하고, 심판이 이어서 `health_check`로 정상화를 확인한다.
- 지원 장애는 단일 H1뿐이다. 전용 설정 `config/http_lab.json`에도 H1만 활성화한다.
- 각 함수는 공통 `LabResult`를 반환하며 명령, 출력, 측정 위치를 observations에 남긴다.

기준 설정은 `labs/http/nginx.conf`이고, 실행 중 변경하는 `active.conf`는 Git에서 제외한다.
복구는 기준 설정을 복사하므로 이전 실행이 실패해 장애 설정이 남아도 다시 정상화할 수 있다.

## 심판 연결과 근거

`http_demo.py`는 기존 `Referee`를 사용해 검증, 정상 확인, 주입, 재현 확인, 진단,
채점, 복구를 실행한다. 결과는 `runs/http-*.jsonl`, 결과 화면은 같은 이름의 HTML로 저장한다.
원본 HTTP 응답(본문은 최대 4096바이트)은 client 컨테이너에서 수집하고
`Probe`와 `probe:http-1` 근거 참조를 통해 심판에 전달한다.

`HttpDiagnoser`는 실제 HTTP 응답을 검사하는 규칙 기반 진단기(`rules-http`, 방식 A)다.
502 응답과 리다이렉트 경로의 반복을 각각 판별하고 관측한 내용을 근거로 첨부한다.
리다이렉트는 같은 출처에서만 따라간다. 이 데모가 주입하는 장애는 H1이다.

현재 H1 주입 방식은 설계안의 '뒷단 서버 중지'와 다르다. 포트 불일치로 502를 재현하므로
팀 통합 시 구현 방식을 합의해야 한다. 다른 네트워크 장애나 복합 장애는 구현하지 않았다.
전체 실험망 담당자는 이 클래스의 네 함수를 참고해 H1 처리를 통합할 수 있다.

## 종료

아래 명령은 이 실습의 컨테이너와 네트워크만 정리한다.

```powershell
docker compose -p netrunner-http -f labs/http/compose.yaml down
```

## 팀에 전달할 파일

`netrunner/http_lab.py`, `netrunner/http_demo.py`, `labs/http/`,
`config/http_lab.json`, `fixtures/http/h1.scenario.json`, 이 문서.
기존 심판, 기본 Lab 규약, 공용 실험망 설정은 수정하지 않는다.
