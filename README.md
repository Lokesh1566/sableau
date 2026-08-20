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

**[Project site](https://YOUR-USERNAME.github.io/sableau/)** · **[Design report](REPORT.md)** ·
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
git clone https://github.com/YOUR-USERNAME/sableau.git && cd sableau
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
product on `http://127.0.0.1:8098`, and a Chromium exposing CDP on 9222, skipping any that are
already up. Both outlive individual commands, which is what lets automation and the
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
`heuristic`** — see [Honest limits](#honest-limits).

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

### The capability API and dashboard

The CLI was always a thin wrapper around one idea: capability plus parameters in, structured result
out. The same idea over HTTP is what an AI agent actually calls.

```bash
python -m sableau.cli serve
```

- **Dashboard** — http://127.0.0.1:8800
- **API docs** — http://127.0.0.1:8800/docs (generated from the same models)

**Open the dashboard in your normal browser, not the automation one.** Invoking drives the
automation browser, so if the dashboard were in that window it would navigate away mid-run.

#### The catalogue an agent discovers

```bash
curl -s http://127.0.0.1:8800/api/capabilities | python3 -m json.tool
```

```json
[{
  "capability_id": "meridian.record_claim_decision",
  "title": "Record a decision on a pending claim",
  "risk_level": "high",
  "inputs": [
    { "name": "claim_id", "type": "string", "required": true, "pattern": "^CLM-[0-9]{6}$" },
    { "name": "outcome",  "type": "enum",   "required": true, "enum": ["APPROVED", "REJECTED"] },
    { "name": "note",     "type": "string", "required": true, "sensitivity": "medium" }
  ],
  "outputs": [
    { "name": "confirmation_code", "type": "string" },
    { "name": "decided_amount",    "type": "number" }
  ],
  "known_outcomes": ["search_no_match", "already_decided", "permission_denied", "..."],
  "tenants": ["riverbend"]
}]
```

**The catalogue is derived, never authored.** It projects the artifacts on disk, so there is no
second copy of the contract to drift out of sync, and a capability becomes callable the moment it is
compiled.

#### Invoking one

```bash
curl -X POST http://127.0.0.1:8800/api/capabilities/meridian.record_claim_decision/invoke \
  -H 'Content-Type: application/json' \
  -d '{"params":{"claim_id":"CLM-004211","outcome":"APPROVED","note":"Within plan limits."}}'
```

```json
{ "category": "SUCCESS", "code": "NONE",
  "outputs": { "confirmation_code": "MCD-77201", "decided_amount": 148.0 },
  "llm_calls": 0, "duration_ms": 5829 }
```

Add `"tenant": "riverbend"` to run it against another institution's instance.

**Status codes are used only for genuine HTTP problems** — unknown capability, malformed body. A
business outcome or a hard failure is a 200 with a typed body, because an HTTP error code cannot
carry the distinction between "no such claim" and "the service is broken", and the caller needs it.

| Endpoint | What it does |
|---|---|
| `GET /api/capabilities` | the catalogue |
| `GET /api/capabilities/{id}` | one contract |
| `POST /api/capabilities/{id}/invoke` | run it, returns `ReplayResult` |
| `GET /api/runs` | recent runs with outcome, drift and AI-call count |
| `GET /api/runs/{run_id}` | one run with its full structured log |
| `POST /api/chat` | thin natural-language front door |
| `GET /api/health` | liveness |

#### The chat endpoint

Deliberately thin. Intent parsing is keyword matching, not a model — a scope decision, not a design
one. The interesting surface is the typed API underneath, and in production interface.ai's own agent
sits in this position. What it demonstrates is the shape: text in, a typed capability call out, a
typed result back.

```bash
curl -X POST http://127.0.0.1:8800/api/chat -H 'Content-Type: application/json' \
  -d '{"message":"approve claim CLM-004211"}'
```

```json
{ "reply": "Done. Confirmation code MCD-77201 for 148.0.",
  "invoked": "meridian.record_claim_decision",
  "params": { "claim_id": "CLM-004211", "outcome": "APPROVED", "note": "..." } }
```

The four outcome categories map onto four different things you would say to a person, which is
exactly why they are separate.

### Cross-tenant reuse

`scripts/up.sh` also starts a second instance of the same claims product on port 8098, branded and
versioned as another institution would run it: different labels, no test ids on the search screen,
different test ids on the decision panel, and a differently named iframe. The base capability runs
against it with no re-recording, specialised by an overlay:

```bash
python -m sableau.cli replay \
  --capability capabilities/meridian.record_claim_decision.v1.0.0.json \
  --overlay capabilities/overlays/riverbend.json --confirm-risky \
  --param claim_id=CLM-004212 --param outcome=APPROVED \
  --param "note=Imaging authorised under referral 88213, within schedule."
```

```
SUCCESS/NONE | outputs=confirmation_code=MCD-77201, decided_amount=612.5 |
              drift=0.25 (6 degraded) | llm_calls=0
  drift: 2/8 controls found by their preferred locator
    s1_enter_the_claim_reference: fell back to candidate 1 (css)
    ...
```

The drift line is the point: the run succeeded, and it also told you which six controls this tenant
has moved. See [REPORT §4](REPORT.md#4-heterogeneity-and-multi-tenant-design).

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
python -m pytest tests/unit tests/test_no_llm_in_replay.py -v   # 69 tests, no browser needed
./scripts/up.sh && python -m pytest tests/integration -v        # 29 tests, real Chromium
python -m pytest -q                                             # all 80
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
  api/         capability API and dashboard
targetapp/     Meridian Claims Desk
jobs/          discovery job specs: the contract declared up front
capabilities/  compiled artifacts, tenant overlays, outcome catalogues, exported JSON Schema
docs/          project site published to GitHub Pages
evidence/      real run output
```

---

## Honest limits

I would rather state these than have someone find them.

- **The committed evidence used the heuristic planner.** `AnthropicPlanner` is complete tool-use code
  on the same interface; run the discovery command above with a key and it produces model-driven
  evidence. Everything else in `evidence/` — the UI interaction, the compilation, every replay, the
  handoff — is genuine execution against real Chromium.
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
   touches it. It lands at `https://YOUR-USERNAME.github.io/sableau/`.

Then replace `YOUR-USERNAME` in `README.md` and `docs/index.html`:

```bash
grep -rl YOUR-USERNAME README.md docs/index.html | xargs sed -i 's/YOUR-USERNAME/your-github-handle/g'
```

If you would rather skip Actions entirely, **Settings → Pages → Source: Deploy from a branch →
`main` / `/docs`** works too; `docs/.nojekyll` is already there so the site is served as-is.

`.github/workflows/tests.yml` runs the unit suite on every push, and runs discovery plus the
integration suite against a headless Chromium. Neither workflow needs an API key.

---

## Licence

MIT. See [LICENSE](LICENSE).
