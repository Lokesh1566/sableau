# Sableau 

Sableau turns one successful computer-use discovery into a typed, versioned capability that replays without an LLM. This submission is adapted end to end to the live **MERIDIAN CORE** legacy banking application at `https://web-sample.interface-hiring.com`.

The production path is deliberately simple:

```text
goal + live UI -> LLM observe/decide/act -> capability JSON
capability JSON + new inputs -> deterministic browser replay -> structured result
```

The production catalog contains exactly seven MERIDIAN capabilities, a capability API, a thin banking chatbot, a run/evidence dashboard, explicit runtime outcomes, risky-step confirmation, redaction, and pause/escalation support. [REPORT.md](REPORT.md) explains the design; [evidence/README.md](evidence/README.md) indexes representative real runs.

## Quick start from the ZIP

Python 3.11 or newer is required.

```bash
unzip sableau2-final-submission.zip
cd sableau2
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ".[dev]"
python -m playwright install chromium
```

Validate the seven shipped artifacts and run the browser-free suite:

```bash
for cap in capabilities/meridian_core.*.v1.0.0.json; do
  python -m sableau.cli validate --capability "$cap"
done
python -m pytest -q
```

Start the API and dashboard:

```bash
python -m sableau.cli serve
```

The `serve` command selects `policy-core.json` automatically. Set
`SABLEAU_POLICY` only when intentionally running a different deployment policy.

Open:

- Dashboard: `http://127.0.0.1:8800`
- OpenAPI docs: `http://127.0.0.1:8800/docs`

The console opens on the balance-inquiry capability and lists only the seven live banking workflows. The first dashboard invocation automatically opens a **visible** shared Chromium window on the dashboard-specific CDP port `9334`, while the dashboard streams each step, timing, result, and escalation status. Set `SABLEAU_HEADLESS=1` for an unattended run. If a browser already exposes CDP, set `SABLEAU_CDP_URL`, for example `http://127.0.0.1:9222`.

### Dashboard-first demo (no replay commands)

After `python -m sableau.cli serve`, every required workflow can be run from
`http://127.0.0.1:8800`:

1. Select any of the seven capability forms, enter the public demo password
   `password`, and click **Invoke**. High-risk forms require the confirmation
   checkbox.
2. Or click a chat example for sign-on, member lookup, balance, transfer, open
   share, member update, or account hold, then press **Send**. High-risk chat
   examples include the required word `confirm`.
3. Watch the controlled MERIDIAN browser and the dashboard's **Live processing**
   panel together. The panel reports queued/running/final state, every step and
   its duration, structured output, and a prominent escalation indicator.
4. Click the completed row under **Recent runs** to inspect redacted inputs,
   outputs, step reports, logs, screenshots, DOM snapshots, and other evidence.

## Public demo access and configuration

MERIDIAN CORE uses synthetic data and public demo operators:

| Operator | Password | Role |
|---|---|---|
| `teller1` | `password` | teller |
| `super1` | `password` | supervisor |

The chatbot uses those public defaults. They may be overridden without changing an artifact:

```bash
export SABLEAU_OPERATOR=teller1
export SABLEAU_SUPERVISOR_OPERATOR=super1
export SABLEAU_OPERATOR_PASSWORD=password
export SABLEAU_BRANCH=MAIN-001
export SABLEAU_POLICY=policy-core.json
```

`ANTHROPIC_API_KEY` is needed only to make a new model-driven discovery. It is never read during replay, and no `.env` file or key is included in the deliverable.

```bash
cp .env.example .env
# edit .env, then:
set -a; source .env; set +a
```

## Required MERIDIAN functions

Every row has a job specification under `jobs/`, a compiled artifact under `capabilities/`, and live discovery/replay evidence under `evidence/runs/`.

