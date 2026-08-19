"""The discovery loop.

One turn is: observe the live screen, ask the planner for a single structured
action, check it against policy, execute it, and record what actually happened.

The part that matters most is what gets recorded. At the exact moment an action
succeeds, the loop asks the surface to describe the element it acted on, builds
every locator it could plausibly use to find that element again, and *probes
each one against the live DOM* to see how many things it matches. Only locators
that resolved to exactly one element survive into the artifact.

That is why the planner never writes selectors: it says what it wants in human
terms, and the loop measures which machine-durable descriptions are actually
unambiguous on the real page.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from ..kernel.observability import RunRecorder
from ..kernel.policy import Policy
from ..schema import (
    ClickAction,
    NavigateAction,
    PressAction,
    ReadAction,
    SelectAction,
    TargetSpec,
    TypeAction,
)
from ..schema.errors import PolicyViolation, SableauError
from ..surface.base import Surface
from .planner import Planner, history_key
from .tools import Planned, hint_summary


@dataclass
class ProbedLocator:
    locator: dict[str, Any]
    match_count: int
    ok: bool
    note: str = ""


@dataclass
class TraceEntry:
    seq: int
    tool: str
    args: dict[str, Any]
    rationale: str
    url_before: str
    url_after: str = ""
    frame_path: list[str] = field(default_factory=list)
    descriptor: dict[str, Any] | None = None
    probes: list[ProbedLocator] = field(default_factory=list)
    read_value: Any = None
    status: str = "ok"
    error: str | None = None


@dataclass
class DiscoveryTrace:
    goal: str
    planner: str
    model: str | None
    entry_url: str
    params: dict[str, Any]
    entries: list[TraceEntry] = field(default_factory=list)
    finished: bool = False
    status: str = "incomplete"
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "planner": self.planner,
            "model": self.model,
            "entry_url": self.entry_url,
            "params": self.params,
            "status": self.status,
            "summary": self.summary,
            "entries": [asdict(e) for e in self.entries],
        }


class DiscoveryLoop:
    def __init__(
        self,
        surface: Surface,
        planner: Planner,
        recorder: RunRecorder,
        policy: Policy | None = None,
        max_turns: int = 25,
        repeat_warn: int = 2,
        repeat_abort: int = 4,
    ):
        self.surface = surface
        self.planner = planner
        self.recorder = recorder
        self.policy = policy or Policy.load()
        self.max_turns = max_turns
        self.repeat_warn = repeat_warn
        self.repeat_abort = repeat_abort

    async def run(self, job: dict[str, Any], params: dict[str, Any]) -> DiscoveryTrace:
        goal = job["goal"]
        trace = DiscoveryTrace(
            goal=goal,
            planner=self.planner.name,
            model=getattr(self.planner, "model", None),
            entry_url=job["entry_url"],
            params=params,
        )
        context = {"params": params, "outputs": job.get("outputs", []), "entry_url": job["entry_url"]}
        history: list[str] = []
        repeats: dict[str, int] = {}
        declared_outputs = [o["name"] for o in job.get("outputs", [])]
        captured: dict[str, Any] = {}
        context["outputs_remaining"] = list(declared_outputs)
        context["outputs_captured"] = captured

        self.recorder.log("discovery.start", goal=goal, planner=self.planner.name,
                          entry_url=job["entry_url"])

        for turn in range(1, self.max_turns + 1):
            obs = await self.surface.observe(with_screenshot=False)
            planned = await self.planner.decide(goal, obs, history, context)
            self.recorder.log(
                "discovery.decision",
                turn=turn,
                tool=planned.tool,
                intent=planned.intent,
                hints=hint_summary(planned.args),
                rationale=planned.rationale[:200] or None,
            )

            # A planner that keeps re-issuing an action which changes nothing is
            # stuck. Say so out loud rather than burning the turn budget in
            # silence: first nudge it, then stop.
            signature = _signature(planned, obs.url)
            repeats[signature] = repeats.get(signature, 0) + 1
            if repeats[signature] == self.repeat_warn:
                history.append(
                    f"NOTE: you have already done '{planned.intent}' "
                    f"{self.repeat_warn} times and the screen did not change. "
                    "Check the controls' current_value, then do the NEXT step or call finish."
                )
                self.recorder.log("discovery.repeat_warning", signature=signature,
                                  count=repeats[signature])
            if repeats[signature] >= self.repeat_abort:
                trace.status = "stuck"
                trace.summary = (
                    f"planner repeated '{planned.intent}' {repeats[signature]} times "
                    "without the screen changing"
                )
                self.recorder.log("discovery.stuck", signature=signature)
                break

            entry = TraceEntry(
                seq=turn,
                tool=planned.tool,
                args=dict(planned.args),
                rationale=planned.rationale,
                url_before=obs.url,
            )

            if planned.tool == "finish":
                trace.finished = True
                trace.status = planned.args.get("status", "give_up")
                trace.summary = planned.args.get("summary", "")
                entry.url_after = obs.url
                trace.entries.append(entry)
                break

            if planned.tool == "assert_state":
                ok = await self._check_assertion(planned)
                entry.status = "ok" if ok else "assertion_false"
                entry.url_after = obs.url
                entry.frame_path = _frame_path(planned.args.get("frame"))
                trace.entries.append(entry)
                history.append(f"{history_key(planned)} {planned.intent}")
                self.recorder.log("discovery.assert", id=planned.args.get("id"), holds=ok)
                continue

            entry.args["_outputs_remaining"] = list(context.get("outputs_remaining", []))
            try:
                await self._execute(planned, entry, obs)
            except PolicyViolation as exc:
                entry.status = "policy_blocked"
                entry.error = exc.message
                trace.entries.append(entry)
                self.recorder.log("discovery.policy_blocked", message=exc.message)
                trace.status = "blocked"
                trace.summary = f"policy blocked an action: {exc.message}"
                break
            except SableauError as exc:
                entry.status = "error"
                entry.error = f"{exc.code.value}: {exc.message}"
                trace.entries.append(entry)
                self.recorder.log("discovery.action_failed", code=exc.code.value, message=exc.message)
                history.append(f"{history_key(planned)} FAILED {exc.code.value}")
                continue

            entry.args.pop("_outputs_remaining", None)
            trace.entries.append(entry)
            if entry.tool == "act" and entry.args.get("action") == "read":
                # Reads leave the screen untouched, so the only way the planner
                # learns it succeeded is if we tell it what came back.
                name = entry.args.get("output")
                if name:
                    captured[name] = entry.read_value
                    context["outputs_remaining"] = [
                        o for o in declared_outputs if o not in captured
                    ]
                history.append(
                    f"{history_key(planned)} {planned.intent} -> captured {entry.read_value!r}"
                )
            else:
                history.append(f"{history_key(planned)} {planned.intent}")

            # A URL change is a screen change. Rather than hope the planner
            # notices, say so: checkpoints are what make replay verifiable, and
            # this is the one moment we know one is warranted.
            if entry.url_after and entry.url_after != entry.url_before:
                history.append(
                    "NOTE: the screen changed. Call assert_state now to record what "
                    "must be true here, before doing anything else."
                )

        shot = await self.surface.observe(with_screenshot=True)
        self.recorder.write_screenshot("discovery_final.png", shot.screenshot_png)
        self.recorder.write_json("trace.json", trace.to_dict())
        self.recorder.log("discovery.finish", status=trace.status, turns=len(trace.entries),
                          summary=trace.summary)
        return trace

    # ------------------------------------------------------------------

    async def _execute(self, planned: Planned, entry: TraceEntry, obs=None) -> None:
        a = planned.args
        kind = a.get("action", "")
        self.policy.check_action(kind)
        # The planner names controls the way a person would and often does not
        # say which frame one lives in. The observation already knows, so infer
        # it rather than making the planner track document boundaries.
        frame_path = _frame_path(a.get("frame") or _infer_frame(a, obs))
        entry.frame_path = frame_path

        if kind == "navigate":
            url = a.get("url") or ""
            self.policy.check_url(url)
            await self.surface.act(None, NavigateAction(url=url))
            entry.url_after = await self.surface.current_url()
            return

        hint_target = _hint_target(a, frame_path)
        resolution = await self.surface.resolve(hint_target, timeout_ms=6000)

        describe = getattr(self.surface, "describe", None)
        descriptor = await describe(resolution) if describe else {}
        entry.descriptor = descriptor
        entry.probes = await self._probe(descriptor, frame_path)

        if kind == "read" and not a.get("output"):
            remaining = list(entry.args.get("_outputs_remaining") or [])
            if len(remaining) == 1:
                a["output"] = remaining[0]
                entry.args["output"] = remaining[0]
                self.recorder.log("discovery.output_inferred", output=remaining[0])

        action = _build_action(kind, a)
        risk = self.policy.classify_risk(kind, planned.intent)
        self.policy.check_risky(risk, f"turn{entry.seq}", confirmed=True)
        result = await self.surface.act(resolution, action)
        if kind == "read":
            entry.read_value = result.value
        entry.url_after = await self.surface.current_url()
        self.recorder.log(
            "discovery.acted",
            turn=entry.seq,
            action=kind,
            risk=risk,
            resolved_via=resolution.strategy,
            durable_locators=[p.locator["strategy"] for p in entry.probes if p.ok],
            url=entry.url_after,
        )

    async def _check_assertion(self, planned: Planned) -> bool:
        from ..schema import Condition

        a = planned.args
        cond = Condition(
            kind=a["kind"],
            value=a.get("value"),
            target=_assert_target(a) if a["kind"] == "element_visible" else None,
            frame_path=_frame_path(a.get("frame")),
        )
        return await self.surface.evaluate(cond, timeout_ms=3000)

    async def _probe(self, descriptor: dict[str, Any], frame_path: list[str]) -> list[ProbedLocator]:
        """Try every candidate locator against the live page and count matches."""
        probes: list[ProbedLocator] = []
        for loc in _candidate_locators(descriptor):
            spec = TargetSpec.model_validate(
                {"candidates": [loc], "frame_path": frame_path, "ambiguity_policy": "fail_if_multiple"}
            )
            try:
                res = await self.surface.resolve(spec, timeout_ms=900)
                probes.append(ProbedLocator(locator=loc, match_count=res.match_count, ok=True))
            except SableauError as exc:
                probes.append(
                    ProbedLocator(locator=loc, match_count=0, ok=False, note=exc.code.value)
                )
        return probes


# ----------------------------------------------------------------------
# hint and locator construction
# ----------------------------------------------------------------------


def _infer_frame(args: dict[str, Any], obs) -> str | None:
    """Find the frame of the control the planner described."""
    if obs is None:
        return None
    testid = args.get("target_testid")
    name = (args.get("target_name") or args.get("target_label") or "").strip().lower()
    for control in obs.controls:
        if testid and control.get("testid") == testid:
            return control.get("frame")
        if name and name in (
            (control.get("name") or "").lower(),
            (control.get("label") or "").lower(),
        ):
            return control.get("frame")
    return None


def _frame_path(frame: str | None) -> list[str]:
    if not frame or frame in ("main", "top", ""):
        return []
    return [frame]


def _hint_target(args: dict[str, Any], frame_path: list[str]) -> TargetSpec:
    """Turn the planner's human level hints into something resolvable *now*.

    Ambiguity is tolerated here (``first``) because this is exploration. The
    artifact that comes out the other side is held to a stricter standard.
    """
    cands: list[dict[str, Any]] = []
    if args.get("target_testid"):
        cands.append({"strategy": "testid", "value": args["target_testid"]})
    if args.get("target_role") and args.get("target_name"):
        cands.append({"strategy": "role", "role": args["target_role"], "name_equals": args["target_name"]})
    if args.get("target_label"):
        cands.append({"strategy": "label", "text": args["target_label"], "exact": False})
    if args.get("target_role") and not args.get("target_name"):
        cands.append({"strategy": "role", "role": args["target_role"]})
    if args.get("target_text"):
        cands.append({"strategy": "text", "text": args["target_text"]})
    if args.get("target_name"):
        cands.append({"strategy": "text", "text": args["target_name"]})
    if not cands:
        raise PolicyViolation("planner proposed an action with no way to identify the control")
    return TargetSpec.model_validate(
        {"candidates": cands, "frame_path": frame_path, "ambiguity_policy": "first",
         "description": args.get("intent", "")[:80]}
    )


def _assert_target(args: dict[str, Any]) -> TargetSpec:
    cands: list[dict[str, Any]] = []
    if args.get("target_testid"):
        cands.append({"strategy": "testid", "value": args["target_testid"]})
    if args.get("target_role"):
        c: dict[str, Any] = {"strategy": "role", "role": args["target_role"]}
        if args.get("target_name"):
            c["name_equals"] = args["target_name"]
        cands.append(c)
    if not cands:
        cands.append({"strategy": "text", "text": args.get("value", "")})
    return TargetSpec.model_validate({"candidates": cands, "ambiguity_policy": "first"})


IMPLICIT_ROLE = {
    "a": "link",
    "button": "button",
    "select": "combobox",
    "textarea": "textbox",
    "h1": "heading",
    "h2": "heading",
}


def role_of(descriptor: dict[str, Any]) -> str:
    if descriptor.get("explicit_role"):
        return descriptor["explicit_role"]
    tag = descriptor.get("tag", "")
    if tag == "input":
        return "textbox" if descriptor.get("type", "text") in ("text", "", "search") else "button"
    return IMPLICIT_ROLE.get(tag, tag)


def _candidate_locators(descriptor: dict[str, Any]) -> list[dict[str, Any]]:
    """Every durable way to name this element, best first."""
    out: list[dict[str, Any]] = []
    if descriptor.get("testid"):
        out.append({"strategy": "testid", "value": descriptor["testid"], "confidence": 0.95})
    role = role_of(descriptor)
    name = (descriptor.get("aria_label") or descriptor.get("text") or "").strip()
    if role and name:
        out.append({"strategy": "role", "role": role, "name_equals": name, "confidence": 0.9})
    if descriptor.get("label"):
        out.append({"strategy": "label", "text": descriptor["label"], "exact": True, "confidence": 0.85})
    if descriptor.get("placeholder"):
        out.append({"strategy": "placeholder", "text": descriptor["placeholder"], "confidence": 0.7})
    if name and len(name) < 60:
        out.append({"strategy": "text", "text": name, "exact": False, "confidence": 0.6})
    if descriptor.get("css_path"):
        out.append({"strategy": "css", "value": descriptor["css_path"], "confidence": 0.3})
    return out


def _build_action(kind: str, a: dict[str, Any]):
    if kind == "click":
        return ClickAction()
    if kind == "type":
        return TypeAction(text=a.get("text", ""))
    if kind == "select":
        return SelectAction(value=a.get("value", ""))
    if kind == "press":
        return PressAction(key=a.get("key", "Enter"))
    if kind == "read":
        return ReadAction(binding="text")
    raise PolicyViolation(f"planner proposed an unsupported action: {kind}")


def _signature(planned: Planned, url: str) -> str:
    """Identify 'the same action on the same control on the same screen'."""
    a = planned.args
    bits = [planned.tool, str(a.get("action")), url]
    bits += [str(a.get(k, "")) for k in
             ("target_testid", "target_role", "target_name", "target_label",
              "target_text", "output", "value", "text", "key", "url")]
    return "|".join(bits)


def load_job(path: str) -> dict[str, Any]:
    with open(path) as fh:
        return json.load(fh)
