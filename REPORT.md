# Design report

## Architecture

Sableau has two intentionally separate execution paths. Discovery accepts a natural-language goal and a typed job contract, then an LLM observes the current UI, emits one structured action, and repeats until success or a stop condition. At the moment an action succeeds, the DOM adapter describes the actual control and probes every plausible locator against the live page. The compiler turns the successful trace into a capability artifact. Production replay loads that artifact, binds new inputs, and executes its steps without importing or calling a planner.

Both paths share the kernel: deployment policy, redaction, structured evidence, outcome classification, and the automation/human ownership state machine. They also share a surface protocol (`navigate`, `resolve`, `act`, `evaluate`, `observe`, `evidence`). The implemented adapter is Playwright over a long-lived CDP browser. Keeping the browser outside a single run preserves cookies, page state, and the exact tab across escalation.

Adapting the original claims demonstration to live MERIDIAN CORE did not require a replay rewrite. The meaningful adapter changes were support for table-layout legacy controls, `name`-attribute targeting, form submission for old anchors/buttons whose ordinary click does not navigate, leaf table cells as readable controls, real `<option value>` capture, and safe representation of hidden inputs. Seven target job specifications and eleven target outcomes were then compiled through the existing schema and engine.

The agent-facing layer is deliberately thin. FastAPI projects a catalog directly from artifacts and invokes the same replay engine as the CLI. A keyword banking chatbot maps plain text to typed calls. Synchronous endpoints preserve the simple agent contract, while watchable endpoints return a run ID immediately so the dashboard can poll redacted step-start/step-complete events. The dashboard renders all seven forms, chat examples, live progress and timings, structured output, explicit escalation state, discovery/replay history, logs, and evidence files. Invocation is serialized because this demo has one browser. A production version would replace that lock with a browser/session pool keyed by institution and operator session.

## Artifact schema

Each JSON artifact is a reviewable capability contract, not a transcript. It includes a schema and capability version, title and purpose, discovery provenance, surface requirements and entry URL, safety envelope, typed inputs and outputs, ordered steps, checkpoints, known runtime outcomes, and an escalation contract.

Each step separates intent from mechanism. Its action is a discriminated type (`click`, `type`, `select`, `read`, and so on), while its target contains ranked locator candidates. Semantic role/name or stable `name` attributes rank above measured CSS fallbacks. Every recorded candidate was proven unique on the live page; replay fails on ambiguity. Preconditions and postconditions reference explicit checkpoints, and output definitions point to the read step and binding that produce them.

Parameterization is compiler-owned. Discovery examples such as member `101555`, branch `WEST-014`, share IDs, amounts, and contact values become `{{input.*}}` bindings only where the successful interaction actually used them. Compilation refuses a required input that never appears in an action, locator, or checkpoint; this caught default dropdown examples during adaptation. Structural identifiers such as `name=password` are never parameterized, while visible record links may be.

The MERIDIAN artifacts cover sign-on, member search, balance, transfer, open share, update member, and account hold. Transfer/open/hold include the review and final post screens and extract real confirmation references. The hidden `_token` is never a parameter or persisted value: observations show only `[opaque]`, and the live form carries its fresh token when submitted.

## Determinism & error handling

Replay contains no planner reference and reports `llm_calls=0`. It validates inputs before executing steps, checks the deployment policy against the artifact policy, resolves candidates in fixed rank order, applies bounded retries only where declared, evaluates checkpoints after screen changes, and extracts only declared outputs. A changed member, branch, share type, address, or transfer destination therefore changes bound values, not control flow.

Results have four categories. `SUCCESS` includes outputs and drift statistics. `BUSINESS_OUTCOME` is a legitimate domain answer such as record not found, insufficient funds, or an account already on hold. `RECOVERABLE` identifies a known state for which retry/re-authentication is safe, such as session expiry or maintenance. `HARD_FAILURE` stops on validation, permission, policy, application, checkpoint, or exhausted-retry failure and identifies the step and evidence.

MERIDIAN detectors cover rejected sign-on, timeout, transaction rejection, injected validation and not-found faults, natural not-found, insufficient funds, an on-hold source share, maintenance, application error, and supervisor denial. Detector specificity matters: supervisors also see a “restricted function” warning while being allowed to continue. The final detector keys on the teller-only authorization sentence, preventing that warning from becoming a false permission failure.

