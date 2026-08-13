# Evidence

Every file here is output from a real execution on this machine. Nothing is
hand written or reconstructed. Rebuild the whole directory with:

    ./scripts/make_evidence.sh

Generated 2026-08-13T19:18:52+00:00.

## Transcripts

- **[Discovery](01_discovery.txt)** One observe, decide, act run against the live application, and the capability compiled from it.
- **[Deterministic replay](02_replay.txt)** The compiled capability executed with parameters it has never seen, twice, with no model in the loop.
- **[Outcome and error taxonomy](03_errors.txt)** Ten runtime conditions, each classified rather than raised.
- **[Human handoff](04_handoff.txt)** Automation pauses, a person acts on the same live session, automation resumes and finishes.
- **[Test results](05_tests.txt)** Unit suite and live integration suite.

## Run directories

Each contains `log.jsonl` (structured, redacted), a `result.json` or
`trace.json`, and `screenshots/` captured at failures and escalations.

| run | kind | outcome | detail | llm calls |
| --- | --- | --- | --- | --- |
| `discovery_20260813T191715Z_136f53` | discovery | success | planner=heuristic turns=11 | - |
| `handoff_20260813T191815Z_5ef50f` | replay | SUCCESS/NONE (escalated) | confirmation_code=MCD-77201, decided_amount=385.0 | 0 |
| `replay_20260813T191722Z_645bf0` | replay | SUCCESS/NONE | confirmation_code=MCD-77201, decided_amount=612.5 | 0 |
| `replay_20260813T191727Z_6f447d` | replay | SUCCESS/NONE | confirmation_code=MCD-77202, decided_amount=78.0 | 0 |
| `replay_20260813T191737Z_d6fac3` | replay | HARD_FAILURE/INVALID_INPUT | input claim_id does not match required format ^CLM-[0-9]{6}$ | 0 |
| `replay_20260813T191740Z_5a2368` | replay | HARD_FAILURE/INVALID_INPUT | input outcome must be one of ['APPROVED', 'REJECTED'], got 'MAYBE' | 0 |
| `replay_20260813T191740Z_a1dd32` | replay | BUSINESS_OUTCOME/RECORD_NOT_FOUND | search_no_match | 0 |
| `replay_20260813T191742Z_02f17e` | replay | BUSINESS_OUTCOME/ALREADY_PROCESSED | already_decided | 0 |
| `replay_20260813T191743Z_48a51a` | replay | HARD_FAILURE/VALIDATION_ERROR | The claims system rejected the decision form. | 0 |
| `replay_20260813T191747Z_1c6515` | replay | HARD_FAILURE/PERMISSION_DENIED | The signed in operator is not permitted to decide this claim. | 0 |
| `replay_20260813T191751Z_eec31c` | replay | SUCCESS/NONE | confirmation_code=MCD-77201, decided_amount=210.0 | 0 |
| `replay_20260813T191758Z_c89818` | replay | SUCCESS/NONE | confirmation_code=MCD-77202, decided_amount=78.0 | 0 |
| `replay_20260813T191805Z_4f01a0` | replay | RECOVERABLE/SESSION_EXPIRED | The session expired. Re-authenticate and invoke the capability again. | 0 |
| `replay_20260813T191815Z_011351` | replay | HARD_FAILURE/POLICY_VIOLATION | capability declares hosts that the deployment policy does not allow: [ | 0 |

Note the `llm calls` column: every replay row is zero. That number comes from
the engine's own result contract, and the same invariant is asserted
structurally in `tests/test_no_llm_in_replay.py`.

