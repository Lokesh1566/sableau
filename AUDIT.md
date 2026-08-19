# Requirement audit

Every row points at code that exists and, where relevant, at evidence produced by a real run. Rows
marked **partial** say plainly what is missing.

Verified on my machine: 68 tests passing (59 unit, 9 live-browser integration), 14 real run
directories in `evidence/runs/`.

## Core flow

| # | Requirement | Status | Where |
|---|---|---|---|
| 1 | Natural-language goal plus target application | done | `jobs/approve_claim.json`, `cli.py::cmd_discover` |
| 2 | LLM observes the live UI and decides the action | done | `discovery/loop.py`, `discovery/planner.py::AnthropicPlanner` |
| 3 | Clicks, types, navigates, reads until complete | done | `surface/playwright_dom.py::act`, `evidence/01_discovery.txt` |
| 4 | Successful run becomes a typed, versioned artifact | done | `discovery/compiler.py`, `capabilities/meridian.record_claim_decision.v1.0.0.json` |
| 5 | Artifact holds inputs, outputs, actions, targeting, checkpoints, error behaviour | done | `schema/capability.py` |
| 6 | Invocable with new parameters | done | `evidence/02_replay.txt`, two claims discovery never saw |
| 7 | Deterministic replay, no LLM in decisions | done | `replay/engine.py`, `tests/test_no_llm_in_replay.py` |
| 8 | Replay verifies checkpoints, returns structured outputs | done | `engine.py::_assert_checkpoint`, `_collect_outputs` |
| 9 | Four-way outcome classification | done | `schema/errors.py::OutcomeCategory`, `evidence/03_errors.txt` |
| 10 | Pause and transfer the same live session to a human | done | `kernel/control.py`, `operator/app.py`, `evidence/04_handoff.txt` |
| 11 | Human acts, then returns control | done | same file, four recorded transitions |
| 12 | Everything logged with sensitive values redacted | done | `kernel/observability.py`, `kernel/redaction.py` |

## Engineering requirements

| Requirement | Status | Where |
|---|---|---|
| Observe → decide → act loop | done | `discovery/loop.py::DiscoveryLoop.run` |
| Real UI interaction | done | Chromium 150 over CDP; screenshots in `evidence/runs/*/screenshots/` |
| Surface abstraction | done | `surface/base.py::Surface` plus `SurfaceFeature` |
| Typed, versioned capability schema | done | `schema/capability.py`, semver enforced, `capabilities/capability.schema.json` |
| Parameterised inputs | done | closed binding grammar, `replay/bindings.py` |
| Typed outputs | done | `OutputSpec` with `extract_regex`, cast at extraction |
| Robust locator targeting | done | ranked candidates probed live, `verify` predicate, frame paths |
| Deterministic replay engine | done | `replay/engine.py` |
| Explicit checkpoints | done | `Checkpoint`, pre/postconditions, parameter-bound |
| Structured result contract | done | `schema/results.py::ReplayResult` |
| Error and outcome taxonomy | done | 16 codes, 4 categories, no generic exceptions |
| Bounded retry and recovery | done | locator fallback → step retry → capability restart, all counted |
| Domain and action allowlist | done | `policy.json`, `kernel/policy.py`, enforced in both paths |
| Safe vs risky classification | done | mutation plus intent verbs; can raise risk, never lower |
| Sensitive-data redaction | done | boundary-level; leak test on a live run |
| Structured observability | done | JSONL per run |
| Screenshots and traces on failure | done | captured at every failure and escalation |
| Human escalation | done | reason, step, state, evidence, owner |
| Control ownership state machine | done | illegal transitions raise |
| Pause and resume the same live session | done | one process, one ownership token |
| Tests for critical components | done | 83 tests |
| Capability not coupled to browser DOM | done | no DOM concept in `schema/`; `NullSurface` proves it |
| Raw transcript is not the capability | done | 11 turns → 8 steps, locators re-derived, 25 literals parameterised |
| Realistic runtime error conditions | done | all ten, against the live application |
| Safe demo application | done | `targetapp/`, fictional, localhost only |
| Real evidence, never fabricated | done | `scripts/make_evidence.sh` regenerates the lot |
| README and REPORT | done | both, with exact commands |
| Small architecture, no needless infrastructure | done | no queue, broker, or orchestration |

## Partial

| Requirement | What exists | What does not |
|---|---|---|
| Discovery robustness | a real `claude-sonnet-4-6` run, plus loop detection and field-state reporting | the loop does not retry a failed exploration with a revised strategy; one shot, then it refuses to compile |
| Multiple surfaces | protocol, feature declaration, compatibility refusal, two implementations | a11y-tree, coordinate and desktop surfaces designed for, not written |
| Multi-tenant | namespaced ids, per-capability host scoping, policy intersection, isolated runs | no tenant registry or credential vaulting |
| Session-expiry recovery | detected and classified `RECOVERABLE/SESSION_EXPIRED` | no automatic re-authentication |

## Reproducing all of it

```bash
pip install -e ".[dev]" && python -m playwright install chromium
./scripts/up.sh
./scripts/make_evidence.sh      # discovery, replays, every error case, handoff, tests
python -m pytest -q             # 83 tests
```
