# Design report

I wrote this to be defended in a room rather than skimmed. Each section says what I built, why I
chose that shape over the alternatives, and where it breaks.

---

## 1. Architecture

Five layers, dependencies pointing strictly downward. Nothing below the discovery layer knows a
language model exists.

```
┌───────────────────────────────────────────────────────┐
│ Entry points:  discover   |   replay   |   handoff     │
└──────────────┬────────────────────────┬────────────────┘
               │                        │
┌──────────────▼─────────┐  ┌───────────▼────────────────┐
│ Discovery              │  │ Replay engine              │
│  planner (LLM or rule) │  │  NO LLM DEPENDENCY         │
│  observe→decide→act    │  │  steps, retries, restarts  │
│  locator probing       │  │  checkpoints, outcomes     │
│  compiler              │  │  typed output extraction   │
└──────────────┬─────────┘  └───────────┬────────────────┘
               │                        │
        ┌──────▼────────────────────────▼───────┐
        │ Kernel                                │
        │  control state machine + ownership    │
        │  policy enforcement                   │
        │  redaction                            │
        │  structured logging + evidence        │
        └──────────────────┬────────────────────┘
                           │
        ┌──────────────────▼────────────────────┐
        │ Surface protocol                      │
        │  navigate / resolve / act / evaluate  │
        │  observe / evidence, + feature set    │
        └──────────────────┬────────────────────┘
                           │
        ┌──────────────────▼────────────────────┐
        │ PlaywrightDomSurface   NullSurface    │
        └───────────────────────────────────────┘
```

**Why the kernel sits between both paths.** Discovery and replay share policy, redaction, evidence
and control. If discovery had its own policy layer, the allowlist would be enforced twice with two
chances to drift apart. Sharing it means an exploring model is held to exactly the limits production
replay is held to.

**Why the surface sits below the kernel.** The operator console needs to act on the session, and I
wanted it to go through the same `Surface.act` path automation uses. That way a human click is
recorded in the same trace format, subject to the same allowlist, and visible in the same audit
trail. Giving the console its own browser handle would have been three lines shorter and would have
quietly made the audit trail a lie.

**Why one process.** The handoff requirement is that a person drives *the same live session*. Running
the operator console in a separate process means serialising session state or reattaching over CDP,
both easy to get subtly wrong and hard to prove. One process, one event loop, one ownership token
makes "same session" true by construction, and the tests assert the page is continuous across the
transfer.

**Stack.** Python 3.11+, Pydantic v2, Playwright, FastAPI, pytest.

- *Pydantic v2* because the artifact is the highest-value thing here, and it gives me discriminated
  unions for the locator and action hierarchies, validation at load time, and a JSON Schema I ship in
  the repo. That makes "another system could consume this" demonstrable rather than asserted.
- *Playwright over CDP* rather than launching per run, so the browser outlives any single run and can
  be handed over mid-task.
- *Anthropic tool use* rather than parsing prose, so the planner physically cannot express an action
  outside the vocabulary the policy layer and replay engine already understand. That is what makes
  the allowlist enforceable instead of advisory.
- *Filesystem persistence, no database.* There is no queue, no broker, no orchestration. None of it
  would have earned its keep at this size.

---

## 2. Artifact schema

`src/sableau/schema/capability.py`. Exported JSON Schema: `capabilities/capability.schema.json`.

A capability answers, without reference to any other document: what it does, what it needs, what it
returns, what it will do to the application, how each control is found, what must be true along the
way, what known answers exist, and what it is allowed to touch.

```jsonc
{
  "schema_version": "1.0.0",
  "capability_id": "meridian.record_claim_decision",
  "version": "1.0.0",
  "provenance": { "goal": "...", "planner": "heuristic", "model": null,
                  "trace_ref": "evidence/runs/.../trace.json", "compiler_version": "1.0.0" },
  "surface":  { "kind": "dom",
                "required_features": ["frames","label_query","role_query","testid_query",...] },
  "safety":   { "allowed_hosts": ["127.0.0.1:8099"],
                "allowed_actions": ["click","read","select","type","wait"],
                "risk_level": "high", "confirm_steps": ["s6_save_the_decision"],
                "redact_paths": ["input.note"] },
  "inputs":   [{ "name": "claim_id", "type": "string", "pattern": "^CLM-[0-9]{6}$" }, ...],
  "outputs":  [{ "name": "confirmation_code", "type": "string",
                 "source": { "step": "s7_...", "binding": "text",
                             "extract_regex": "MCD-[0-9]+" } }, ...],
  "steps":    [...],
  "checkpoints": [...],
  "known_outcomes": [...],
  "recovery": { "global_max_retries": 3, "escalate_on": [...],
                "escalation_mode": "human_handoff" }
}
```

