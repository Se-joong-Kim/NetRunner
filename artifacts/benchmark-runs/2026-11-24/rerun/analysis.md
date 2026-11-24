# 진단 실패 라운드 재실행 상세 분석

- 원본 측정 커밋: `d3dde94`
- 재실행 코드 커밋: `91e509c7d4a5f1e872ee4a11ee9374be76dc07ef`
- GLM 모델: `opencode-go/glm-5.3-flash`; OpenCode 호출 제한: 120.0초
- 원본 무효 라운드 51건을 원본 순서대로 각각 1회 재실행했다.
- 재실행 성공은 35/51건, 진단 실패 재현은 16/51건이었다.
- 증상 재현 51/51, 초기화 확인 51/51; 실험망 단계 실패 0건.
- Raw model I/O: `model-io.jsonl`
- Persistent-failure calls only: `persistent-failure-calls.jsonl` (36 call records; full prompt and raw OpenCode output).
- Full rerun records: `rounds.jsonl`
- Per-attempt details: `details.jsonl`

## 방식별 결과

| 방식 | 재실행 | 정상 채점 | 진단 실패 재현 | 모델 호출 | 진단기 검사 호출 |
|---|---:|---:|---:|---:|---:|
| B | 35 | 21 | 14 | 66 | 0 |
| C | 7 | 6 | 1 | 33 | 23 |
| C-1 | 2 | 1 | 1 | 10 | 6 |
| C-2 | 7 | 7 | 0 | 28 | 20 |

## 재현된 세부 실패

- **B — 14건:** 모두 2회 호출 뒤에도 JSON 디코딩에 실패했다. 최종 응답의 파서 오류는 `Expecting value` 8건(1행 1열), `Extra data` 6건(3행 1열)이었다. B 전체 재실행 35건 중 4건은 첫 호출에 성공, 17건은 형식 재요청 뒤 성공, 14건은 두 호출 모두 실패했다.
- **C — 1건:** C-01의 네 번째 모델 호출이 120초 제한에 걸렸다. 앞에서 검사 선택 두 번과 검사 실행 두 건이 있었고, 세 번째 응답은 JSON 파싱 오류였다.
- **C-1 — 1건:** C-1-01의 네 번째 응답은 JSON 객체였지만 키가 두 개여서 허용된 검사 선택/최종 진단 응답 스키마와 맞지 않았다. 앞선 모델 호출 세 번은 검사 선택 응답이었고 검사 세 건을 실행했다.
- **C-2 — 0건:** 원래 실패 7건 모두 재실행에서 정상 채점됐다. 호출 도중 JSON 형식 오류 1회가 있었지만 최종 실패로 이어지지 않았다.

## 건별 재실행 원장

건 ID는 진단기별 원본 무효 라운드 순번이다. 시나리오 ID, 정답, 검사 출력은 여기에 복사하지 않았다.

