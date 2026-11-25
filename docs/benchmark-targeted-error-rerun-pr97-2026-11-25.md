# PR #97 targeted error-case remeasurement (2026-11-25)

## Scope and setup

This reran the 51 rounds that were invalid in the original fixed-set benchmark, not the full 30-scenario × five-method × three-repeat benchmark. Each prior failing attempt was rerun once with its original method and scenario: B 35, C 7, C-1 2, and C-2 7. B-13 needed one extra attempt because its first run did not reproduce the injected symptom; that extra run scored successfully.

- Code under test: [PR #97](https://github.com/Se-joong-Kim/NetRunner/pull/97), head `2d22509`.
- Model: `opencode-go/glm-5.3-flash` through OpenCode 2.3.0.
- Runtime: rebuilt Docker experiment network; all final sample runs verified reset.
- Limits: `Limits.max_seconds=300`, also used as the per-call timeout ceiling. The earlier failure rerun used 120 seconds, so wall-time comparisons are not like-for-like.
- Baseline counts are from [the 2026-11-24 failure rerun report](./benchmark-failure-reruns-2026-11-24.md).

## Results

| Method | Previously scored / invalid | Targeted rerun scored / diagnostic-invalid | Stage correct | Cause correct |
|---|---:|---:|---:|---:|
| B | 21 / 14 | 35 / 0 | 22/35 (63%) | 22/35 (63%) |
| C | 6 / 1 | 7 / 0 | 5/7 (71%) | 4/7 (57%) |
| C-1 | 1 / 1 | 2 / 0 | 0/2 (0%) | 0/2 (0%) |
| C-2 | 7 / 0 | 7 / 0 | 7/7 (100%) | 7/7 (100%) |
| **Total** | **35 / 16** | **51 / 0** | **34/51 (67%)** | **33/51 (65%)** |

All 51 selected cases ultimately produced scored rounds, with reset verified for every final run. One initial B attempt (`FS09-T1-SHOP`) was invalid only because the injected symptom did not reproduce; the single retry reproduced the symptom and scored correctly. There were no final diagnostic-format/action failures in the selected cases.

Scored output is not the same as a correct diagnosis: B and especially C-1 still had substantial stage/cause error rates. This remeasurement supports the PR's targeted reliability changes, not an overall accuracy improvement claim.

## Focused regression evidence

- 19 selected tests for response parsing, invalid request/evidence retries, deadline/call limits, empty output, and citation validation passed.
- Replaying the 14 stored persistent B failure responses through the PR decoder accepted all 14; the previous strict JSON parser rejected all 14.

These checks and the 51-case rerun do not replace a full benchmark comparison. Raw run records and HTML reports were kept outside the repository at `/tmp/netrunner-pr97-full-error-results/` to avoid committing response/probe details.
