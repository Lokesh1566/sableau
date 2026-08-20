# Requirement audit

Verified against the live MERIDIAN CORE target on 2026-08-20. Automated result: `90 passed, 13 skipped`; skipped tests require explicitly enabled live services.

| Requirement | Status | Evidence / implementation |
|---|---|---|
| Real LLM observe/decide/act discovery | done | Five Anthropic live discovery traces indexed in `evidence/README.md`; `src/sableau/discovery/` |
| Typed, versioned, reviewable artifacts | done | `src/sableau/schema/`; seven `capabilities/meridian_core.*.v1.0.0.json` files |
| All seven requested banking functions | done | sign-on, find member, balance, transfer, open share, update member, account hold |
| Deterministic replay with new parameters | done | representative replay for every function; all report `llm_calls=0` |
| Hidden per-form transaction token | done | observed only as `[opaque]`; submitted through the live form; absent from artifacts |
| Business/recoverable/hard-failure separation | done | `ReplayResult`, `OutcomeCategory`, eleven MERIDIAN outcome detectors |
| Validation, not-found, permission, timeout, maintenance, server errors | done | typed input validation plus `capabilities/outcomes/meridian_core.json` |
| Successful and exceptional evidence | done | `evidence/runs/`; teller denial run `replay_20260820T202123Z_ed01a2` |
| Pause and escalate with context | done | teller denial pauses control and emits step/URL/reason/screenshot context |
| Same-session take-control/resume | done | `src/sableau/kernel/control.py`, `src/sableau/operator/`, optional `scripts/demo_handoff.py` fixture |
| Host/action allowlist and risky confirmation | done | `policy-core.json`; CLI/API/chat/dashboard confirmations |
| Secret and PII redaction | done | typed sensitivities, redaction boundary, safe artifact dump, redacted API/dashboard evidence |
| Capability catalog and invoke API | done | exactly seven live MERIDIAN entries; `/api/capabilities`, `/invoke`, OpenAPI docs |
| Banking chatbot | done | safe reads plus confirmation-gated write intents in `src/sableau/api/app.py` |
| Dashboard inputs/live steps/results/history/evidence | done | seven forms and chat examples; watchable run API; live timings and escalation badge in `src/sableau/api/static/index.html` |
| Exact README demo commands | done | `README.md` |
| Seven-heading design report | done | `REPORT.md` |

Deliberate cuts—desktop/screenshot adapters, browser fleet, credential vault, encrypted evidence store, and production identity/dual-control integration—are documented in `REPORT.md` under **Cuts**.
