# MERIDIAN CORE evidence

These run directories were produced by the application while driving the public live MERIDIAN UI. Discovery directories contain `trace.json`, a redacted structured `log.jsonl`, `capability.json`, and a final screenshot. Replay directories contain `result.json`, a redacted log, and richer evidence captured by the engine. Replays report `llm_calls=0` in their own result contract.

## Representative run pairs

| Capability | Discovery | Planner | Deterministic replay | Verified replay result |
|---|---|---|---|---|
| Sign on | `discovery_20260820T194521Z_786e4e` | Anthropic / Claude Sonnet 4.6 | `replay_20260820T194604Z_83d003` | `SUCCESS`, `MAIN MENU`, 0 LLM calls |
| Find member | `discovery_20260820T194820Z_4f525e` | Anthropic / Claude Sonnet 4.6 | `replay_20260820T195047Z_674998` | `SUCCESS`, member `101555`, 0 LLM calls |
| Check balance | `discovery_20260820T195108Z_5d74f6` | Anthropic / Claude Sonnet 4.6 | `replay_20260820T200221Z_1d13b5` | second member `102777`, balance/status extracted, 0 LLM calls |
| Transfer funds | `discovery_20260820T195824Z_19e43c` | Anthropic / Claude Sonnet 4.6 | `replay_20260820T200152Z_f36069` | posted to a different destination, confirmation `CN480086`, 0 LLM calls |
| Open new share | `discovery_20260820T200314Z_a6e611` | Anthropic / Claude Sonnet 4.6 | `replay_20260820T200553Z_acf2cb` | different branch/type, new share `103001-S0070-6`, 0 LLM calls |
| Update member | `discovery_20260820T201559Z_d99601` | labeled heuristic fallback | `replay_20260820T201627Z_c94dc7` | different contact values, `MEMBER INFORMATION UPDATED`, 0 LLM calls |
| Place account hold | `discovery_20260820T201653Z_545301` | labeled heuristic fallback | `replay_20260820T202053Z_705f5e` | supervisor posted hold, confirmation `CN480100`, 0 LLM calls |

The heuristic rows are intentionally identified as such in both this index and `capability.json`; they are not presented as model runs. Five genuine model-driven live discoveries remain bundled, exceeding the original requirement for at least one.

## Exceptional and escalation run

`replay_20260820T202123Z_ed01a2` invokes `meridian_core.place_account_hold` as `teller1`. The capability fills the live form and attempts to continue. MERIDIAN returns its teller-only authorization sentence, after which replay:

- classifies `HARD_FAILURE/PERMISSION_DENIED`;
- captures `screenshots/escalation_supervisor_override_required.png` and a DOM snapshot;
- transitions control from `AUTOMATION_RUNNING` to `PAUSED`;
- emits an escalation with capability, step, URL, reason, screenshot, and timestamps;
- returns with `llm_calls=0` and without posting the hold.

This is a natural target permission error, not an injected or mocked exception.
The committed run predates the interactive dashboard controls and intentionally
ends at `PAUSED`. Current watchable runs retain that same CDP session for an
identified operator to take control, act through the effective policy, and
return an explicit retry/continue/abort decision; the API integration suite
verifies the complete ownership cycle without mutating the public target.

## Data handling

Passwords, contact data, memos, and hold notes are redacted. The live form token appears only as `[opaque]` in observations and is absent from compiled artifacts. The public demo contains synthetic records; no real customer data is included.

## Live dashboard human handoff

Run: `api_20260821T045740Z_4e6889`

The live MERIDIAN account-hold workflow detected that `teller1` lacked
supervisor permission and paused automation. The identified dashboard operator
`operator.demo` took control of the same browser session and returned control
with `RETRY_STEP`. When MERIDIAN denied the operation again, the operator took
control a second time and selected `ABORT`.

The run finished as `HARD_FAILURE/ABORTED_BY_OPERATOR`, which is the expected
safe outcome. No account hold was posted. The run contains redacted logs,
control transitions, escalation records, DOM evidence, and screenshots.