| 건 ID | 원본 무효 사유 | 재실행 결과 | 모델 호출별 형식/종료 상태 | 검사 수 | 초기화 |
|---|---|---|---|---:|---|
| C-01 | 진단 실행 오류: CompletionError: opencode 시간 초과(120초) | invalid: 진단 실행 오류: CompletionError: opencode 시간 초과(120초) | 검사 선택 응답 → 검사 선택 응답 → JSON 파싱 오류(Expecting value, 1행 1열) → 120초 호출 시간 초과 | 2 | 확인 |
| B-01 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Expecting value, 1행 1열) | 0 | 확인 |
| C-02 | 진단 출력 형식 오류: 이번 라운드에서 실행한 검사만 근거로 인용할 수 있음 | scored | 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → JSON 파싱 오류(Expecting value, 1행 1열) → 진단 결과 응답 | 5 | 확인 |
| C-2-01 | 진단 출력 형식 오류: GLM이 허용되지 않은 검사 또는 응답 형식을 요청함 | scored | 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 진단 결과 응답 | 5 | 확인 |
| B-02 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Extra data, 3행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-03 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-04 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | 진단 JSON 형식 정상 | 0 | 확인 |
| B-05 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Extra data, 3행 1열) → JSON 파싱 오류(Expecting value, 1행 1열) | 0 | 확인 |
| C-2-02 | 진단 출력 형식 오류: GLM 응답이 JSON 객체 형식이 아님 | scored | 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 진단 결과 응답 | 3 | 확인 |
| C-03 | 진단 출력 형식 오류: GLM 응답이 JSON 객체 형식이 아님 | scored | 검사 선택 응답 → 검사 선택 응답 → 진단 결과 응답 | 2 | 확인 |
| B-06 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Expecting value, 1행 1열) | 0 | 확인 |
| C-2-03 | 진단 출력 형식 오류: GLM 응답이 JSON 객체 형식이 아님 | scored | 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → JSON 파싱 오류(Expecting value, 1행 1열) → 진단 결과 응답 | 3 | 확인 |
| B-07 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| C-2-04 | 진단 출력 형식 오류: GLM이 허용되지 않은 검사 또는 응답 형식을 요청함 | scored | 검사 선택 응답 → 검사 선택 응답 → 진단 결과 응답 | 2 | 확인 |
| B-08 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Expecting value, 1행 1열) | 0 | 확인 |
| B-09 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| C-2-05 | 진단 출력 형식 오류: 이번 라운드에서 실행한 검사만 근거로 인용할 수 있음 | scored | 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 진단 결과 응답 | 3 | 확인 |
| B-10 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Extra data, 3행 1열) | 0 | 확인 |
| C-04 | 진단 출력 형식 오류: GLM이 허용되지 않은 검사 또는 응답 형식을 요청함 | scored | 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 진단 결과 응답 | 5 | 확인 |
| B-11 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Extra data, 3행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-12 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| C-05 | 진단 출력 형식 오류: GLM이 허용되지 않은 검사 또는 응답 형식을 요청함 | scored | 검사 선택 응답 → 검사 선택 응답 → 진단 결과 응답 | 2 | 확인 |
| C-2-06 | 진단 출력 형식 오류: GLM 응답이 JSON 객체 형식이 아님 | scored | 검사 선택 응답 → 검사 선택 응답 → 진단 결과 응답 | 2 | 확인 |
| B-13 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Expecting value, 1행 1열) | 0 | 확인 |
| B-14 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Expecting value, 1행 1열) | 0 | 확인 |
| B-15 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Extra data, 3행 1열) | 0 | 확인 |
| B-16 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-17 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | 진단 JSON 형식 정상 | 0 | 확인 |
| B-18 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-19 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-20 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Expecting value, 1행 1열) | 0 | 확인 |
| B-21 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Expecting value, 1행 1열) | 0 | 확인 |
| B-22 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| C-1-01 | 진단 출력 형식 오류: GLM이 허용되지 않은 검사 또는 응답 형식을 요청함 | invalid: 진단 출력 형식 오류: GLM이 허용되지 않은 검사 또는 응답 형식을 요청함 | 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 응답 객체 스키마 오류(2개 키) | 3 | 확인 |
| C-1-02 | 진단 출력 형식 오류: GLM 응답이 JSON 객체 형식이 아님 | scored | 검사 선택 응답 → 검사 선택 응답 → JSON 파싱 오류(Expecting value, 1행 1열) → 진단 결과 응답 → 검사 선택 응답 → 진단 결과 응답 | 3 | 확인 |
| B-23 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Extra data, 3행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-24 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Extra data, 3행 1열) | 0 | 확인 |
| B-25 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-26 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-27 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | 진단 JSON 형식 정상 | 0 | 확인 |
| B-28 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Extra data, 3행 1열) | 0 | 확인 |
| B-29 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-30 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-31 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Expecting value, 1행 1열) → JSON 파싱 오류(Extra data, 3행 1열) | 0 | 확인 |
| C-06 | 진단 출력 형식 오류: GLM 응답이 JSON 객체 형식이 아님 | scored | 검사 선택 응답 → 검사 선택 응답 → 진단 결과 응답 → 검사 선택 응답 → 진단 결과 응답 | 3 | 확인 |
| B-32 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | 진단 JSON 형식 정상 | 0 | 확인 |
| B-33 | 진단 실행 오류: CompletionError: 모델 응답이 비어 있음 | scored | JSON 파싱 오류(Extra data, 3행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| B-34 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | scored | JSON 파싱 오류(Expecting value, 1행 1열) → 진단 JSON 형식 정상 | 0 | 확인 |
| C-2-07 | 진단 출력 형식 오류: 이번 라운드에서 실행한 검사만 근거로 인용할 수 있음 | scored | 검사 선택 응답 → 검사 선택 응답 → 진단 결과 응답 | 2 | 확인 |
| B-35 | 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | invalid: 진단 출력 형식 오류: 방식 B는 JSON 객체를 제출해야 함 | JSON 파싱 오류(Extra data, 3행 1열) → JSON 파싱 오류(Extra data, 3행 1열) | 0 | 확인 |
| C-07 | 진단 출력 형식 오류: 이번 라운드에서 실행한 검사만 근거로 인용할 수 있음 | scored | 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 검사 선택 응답 → 진단 결과 응답 | 4 | 확인 |