### The four decisions I would defend hardest

**Targeting is a ranked candidate list, and the model does not write it.** A step records several
ways to find one control:

```jsonc
"target": {
  "candidates": [
    { "strategy": "role", "role": "link", "name_equals": "{{input.claim_id}}", "confidence": 0.9 },
    { "strategy": "text", "text": "{{input.claim_id}}", "confidence": 0.6 },
    { "strategy": "css",  "value": "table.tbl-9f3a > tbody > tr > td > a", "confidence": 0.3 }
  ],
  "frame_path": [],
  "ambiguity_policy": "fail_if_multiple",
  "verify": { "kind": "attribute_contains", "attr": "href", "value": "/claims/{{input.claim_id}}" }
}
```

None of those candidates were invented. At the moment an action succeeded, the loop asks the surface
to describe the element it just acted on, generates every plausible way to name it, and **probes each
one against the live DOM**. Only descriptions that resolved to exactly one element survive
(`discovery/loop.py::_probe`, `discovery/compiler.py::_durable_candidates`). The planner says *what*
it wants in human terms; measurement decides *how* that gets recorded.

`verify` is the safety catch. A locator that still resolves after the application changes can quietly
match the wrong control, which I think is the worst failure mode in UI automation precisely because
it succeeds. The verify predicate turns a stale match into a loud failure.

One refinement worth calling out because it was a bug first: text-based locators are dropped for
value-bearing elements and for `read` steps. The text of a field you are reading is the *payload*,
not an identity. Recording `text = "MCD-77201"` would bake one run's data into the artifact and break
the very next replay.

**Parameterisation is a closed grammar.** Only `{{input.name}}` and `{{env.NAME}}` resolve. No
arithmetic, no attribute walks, no function calls, so loading an artifact can never execute anything.
The compiler substitutes bindings everywhere a literal came from an input value — including *inside
locator names and verify predicates*, which is what makes replay look for a different link on every
invocation. Load-time validation rejects a binding with no matching input spec.

**Known outcomes are first class, separate from errors.** "No claims match that search" is a
successful execution with a business answer. Modelling it as an exception forces every caller to
parse error strings to work out whether anything is actually wrong. The catalogue
(`capabilities/outcomes/meridian.json`) is curated once per application and attached to every
capability compiled against it, because the set of things an application can legitimately say is
domain knowledge, not something worth rediscovering per workflow.

**Safety travels with the artifact.** Hosts, action allowlist and risk level are derived from what
the run actually did, not from what anything claimed it would do. At replay the deployment policy is
*intersected* with the artifact's constraints: a capability can only narrow, never widen. Declaring a
host the deployment forbids is refused outright (`kernel/policy.py::intersect`, demonstrated in
`evidence/03_errors.txt`).

### Why the transcript is not the capability

The trace is one run with one set of values, full of planner rationale, retries, dead ends and
literal data. The capability is general, typed, measured and executable. Concretely: discovery
produced 11 turns; the compiler emitted 8 steps, 2 checkpoints, 2 typed outputs and 8 known outcomes,
with 25 literal values replaced by bindings and every locator re-derived from live measurement. I
keep the trace and reference it from `provenance.trace_ref` for audit, and the compiler *refuses* to
emit an artifact from an unsuccessful run.

---

## 3. Determinism and error handling

### Enforcing zero LLM calls

Three independent mechanisms, because a comment saying "don't import this" is not an architecture:

1. **Import graph.** `tests/test_no_llm_in_replay.py` parses every module under `replay/`, `schema/`,
   `surface/` and `kernel/` and fails if any of them reaches an LLM SDK, an HTTP client, or the
   discovery package.
2. **Poisoned SDK.** A test replaces `anthropic` and `openai` with an object that raises on any
   attribute access, then runs a complete replay. It passes.
3. **Result contract.** `ReplayResult.llm_calls` is carried to the caller and asserted zero. Every
   replay row in `evidence/README.md` shows `0`.