| Function | Capability ID | Main outputs | Risk |
|---|---|---|---|
| Sign on / session establishment | `meridian_core.sign_on` | session status | low |
| Member inquiry by number or last name | `meridian_core.find_member` | member number, member name | low |
| Balance inquiry | `meridian_core.check_member_balance` | member, share ID/type/balance/status | low |
| Transfer review and post | `meridian_core.transfer_funds` | confirmation, posted amount | high |
| Open share review and post | `meridian_core.open_new_share` | new share ID, confirmation | high |
| Update member information | `meridian_core.update_member_information` | confirmation message | high |
| Place account hold review and post | `meridian_core.place_account_hold` | confirmation, hold status | high; supervisor-only |

The review/post forms contain a per-transaction hidden `_token`. Discovery records neither its value nor a locator action for it. Replay submits the current live form, so the browser supplies the fresh token naturally.

## Exact demo path

### 1. Real LLM discovery

Set `ANTHROPIC_API_KEY`, then record the mandatory balance workflow:

```bash
export SABLEAU_POLICY=policy-core.json
export SABLEAU_PLANNER=anthropic
bash core/record.sh balance
```

That command drives the real website, writes `capabilities/meridian_core.check_member_balance.v1.0.0.json`, and saves its trace, screenshot, log, and compiled artifact under `evidence/runs/discovery_*`.

The repository already contains genuine Anthropic discoveries for sign-on, member inquiry, balance inquiry, transfer, and open-share. When a model key is unavailable, the same live observation, policy, probing, compiler, and evidence path can be exercised with the explicitly labeled offline planner:

```bash
export SABLEAU_PLANNER=heuristic
bash core/record.sh update
```

Artifacts always record `provenance.planner` and the model name, so heuristic evidence cannot be mistaken for LLM evidence.

### 2. Deterministic replay with new inputs

```bash
export SABLEAU_POLICY=policy-core.json
python -m sableau.cli replay \
  --capability capabilities/meridian_core.check_member_balance.v1.0.0.json \
  --param operator=teller1 \
  --param password=password \
  --param branch=MAIN-001 \
  --param member_number=102777
```

Representative result:

```text
SUCCESS/NONE | outputs=member_name=Johnson, Katherine,
share_id=102777-S0001, share_type=Regular Shares,
share_balance=$42,000.00, share_status=HOLD [HOLD] | llm_calls=0
```

Replay has no planner dependency. The result contract itself reports `llm_calls=0`, and `tests/test_no_llm_in_replay.py` enforces that dependency boundary.

### 3. Exceptional path and escalation

Run the bundled demonstrations:

```bash
bash core/demo_errors.sh
```

They cover invalid input, a nonexistent member as a typed business outcome, and a teller attempting the supervisor-only hold flow. The teller run stops after the application refuses the review, captures a screenshot and DOM evidence, transitions control to `PAUSED`, and emits an escalation containing the capability, step, URL, reason, and evidence reference.

The shipped exceptional run is `evidence/runs/replay_20260820T202123Z_ed01a2`:

```text
HARD_FAILURE/PERMISSION_DENIED
control: AUTOMATION_RUNNING -> PAUSED
escalated: true
llm_calls=0
```

The original local claims fixture remains under `targetapp/` as a reproducible test harness for the full take-control/resume console. Its artifact is isolated under `tests/fixtures/`, so it is not exposed by the production capability catalog. Run `./scripts/up.sh` and `python scripts/demo_handoff.py` to see a scripted operator take the same live browser session, perform the blocked action, and hand control back. It is auxiliary to the live MERIDIAN adaptation, not the submitted target.

## API, chatbot, and dashboard

The catalog is projected directly from artifacts; there is no hand-maintained second contract.

```bash
curl -s http://127.0.0.1:8800/api/capabilities | python -m json.tool
```

Invoke balance inquiry through the agent-facing API:

```bash
curl -X POST \
  http://127.0.0.1:8800/api/capabilities/meridian_core.check_member_balance/invoke \
  -H 'Content-Type: application/json' \
  -d '{"params":{"operator":"teller1","password":"password","branch":"MAIN-001","member_number":"101555"}}'
```

Invoke the thin chatbot:

```bash
curl -X POST http://127.0.0.1:8800/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"check balance for member 101555"}'
```

