[![Tests](https://github.com/Lokesh1566/sableau/actions/workflows/tests.yml/badge.svg)](https://github.com/Lokesh1566/sableau/actions/workflows/tests.yml)
[![Project site](https://img.shields.io/badge/site-github%20pages-2c6a55)](https://lokesh1566.github.io/sableau/)

# Sableau

**Turning a language model's successful UI exploration into a deterministic, replayable capability.**

I built this to answer a question that kept bothering me about LLM computer-use agents: if a model
has already worked out how to do a job through an application's interface, why pay a model to work it
out again every single time?

So Sableau does it once. An LLM drives a real browser to complete a task. That successful run gets
compiled into a typed, versioned artifact. From then on the job runs from that artifact with no model
in the decision loop at all. When automation genuinely cannot continue, it hands the *same live
browser session* to a person and takes it back when they are done.

The case I had in mind is an application with no API, where the interface a human uses is the only
way in.

```
  goal + app          discovery              artifact              production
 ─────────────►  observe → decide → act  ──►  capability.json  ──►  replay(params) → result
                   (LLM, once)                (typed, versioned)    (no LLM, ever)
```

**[Project site](https://Lokesh1566.github.io/sableau/)** · **[Design report](REPORT.md)** ·
**[Requirement audit](AUDIT.md)** · **[Evidence from real runs](evidence/)**

---

## What is in here

| | |
|---|---|
| **Target application** | Meridian Claims Desk, a fictional insurer's back office I wrote for this. Search, open record, decide, confirm. The decision form sits in an iframe, the results table has no test ids, and eight seeded claims misbehave on purpose. |
| **Discovery** | An observe, decide, act loop. The planner emits one typed tool call per turn and is never allowed to write a selector. |
| **Compiler** | Turns one run into a general capability: ranked locators measured against the live DOM, parameter bindings, checkpoints, typed outputs, safety constraints. |
| **Replay engine** | Executes a capability with zero LLM calls, enforced structurally rather than by convention. |
| **Outcome taxonomy** | Sixteen codes across four categories. "Claim not found" is an answer, not an exception. |
| **Human handoff** | Automation pauses, an operator drives the same live page, automation resumes. |
| **Evidence** | [`evidence/`](evidence/) is real output from real runs. One command regenerates all of it. |

If you read one other file, read [REPORT.md](REPORT.md). That is where I explain why each decision
went the way it did.

---

## Install

Python 3.11 or newer.

```bash
git clone https://github.com/Lokesh1566/sableau.git && cd sableau
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
```

<details>
<summary>If <code>playwright install</code> cannot reach its CDN</summary>

Some networks block `cdn.playwright.dev`. `browser/` holds a three line Electron shell whose bundled
Chromium works identically over CDP, and Electron pulls from GitHub release assets instead:

```bash
cd browser && npm install && cd ..
```

`scripts/up.sh` uses this automatically when it is present *and* `xvfb-run` is available, which is
the Linux-sandbox case. Everywhere else it launches Playwright's own Chromium via
`scripts/browser_host.py`, which asks Playwright for the executable path rather than guessing at
install directories.
</details>

## Configure

```bash
cp .env.example .env
```

`.env` is gitignored. The only secret is `ANTHROPIC_API_KEY`, and it is needed **only** for
`discover --planner anthropic`. Replay never reads it.

`policy.json` is the deployment safety policy: allowed hosts, allowed action types, which verbs count
as risky. A capability can only ever be *more* restrictive than this file, never less.

## Start the application and the browser

```bash
./scripts/up.sh
```

Starts Meridian Claims Desk on `http://127.0.0.1:8099`, a second tenant's instance of the same
product on `http://127.0.0.1:8098`, and a Chromium exposing CDP on 9222, skipping any already up. Both outlive individual commands, which is what lets automation and the
operator console share one session.

Reset the seeded data whenever you want:

```bash
curl -X POST http://127.0.0.1:8099/admin/reset
```

---

## Commands

### Discovery

With a real model:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m sableau.cli discover \
  --job jobs/approve_claim.json \
  --planner anthropic \
  --param claim_id=CLM-004211 \
  --param outcome=APPROVED \
  --param "note=Within plan limits, provider in network, no duplicate found."
```

Without one:

```bash
python -m sableau.cli discover --job jobs/approve_claim.json --planner heuristic \
  --param claim_id=CLM-004211 --param outcome=APPROVED \
  --param "note=Within plan limits, provider in network, no duplicate found."
```

`--planner heuristic` is a rule based planner that reads the same live screen objects the model
reads. I wrote it so the loop, the compiler and the tests stay runnable with no API credential, and
so CI does not need one. Every artifact records `provenance.planner`, so an offline run can never be
mistaken for a model driven one. **The evidence committed to this repo was produced with
`--planner anthropic` on `claude-sonnet-4-6`** — see `evidence/01_discovery.txt` and
`provenance` in the compiled capability.

Both write `capabilities/meridian.record_claim_decision.v1.0.0.json` and a full trace under
`evidence/runs/`.

### Inspect an artifact

```bash
python -m sableau.cli validate --capability capabilities/meridian.record_claim_decision.v1.0.0.json
python -m sableau.cli schema --out capabilities/capability.schema.json
```

### Deterministic replay

This is the part that matters. New parameters, no model:

```bash
python -m sableau.cli replay \
  --capability capabilities/meridian.record_claim_decision.v1.0.0.json \
  --confirm-risky \
  --param claim_id=CLM-004212 \
  --param outcome=APPROVED \
  --param "note=Imaging authorised under referral 88213, within schedule."
```

```
SUCCESS/NONE | outputs=confirmation_code=MCD-77201, decided_amount=612.5 | llm_calls=0
```

`--confirm-risky` is required because the capability declares its save step risky. Leave it off and
the policy layer refuses before anything is written.

### Cross-tenant reuse

`scripts/up.sh` also starts a second instance of the same claims product on port 8098, branded and
versioned as another institution would run it: Riverbend Credit Union, on an older build, with no
test ids on the search screen, "Find" instead of "Search", "Record decision" instead of "Save
decision", different test ids on the decision panel and receipt, and an iframe named
`decisionPanel` rather than `decision`.

The base capability runs against it with no re-recording, specialised only by an overlay:

```bash
python -m sableau.cli replay \\
  --capability capabilities/meridian.record_claim_decision.v1.0.0.json \\
  --overlay capabilities/overlays/riverbend.json --confirm-risky \\
  --param claim_id=CLM-004212 --param outcome=APPROVED \\
  --param "note=Imaging authorised under referral 88213, within schedule."
```

```
SUCCESS/NONE | outputs=confirmation_code=MCD-77201, decided_amount=612.5 |
              drift=0.33 (6 degraded) | llm_calls=0
  drift: 3/9 controls found by their preferred locator
    s1_type_the_claim_id_into_the: fell back to candidate 1 (css)
    s2_submit_the_search_to_find:  fell back to candidate 1 (role)
    ...
```

The drift line is the point: the run succeeded, and it also named the six controls this tenant has
moved. See [REPORT §4](REPORT.md#4-heterogeneity-and-multi-tenant-design).

### Error and outcome demonstrations

```bash
./scripts/demo_errors.sh
```

Ten conditions, each classified rather than raised: invalid input, enum violation, record not found,
already processed, application validation error, permission denied, a transient backend failure
absorbed by a bounded restart, a slow page waited out, session expiry, and a policy refusal.

### Human handoff

```bash
python scripts/demo_handoff.py
```

Claim `CLM-004214` has a compliance notice covering the decision panel. Automation pauses, an
operator clears it on the same live page, automation finishes the job.

To drive it yourself:

```bash
python -m sableau.cli handoff \
  --capability capabilities/meridian.record_claim_decision.v1.0.0.json --confirm-risky \
  --param claim_id=CLM-004214 --param outcome=APPROVED \
  --param "note=Network review bulletin read, provider remains in network." \
  --hold 300
```

then open **http://127.0.0.1:8777**, press *Take control*, clear the notice, press *Resume*.

### Tests

```bash
python -m pytest tests/unit tests/test_no_llm_in_replay.py -v   # 72 tests, no browser needed
./scripts/up.sh && python -m pytest tests/integration -v        # 11 tests, real Chromium
python -m pytest -q                                             # all 83
```

### Rebuild all evidence

```bash
./scripts/make_evidence.sh
```

Wipes `evidence/` and regenerates it from real runs, index included. Nothing in that directory is
hand written.

---

## Layout

```
src/sableau/
  schema/      capability artifact, outcome taxonomy, result contract
  surface/     Surface protocol; Playwright DOM and in-memory implementations
  kernel/      control state machine, policy, redaction, observability
  discovery/   planners, observe→decide→act loop, compiler        (LLM lives here, only here)
  replay/      deterministic engine and bindings                  (no LLM, enforced)
  operator/    handoff console
targetapp/     Meridian Claims Desk
jobs/          discovery job specs: the contract declared up front
capabilities/  compiled artifacts, tenant overlays, outcome catalogues, exported JSON Schema
docs/          project site published to GitHub Pages
evidence/      real run output
```

---

## Honest limits

I would rather state these than have someone find them.

- **Discovery is one shot, and the planner needed guardrails to get there.** Building the loop
  against a real model surfaced three problems I had to fix: a `<select>` reported its option list
  rather than its selected value, so an already-set dropdown looked untouched and the model kept
  re-setting it; reads leave the screen unchanged, so the planner had no signal that a capture
  landed; and nothing detected a planner repeating itself. The loop now reports field state,
  feeds captured values back, and stops a repeating planner after four identical actions. Those
  are guardrails around a model, not a substitute for one.
- **One surface is implemented.** The abstraction is real (feature declaration, compatibility
  refusal, a second in-memory implementation the whole engine is tested against), but accessibility
  tree, screenshot-plus-coordinates and native desktop surfaces are designed for, not written.
- **Discovery is single-shot.** A failed exploration is not retried with a revised strategy, and the
  compiler refuses to emit an artifact from an unsuccessful run.
- **Session expiry is classified, not repaired.** The engine returns `RECOVERABLE/SESSION_EXPIRED`
  and leaves re-authenticating to the caller.

[REPORT.md §7](REPORT.md#7-cuts-limitations-and-next-steps) covers what I cut deliberately and what I
would build next.

---

## Publishing the project site

`docs/` is a static site with no build step. To publish it:

1. Push the repo to GitHub.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. The workflow in `.github/workflows/pages.yml` deploys `docs/` on every push to `main` that
   touches it. It lands at `https://Lokesh1566.github.io/sableau/`.

Then replace `Lokesh1566` in `README.md` and `docs/index.html`:

```bash
grep -rl Lokesh1566 README.md docs/index.html | xargs sed -i 's/Lokesh1566/your-github-handle/g'
```

If you would rather skip Actions entirely, **Settings → Pages → Source: Deploy from a branch →
`main` / `/docs`** works too; `docs/.nojekyll` is already there so the site is served as-is.

`.github/workflows/tests.yml` runs the unit suite on every push, and runs discovery plus the
integration suite against a headless Chromium. Neither workflow needs an API key.

---

## Licence

MIT. See [LICENSE](LICENSE).