The live evidence demonstrates parameter-varied zero-LLM replays for balance, transfer, open share, update, and supervisor hold. It also demonstrates a teller denial after the hold form is filled: replay classifies `HARD_FAILURE/PERMISSION_DENIED`, captures screen evidence, pauses, and escalates. The stateful public target may reset or accumulate demo transactions, so evidence records concrete outputs but artifacts avoid over-specific confirmation numbers or member names in their checkpoints.

## Heterogeneity & multi-tenant

The capability depends on required surface features rather than Playwright classes. A desktop adapter can implement the same protocol with an accessibility tree, OCR/coordinates, and OS input while preserving actions, checkpoints, outcomes, results, policy, and evidence. A screenshot-only surface would add image-anchor candidates; the artifact can reject replay on a surface that lacks a declared feature instead of degrading silently.

Legacy web is already exercised here: server-rendered tables, sparse semantics, form-bound tokens, and controls best identified by `name`. Candidate chains allow semantic locators where present and measured structural fallbacks where they are not. Replay records which candidate succeeded, producing a drift score without a separate crawl.

For many institutions running the same vendor product, I would keep a vendor/version base capability and apply reviewed tenant overlays for entry URLs, allowed hosts, locator candidate additions, detector wording, and feature flags. Overlays may specialize but never broaden safety. Successful fallback use provides per-step drift telemetry; repeated degradation can trigger revalidation before failure. The repository retains the original two-tenant claims fixture as an implemented example of this overlay seam, while MERIDIAN is the production live target.

## Escalation & handoff

The control state machine distinguishes `AUTOMATION_RUNNING`, `PAUSED`, `HUMAN_IN_CONTROL`, `RESUMING`, and `DONE`, with an explicit owner and recorded transition actor. A terminal outcome with `recovery=escalate`, a policy refusal, exhausted recovery, or a discovery dead end creates an intervention containing capability/goal context, current step, reason code, live URL, screenshot reference, and timestamp. Automation releases ownership before waiting.

The operator console attaches to the same CDP browser and calls the same surface/policy path for manual actions. Taking control changes the owner; actions are appended to the escalation; resume records the human decision and hands the existing page back to replay. The included local harness demonstrates pause, scripted operator action, and completion on the same session. The live MERIDIAN teller-hold evidence demonstrates the same detection, context capture, and paused escalation seam through the new artifact/API path.

The dashboard does not bypass this model. It shows escalation status and evidence from the engine result. High-risk calls require explicit confirmation, and an authorization failure remains a typed failure even when invoked through chat or HTTP.

## Safety

Deployment policy allowlists hostnames and action types. Artifact policy can only narrow it. Locator ambiguity fails closed, step counts are bounded, discovery repetition is detected, and dangerous intent words mark steps risky. CLI writes require `--confirm-risky`; API bodies default confirmation to false; the dashboard has a confirmation checkbox; and chat requires the literal confirmation word before invoking high-risk capabilities.

Inputs carry sensitivity. Passwords, memos/notes, e-mail, phone, and address are redacted in structured logs, artifacts, API/chat echoes, and dashboard evidence. Secret artifact examples are replaced rather than globally rewriting schema identifiers. Per-transaction tokens are never observed in clear text. Evidence endpoints resolve paths within one run directory to prevent traversal.

These controls are suitable for a synthetic public demo, not an authorization system for real banking. A production deployment still needs identity-bound approvals, per-institution policy, secrets management, encrypted evidence, retention limits, dual control for irreversible work, and an independent audit trail.

## Cuts

I kept one browser and filesystem evidence rather than building a queue, database, distributed scheduler, or browser fleet. The chatbot uses a transparent grammar instead of another model because capability selection was the point, not conversational coverage. The operator console is minimal, not co-browsing. Desktop and screenshot adapters remain designed seams. There is no automatic compensating transaction for a successful write, and the demo does not reset the remote app.

The supplied Anthropic balance was exhausted late in adaptation. That did not remove the required proof: the repository contains multiple genuine Anthropic live discoveries with model provenance and full traces, including mandatory balance and transfer. The remaining update/hold recordings used the explicitly labeled heuristic fallback over the same live surface, policy, locator probing, compiler, and replay engine. This distinction is visible in every artifact and evidence run.

Next I would add isolated browser workers, identity-bound approval records, a credential vault, encrypted/retained evidence storage, and a validation pipeline that replays read-only canaries per tenant/version before promotion. I would then add accessibility/desktop and screenshot-coordinate surfaces, followed by artifact signing and a reviewed capability registry.