### The taxonomy

Sixteen codes across four categories (`schema/errors.py`). Every code has a declared category and a
declared retryability, so nothing depends on inspecting a message string.

| Code | Category | Disposition |
|---|---|---|
| `RECORD_NOT_FOUND` | BUSINESS_OUTCOME | return the answer |
| `ALREADY_PROCESSED` | BUSINESS_OUTCOME | return, no second write attempted |
| `INVALID_INPUT` | HARD_FAILURE | rejected before the browser is touched |
| `VALIDATION_ERROR` | HARD_FAILURE | capture the application's message, return |
| `PERMISSION_DENIED` | HARD_FAILURE | evidence, escalate if configured |
| `CHECKPOINT_MISMATCH` | HARD_FAILURE | evidence, return |
| `POLICY_VIOLATION` | HARD_FAILURE | refuse, ideally before acting |
| `SURFACE_INCOMPATIBLE` | HARD_FAILURE | refuse at load |
| `ABORTED_BY_OPERATOR` | HARD_FAILURE | return |
| `MISSING_CONTROL` | RECOVERABLE | next locator candidate, then escalate |
| `AMBIGUOUS_CONTROL` | RECOVERABLE | refuse to guess, escalate |
| `UNEXPECTED_DIALOG` | RECOVERABLE | escalate to a person |
| `SLOW_LOAD` | RECOVERABLE | extended wait, then retry |
| `TRANSIENT_FAILURE` | RECOVERABLE | bounded retry or capability restart |
| `SESSION_EXPIRED` | RECOVERABLE | return; caller re-authenticates |

All ten realistic conditions I set out to handle are demonstrated against the live application in
`evidence/03_errors.txt`, plus a policy refusal.

### Ordering, which is where the subtlety lives

Known outcomes are evaluated **before** postcondition checkpoints, and **on step failure before
escalation**. Both of these were bugs first and fixes second, and both matter:

- A permission denial makes the "decision recorded" checkpoint fail. Checking the checkpoint first
  reports `CHECKPOINT_MISMATCH` — technically true, operationally useless. The application told us
  something specific, so that answer wins.
- After session expiry the search box genuinely is missing, so the step fails with `MISSING_CONTROL`.
  Escalating a human for that wastes their time. Consulting declared outcomes first yields
  `SESSION_EXPIRED`, which the caller can fix without a person.

### Bounded recovery

Three levels, all counted and reported:

- **Locator fallback**, free: the next candidate in the ranked list. `evidence/02_replay.txt` shows
  step `s3` resolving via candidate index 1 because the recorded test id did not exist on that page.
- **Step retry**, `on_error.retry.max_attempts` with backoff, drawn from a capability-wide budget so
  a run cannot retry forever by spreading attempts across steps. The report marks the step
  `recovered` with `attempts=2` — a success that took two goes stays visible rather than being
  smoothed over.
- **Capability restart**, for outcomes declared `recovery: "restart_capability"`. Claim `CLM-004216`
  returns a 503 on first load; the run restarts from step one, bounded by `global_max_retries`, and
  succeeds.

### Determinism hygiene

No `sleep` anywhere in the execution path. Every wait is a condition with a timeout. Given the same
application state and parameters, replay takes the same steps in the same order, and where it does
not, the report says so explicitly.

---

## 4. Heterogeneity and multi-tenant design

### Surface abstraction

The capability schema contains no DOM concept. Not merely "no Playwright import" — no
`querySelector`, no element handles, no CSS in the required path. A locator candidate is
`role + accessible name`, or `label`, or `test id`, with CSS present only as a recorded last resort.

`Surface` (`surface/base.py`) is a six-method protocol: `navigate`, `resolve`, `act`, `evaluate`,
`observe`, `evidence`. The replay engine imports nothing else.

What stops that being decorative is **feature declaration**. Each surface declares a
`frozenset[SurfaceFeature]`; each capability declares `required_features`, derived by the compiler
from the strategies it actually recorded. At load, replay intersects them and refuses with
`SURFACE_INCOMPATIBLE` before touching anything. A screenshot-plus-coordinates surface would declare
`{COORDINATES, SCREENSHOT}`, so a capability whose locators are role-based gets correctly rejected
instead of silently misbehaving.

