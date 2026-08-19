"""Sableau command line.

Five verbs:

  discover   run the LLM loop against the live app and compile a capability
  replay     execute a capability deterministically with new parameters
  handoff    replay a capability that is expected to need a person, with the
             operator console attached to the same live session
  validate   load and check a capability without running it
  schema     print the JSON Schema for the capability artifact
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .browser import cdp_url, open_surface
from .discovery import CompilationError, DiscoveryLoop, compile_capability, load_job, make_planner
from .kernel import Policy, RunRecorder, new_run_id
from .kernel.control import SessionControl
from .kernel.redaction import redactor_for
from .replay import ReplayEngine
from .schema import Capability

EVIDENCE_ROOT = os.environ.get("SABLEAU_EVIDENCE", "evidence/runs")


def _load_dotenv() -> None:
    path = Path(".env")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _params(pairs: list[str] | None, blob: str | None) -> dict:
    out: dict = {}
    if blob:
        out.update(json.loads(blob))
    for pair in pairs or []:
        k, _, v = pair.partition("=")
        out[k.strip()] = v
    return out


# ----------------------------------------------------------------- discover


async def cmd_discover(args) -> int:
    job = load_job(args.job)
    params = _params(args.param, args.params)
    if not params:
        params = {i["name"]: i.get("example", i.get("default", "")) for i in job["inputs"]}

    run_id = new_run_id("discovery")
    recorder = RunRecorder(run_id, root=EVIDENCE_ROOT)
    recorder.redactor.paths.update(job.get("redact_paths", []))
    for spec in job["inputs"]:
        if spec.get("sensitivity") == "secret" and spec["name"] in params:
            recorder.redactor.add_secret(params[spec["name"]])

    policy = Policy.load(args.policy)
    planner = make_planner(args.planner, args.model)
    surface = await open_surface()
    print(f"discovery run {run_id} :: planner={planner.name} browser={cdp_url()}", file=sys.stderr)

    try:
        await surface.navigate(job["entry_url"])
        loop = DiscoveryLoop(surface, planner, recorder, policy, max_turns=args.max_turns)
        trace = await loop.run(job, params)
    finally:
        await surface.close()

    if trace.status != "success":
        print(f"\ndiscovery did not succeed: {trace.status} :: {trace.summary}")
        print(f"trace: {recorder.dir / 'trace.json'}")
        return 2

    job["trace_ref"] = str(recorder.dir / "trace.json")
    try:
        cap = compile_capability(trace, job, policy)
    except CompilationError as exc:
        print(f"\ncompilation refused: {exc}")
        return 3

    out = Path(args.out or f"capabilities/{cap.capability_id}.v{cap.version}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    red = redactor_for(cap, params)
    out.write_text(json.dumps(red.value(cap.model_dump(mode="json")), indent=2))
    recorder.write_json("capability.json", red.value(cap.model_dump(mode="json")))

    print(f"\ncapability written: {out}")
    print(f"  steps={len(cap.steps)} checkpoints={len(cap.checkpoints)} "
          f"outputs={len(cap.outputs)} known_outcomes={len(cap.known_outcomes)}")
    print(f"  evidence: {recorder.dir}")
    return 0


# ------------------------------------------------------------------- replay


async def cmd_replay(args) -> int:
    cap = Capability.model_validate_json(Path(args.capability).read_text())
    params = _params(args.param, args.params)
    run_id = new_run_id("replay")
    recorder = RunRecorder(run_id, root=EVIDENCE_ROOT)
    policy = Policy.load(args.policy)
    surface = await open_surface()
    print(f"replay run {run_id} :: {cap.ref}", file=sys.stderr)
    try:
        if cap.surface.entry_url:
            await surface.navigate(cap.surface.entry_url)
        engine = ReplayEngine(surface, recorder, policy, confirm_risky=args.confirm_risky,
                              escalation_timeout_s=args.escalation_timeout)
        result = await engine.run(cap, params)
    finally:
        await surface.close()

    print("\n" + result.summary())
    print(f"  evidence: {recorder.dir}")
    return 0 if result.ok else (0 if args.tolerate else 1)


# ------------------------------------------------------------------ handoff


async def cmd_handoff(args) -> int:
    import uvicorn

    cap = Capability.model_validate_json(Path(args.capability).read_text())
    params = _params(args.param, args.params)
    run_id = new_run_id("handoff")
    recorder = RunRecorder(run_id, root=EVIDENCE_ROOT)
    policy = Policy.load(args.policy)
    surface = await open_surface()
    control = SessionControl(run_id, on_event=recorder.event_sink())

    from .operator.app import build_console

    console = build_console(control, surface, recorder)
    config = uvicorn.Config(console, host="127.0.0.1", port=args.console_port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.6)
    print(f"operator console: http://127.0.0.1:{args.console_port}", file=sys.stderr)

    try:
        if cap.surface.entry_url:
            await surface.navigate(cap.surface.entry_url)
        engine = ReplayEngine(surface, recorder, policy, control=control,
                              confirm_risky=args.confirm_risky,
                              escalation_timeout_s=args.escalation_timeout)
        result = await engine.run(cap, params)
    finally:
        recorder.write_json("control.json", control.snapshot())
        if args.hold:
            print(f"holding console open for {args.hold}s", file=sys.stderr)
            await asyncio.sleep(args.hold)
        server.should_exit = True
        await server_task
        await surface.close()

    print("\n" + result.summary())
    print(f"  control: {control.state.value} escalations={len(control.escalations)} "
          f"human_actions={control.human_action_count}")
    print(f"  evidence: {recorder.dir}")
    return 0 if result.ok else 1


# ------------------------------------------------------- validate and schema


def cmd_validate(args) -> int:
    cap = Capability.model_validate_json(Path(args.capability).read_text())
    print(f"{cap.ref} is valid")
    print(f"  title       {cap.title}")
    print(f"  surface     {cap.surface.kind} requires {cap.surface.required_features}")
    print(f"  inputs      {[i.name for i in cap.inputs]}")
    print(f"  outputs     {[o.name for o in cap.outputs]}")
    print(f"  steps       {len(cap.steps)} ({sum(1 for s in cap.steps if s.risk == 'risky')} risky)")
    print(f"  checkpoints {[c.id for c in cap.checkpoints]}")
    print(f"  outcomes    {[o.id for o in cap.known_outcomes]}")
    print(f"  hosts       {cap.safety.allowed_hosts}")
    return 0


def cmd_schema(args) -> int:
    out = json.dumps(Capability.model_json_schema(), indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out)
        print(f"wrote {args.out}")
    else:
        print(out)
    return 0


# --------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sableau", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    common_params = lambda sp: (  # noqa: E731
        sp.add_argument("--param", action="append", metavar="k=v"),
        sp.add_argument("--params", metavar="JSON"),
        sp.add_argument("--policy", default=None),
    )

    d = sub.add_parser("discover", help="run the LLM loop and compile a capability")
    d.add_argument("--job", required=True)
    d.add_argument("--planner", default="anthropic", choices=["anthropic", "heuristic"])
    d.add_argument("--model", default=None)
    d.add_argument("--max-turns", type=int, default=25)
    d.add_argument("--out", default=None)
    common_params(d)
    d.set_defaults(func=cmd_discover, is_async=True)

    r = sub.add_parser("replay", help="execute a capability with no LLM in the loop")
    r.add_argument("--capability", required=True)
    r.add_argument("--confirm-risky", action="store_true")
    r.add_argument("--tolerate", action="store_true",
                   help="exit 0 even on a non-success outcome, for demos")
    r.add_argument("--escalation-timeout", type=float, default=20.0)
    common_params(r)
    r.set_defaults(func=cmd_replay, is_async=True)

    h = sub.add_parser("handoff", help="replay with the operator console attached")
    h.add_argument("--capability", required=True)
    h.add_argument("--console-port", type=int, default=8777)
    h.add_argument("--confirm-risky", action="store_true")
    h.add_argument("--escalation-timeout", type=float, default=600.0)
    h.add_argument("--hold", type=float, default=0.0)
    common_params(h)
    h.set_defaults(func=cmd_handoff, is_async=True)

    v = sub.add_parser("validate", help="load and check a capability")
    v.add_argument("--capability", required=True)
    v.set_defaults(func=cmd_validate, is_async=False)

    s = sub.add_parser("schema", help="print the capability JSON Schema")
    s.add_argument("--out", default=None)
    s.set_defaults(func=cmd_schema, is_async=False)
    return p


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    if args.is_async:
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