The parser also maps sign-on, member lookup, transfer, open-share, update, and hold requests. High-risk chat requests are not run unless the message includes the word `confirm`. The dashboard likewise requires a confirmation checkbox. Direct API bodies default `confirm_risky` to `false`.

Useful endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/capabilities` | typed capability catalog |
| `GET /api/capabilities/{id}` | one input/output contract |
| `POST /api/capabilities/{id}/invoke` | deterministic execution |
| `POST /api/capabilities/{id}/start` | start a dashboard run immediately and return its run ID |
| `GET /api/live-runs/{run_id}` | redacted live status, step events, timings, and final result |
| `POST /api/chat` | thin natural-language front door |
| `POST /api/chat/start` | parse chat and start a watchable replay |
| `GET /api/runs` | discovery and replay history |
| `GET /api/runs/{run_id}` | redacted inputs, outputs, log, and evidence index |
| `GET /api/runs/{run_id}/evidence/{path}` | one contained evidence file |
| `GET /api/health` | liveness and artifact count |

Expected business answers and execution failures both return the engine's typed body. HTTP status codes are reserved for malformed requests or missing API resources.

## Runtime outcome model

The MERIDIAN outcome catalog distinguishes:

- `SUCCESS`: checkpoint verified and declared outputs extracted.
- `BUSINESS_OUTCOME`: for example `RECORD_NOT_FOUND`, `INSUFFICIENT_FUNDS`, or a share already on hold.
- `RECOVERABLE`: session expiry or a known maintenance/transient condition with bounded recovery guidance.
- `HARD_FAILURE`: validation, permission, application failure, policy refusal, or exhausted retries.

Eleven target-specific detectors cover bad sign-on, session timeout, transaction validation, injected validation/not-found faults, insufficient funds, account hold, maintenance, application error, and the exact teller authorization denial. The supervisor warning banner is intentionally not treated as denial; a supervisor can continue through it.

## Safety and data handling

- `policy-core.json` allowlists the live host and action vocabulary.
- Artifacts may only narrow deployment policy.
- High-risk steps require explicit CLI/API/dashboard/chat confirmation.
- Credentials, notes, contact details, and other declared sensitive inputs are redacted from logs, API echoes, evidence, and artifact examples.
- The per-form `_token` is represented only as `[opaque]` in observations and is never persisted.
- Locator ambiguity fails closed; bounded retries never guess a different control.
- Escalation preserves the same CDP browser session and records ownership transitions and human actions.

All bundled data is synthetic public-demo data. Do not point this demo policy at a real institution.

## Testing

```bash
python -m pytest -q
```

Current clean result: `90 passed, 13 skipped`. The skipped tests require live services and are opt-in:

```bash
RUN_LIVE_MERIDIAN_TESTS=1 python -m pytest tests/integration/test_api.py -q
RUN_LIVE_LEGACY_TESTS=1 ./scripts/up.sh
RUN_LIVE_LEGACY_TESTS=1 python -m pytest tests/integration/test_live_stack.py -q
```

The committed MERIDIAN evidence was produced against the public live UI, including real LLM discoveries, parameter-varied deterministic replays, successful writes, a natural permission failure, screenshot capture, and escalation.

## Repository map

```text
src/sableau/discovery/       observe/decide/act planners and compiler
src/sableau/replay/          deterministic engine and outcome handling
src/sableau/schema/          capability and result contracts
src/sableau/surface/         surface protocol and Playwright DOM adapter
src/sableau/kernel/          policy, redaction, evidence, control ownership
src/sableau/api/             catalog, invoke API, chat, dashboard
jobs/core_*.json             seven MERIDIAN discovery specifications
capabilities/meridian_core.* seven compiled capabilities
capabilities/outcomes/       target runtime detectors
core/record.sh               repeatable live discovery commands
evidence/runs/               discovery, replay, failure, and escalation evidence
targetapp/                   optional original local claims test fixture
```

MERIDIAN CORE is stateful and may reset on redeploy. Use small demo amounts and inspect the current share list before repeating state-changing examples.