I wrote two implementations. `PlaywrightDomSurface` is the real one. `NullSurface` is an in-memory
fake against which the *entire* engine — checkpoints, outcomes, retries, escalation, output
extraction — is tested with no browser. That second implementation is the proof: if the engine had
DOM assumptions baked into it, `NullSurface` could not exist.

**Frames** are handled as `frame_path` on the target, not as a special case. The decision panel in
the demo application lives in an iframe, and the compiled artifact records `"frame_path":
["decision"]` on three steps. Framesets and nested documents extend the same list.

**What a new surface costs.** Accessibility tree: implement `resolve` against an a11y tree, declare
`{ROLE_QUERY, A11Y_TREE}`, and role-based capabilities port unchanged. Screenshot plus coordinates:
declare `{COORDINATES, SCREENSHOT}` and add a `PointLocator` strategy; existing capabilities are
correctly refused rather than silently degraded. Native desktop: same protocol over an automation
API, `kind: "desktop"`.

### Multi-tenancy

Present in the design, not fully built.

- `capability_id` is namespaced (`meridian.record_claim_decision`) and versioned semantically, so two
  tenants can run different versions of the same workflow side by side.
- `surface.app_id` plus `safety.allowed_hosts` scope an artifact to one application. A capability
  compiled against one tenant's host cannot be pointed at another's; the policy intersection refuses.
- `policy.json` is deployment-level and per-tenant by construction: the effective policy is always
  `deployment ∩ capability`.
- Runs are isolated by `run_id` with their own evidence directory, log stream and redactor.
- **Not built:** a tenant registry, per-tenant credential vaulting, or capability promotion between
  environments. See section 7.

---

## 5. Escalation and handoff

```
                    escalate(reason, evidence)
 AUTOMATION_RUNNING ──────────────────────────► PAUSED
         ▲                                        │ take_control(operator)
         │                                        ▼
         │            resume(decision)       HUMAN_CONTROL
         └────────────────────────────────────────┤
                                                  │ resume(ABORT)
                                                  ▼
                                               ABORTED
```

`SessionControl` (`kernel/control.py`) validates every transition against a legal-transition table
and raises on an illegal one. `automation_may_act()` gates the engine, which awaits the ownership
token before every action.

**Escalation captures**, all recorded before a human is called: the reason code from the taxonomy,
the human-readable reason, the step id, the current URL, a screenshot and DOM snapshot, the
timestamp, and who owns control.

**The operator acts on the same page.** The console holds the same `Surface` object. A click goes
through `Surface.resolve` and `Surface.act` — same path, same policy allowlist — and is recorded via
`control.record_human_action`. The audit trail does not distinguish "what automation did" from "what
the human did" in mechanism, only in the `actor` field.

**Resume carries a decision:** `CONTINUE_FROM_CURRENT_STEP` (the human did it by hand, move on),
`RETRY_STEP`, `SKIP_STEP`, or `ABORT`. `ABORTED_BY_OPERATOR` is its own code, not a generic failure.

Evidence from `evidence/04_handoff.txt`, a real run:

```
reason        UNEXPECTED_DIALOG: A compliance notice is covering the decision panel
at step       s3_open_the_matching_claim_re
screen        http://127.0.0.1:8099/claims/CLM-004214
evidence      evidence/runs/handoff_.../screenshots/escalation_compliance_dialog.png

06:54:57  AUTOMATION_RUNNING  -> PAUSED             owner=nobody     by automation (UNEXPECTED_DIALOG)
06:54:58  PAUSED              -> HUMAN_CONTROL      owner=human      by scripted-operator
06:54:58  HUMAN_CONTROL       -> AUTOMATION_RUNNING owner=automation by scripted-operator (CONTINUE_FROM_CURRENT_STEP)
06:55:01  AUTOMATION_RUNNING  -> DONE               owner=nobody     by automation (run complete)
human action: click {'target': 'ack-compliance', 'frame': 'main', 'via': 'testid'}

SUCCESS/NONE | outputs=confirmation_code=MCD-77201, decided_amount=385.0 | llm_calls=0
```

The demonstration drives the console's HTTP API rather than a person clicking, so it runs unattended
in CI. It is labelled `scripted-operator` in the audit trail and every request it makes is one the
page itself makes. `python -m sableau.cli handoff --hold 300` gives a real person the same console.

The same cycle is tested with no browser at all
(`test_missing_control_escalates_and_a_human_can_resume`), including the human repairing the screen
and choosing `RETRY_STEP`.

---

## 6. Safety

**Domain and action allowlists.** `policy.json`, enforced in discovery *and* replay. Non-HTTP schemes
refused outright. The effective policy is `deployment ∩ capability`, and a capability naming a
forbidden host is rejected before execution.

**Risky action classification.** Any state-mutating action whose stated intent contains an
irreversible business verb is `risky`. The heuristic can only *raise* risk, never lower it — a step
the compiler marked risky stays risky even if the policy would not have flagged it. Under a
confirmation-requiring policy a risky step needs `--confirm-risky`, and the demo capability's save
step is one. That is why `risk_level: "high"` ended up in the artifact: it was derived from what the
run did.

**Secrets.** Environment variables only, `.env` gitignored, `.env.example` documents the shape. No
credential is read by the replay path. `browser/node_modules/` is gitignored too — it is a 270 MB
downloaded binary with no business in version control.

**Redaction** happens at the boundary — the logger, the evidence writer, the artifact serialiser —
not at each call site, because per-call-site redaction is exactly what gets forgotten in the one path
that matters. Two mechanisms: registered secrets (declared `sensitivity: "secret"`, masked wherever
they appear, *including in text the application echoed back*) and pattern redaction (API-key shapes,
bearer tokens, card-like digit runs, national IDs, emails). An integration test pushes a known secret
through a full live run and greps the entire evidence tree for leakage.

**Observability.** One JSONL stream per run: decisions, actions with the resolved strategy and
candidate index, checkpoint results, outcome detections, retries, restarts, control transitions,
human actions. Screenshots and DOM snapshots on every failure and escalation.

**Target application safety.** Fictional insurer, synthetic claims, no real financial data, no real
credentials, bound to localhost.

---

## 7. Cuts, limitations and next steps

### What I cut deliberately

- **No queue, broker, container orchestration or service mesh.** None of it would have earned its
  keep. One process, a filesystem, and a browser.
- **No capability store or registry.** Artifacts are files. A real deployment needs versioned
  storage, promotion between environments, and signing.
- **No self-healing.** When a locator degrades, the engine escalates. Re-invoking discovery to repair
  a capability is the obvious next feature and I left it out on purpose, because a system that
  silently rewrites its own production artifacts needs an approval gate first.
- **Operator console is plain.** The transfer mechanism mattered more to me than the interface, and
  that is where the effort went.

### Limitations I would raise before being asked

1. **The committed evidence used the heuristic planner.** The machine I built this on had no API key.
   `AnthropicPlanner` is complete tool-use code on the same interface, and provenance records which
   planner ran. Everything else in `evidence/` is genuine execution: real Chromium 150, real clicks,
   real screenshots, real compilation, real replays.
2. **One surface implemented.** The abstraction is exercised by two implementations and enforced by
   feature declaration, but the a11y, coordinate and desktop surfaces are designed for, not written.
3. **Discovery is single-shot** and does not retry with a revised strategy on failure.
4. **Session expiry is classified, not repaired.** There is no re-authentication sub-capability.
5. **Outcome catalogues are curated by hand.** The planner does not propose known outcomes; a person
   writes them once per application. I think that is defensible, since what an application can
   legitimately say is domain knowledge, but it is manual work per application.
6. **Determinism is behavioural, not bit-exact.** Same steps, same order, same classifications;
   timing and confirmation codes vary because the application generates them.
7. **The demo application is friendlier than reality.** It has some test ids and clean accessible
   names. A genuinely hostile legacy application with generated ids, nested framesets and canvas
   widgets would stress locator synthesis much harder than this does.

### Next steps, in the order I would do them

1. **Capability repair loop.** On repeated `MISSING_CONTROL`, re-run discovery for that step only,
   produce a candidate patch, and require human approval before bumping the version.
2. **Accessibility-tree surface.** The highest-value second surface: it validates the abstraction
   against something genuinely different, while most role-based capabilities port unchanged.
3. **Capability registry** with signing, environment promotion, and a compatibility check on
   `schema_version`.
4. **Multi-tenant credential vaulting**, so `{{env.X}}` resolves per tenant from a secret store
   rather than the process environment.
5. **Drift detection.** Replay against a staging copy nightly and report locator candidates whose
   match count has changed, which is the earliest warning that an artifact is going stale.
