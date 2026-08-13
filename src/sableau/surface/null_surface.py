"""A tiny in-memory surface.

Its job is to prove a claim rather than to be useful in production: the replay
engine really does depend on nothing but the Surface protocol, so the entire
engine, including checkpoints, known outcomes, retries and output extraction,
can be exercised in unit tests with no browser at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..schema import Action, Condition, TargetSpec
from ..schema.errors import AmbiguousControlError, ControlResolutionError, TransientFailure
from .base import ActionResult, EvidenceBundle, Observation, Resolution, SurfaceFeature


@dataclass
class FakeElement:
    key: str
    role: str = "button"
    name: str = ""
    testid: str = ""
    label: str = ""
    text: str = ""
    attrs: dict[str, str] = field(default_factory=dict)
    value: str = ""
    visible: bool = True
    duplicate: int = 1  # simulate ambiguous matches


@dataclass
class FakeScreen:
    url: str
    body_text: str = ""
    elements: list[FakeElement] = field(default_factory=list)


class NullSurface:
    kind = "dom"
    features = frozenset(
        {
            SurfaceFeature.ROLE_QUERY,
            SurfaceFeature.TEXT_QUERY,
            SurfaceFeature.LABEL_QUERY,
            SurfaceFeature.TESTID_QUERY,
            SurfaceFeature.DOM_QUERY,
            SurfaceFeature.FRAMES,
            SurfaceFeature.SCREENSHOT,
            SurfaceFeature.DOM_SNAPSHOT,
        }
    )

    def __init__(self, screens: dict[str, FakeScreen], start: str):
        self.screens = screens
        self.url = start
        self.typed: dict[str, str] = {}
        #: key -> callable(surface) executed on click, used to move between screens
        self.on_click: dict[str, Callable[["NullSurface"], None]] = {}
        self.action_log: list[tuple[str, str]] = []
        self.fail_next: dict[str, int] = {}  # element key -> remaining forced failures
        self.closed = False

    # -- helpers used by tests -------------------------------------------

    @property
    def screen(self) -> FakeScreen:
        return self.screens[self.url]

    def goto(self, url: str) -> None:
        self.url = url

    # -- protocol ---------------------------------------------------------

    async def navigate(self, url: str) -> None:
        if url not in self.screens:
            raise TransientFailure(f"no such screen {url}")
        self.url = url

    async def current_url(self) -> str:
        return self.url

    def _matches(self, el: FakeElement, cand: Any) -> bool:
        s = cand.strategy
        if not el.visible:
            return False
        if s == "role":
            if el.role != cand.role:
                return False
            if cand.name_equals is not None:
                return el.name == cand.name_equals
            if cand.name_matches is not None:
                return re.search(cand.name_matches, el.name) is not None
            return True
        if s == "testid":
            return el.testid == cand.value
        if s == "label":
            return el.label == cand.text
        if s == "placeholder":
            return el.attrs.get("placeholder") == cand.text
        if s == "text":
            return cand.text in (el.text or el.name)
        if s == "css":
            return el.attrs.get("css") == cand.value
        return False

    def _verify_ok(self, el: FakeElement, verify) -> bool:
        if verify is None:
            return True
        if verify.kind == "attribute_contains":
            return (verify.value or "") in el.attrs.get(verify.attr or "", "")
        if verify.kind == "text_contains":
            return (verify.value or "") in (el.text or el.name)
        if verify.kind == "role_equals":
            return el.role == verify.value
        if verify.kind == "enabled":
            return el.attrs.get("disabled") != "true"
        return True

    async def resolve(self, target: TargetSpec, timeout_ms: int = 8000) -> Resolution:
        problems, ambiguous = [], False
        for idx, cand in enumerate(target.candidates):
            hits = [e for e in self.screen.elements if self._matches(e, cand)]
            total = sum(h.duplicate for h in hits)
            if total == 0:
                problems.append(f"{cand.strategy}: no match")
                continue
            if total > 1 and target.ambiguity_policy == "fail_if_multiple":
                problems.append(f"{cand.strategy}: {total} matches")
                ambiguous = True
                continue
            if not self._verify_ok(hits[0], target.verify):
                problems.append(f"{cand.strategy}: verify failed")
                continue
            return Resolution(
                handle=hits[0],
                strategy=cand.strategy,
                candidate_index=idx,
                match_count=total,
                frame_path=tuple(target.frame_path),
                describe=target.description or cand.strategy,
            )
        detail = "; ".join(problems)
        if ambiguous and all("no match" not in p for p in problems):
            raise AmbiguousControlError(f"ambiguous control ({detail})")
        raise ControlResolutionError(f"could not resolve control ({detail})")

    async def act(self, resolution: Resolution | None, action: Action) -> ActionResult:
        t = action.type
        if t == "navigate":
            await self.navigate(action.url)
            return ActionResult(ok=True, value=self.url)
        if t == "wait":
            return ActionResult(ok=await self.evaluate(action.until))
        if resolution is None:
            if t == "read" and action.binding == "url":
                return ActionResult(ok=True, value=self.url)
            raise ControlResolutionError(f"action {t} requires a control")

        el: FakeElement = resolution.handle
        remaining = self.fail_next.get(el.key, 0)
        if remaining > 0:
            self.fail_next[el.key] = remaining - 1
            raise TransientFailure(f"injected transient failure on {el.key}")

        self.action_log.append((t, el.key))
        if t == "click":
            hook = self.on_click.get(el.key)
            if hook:
                hook(self)
            return ActionResult(ok=True)
        if t == "type":
            el.value = action.text
            self.typed[el.key] = action.text
            return ActionResult(ok=True)
        if t == "select":
            el.value = action.value
            return ActionResult(ok=True)
        if t == "press":
            hook = self.on_click.get(el.key)
            if hook:
                hook(self)
            return ActionResult(ok=True)
        if t == "read":
            if action.binding == "text":
                return ActionResult(ok=True, value=el.text or el.name)
            if action.binding == "value":
                return ActionResult(ok=True, value=el.value)
            if action.binding == "attribute":
                return ActionResult(ok=True, value=el.attrs.get(action.attr or ""))
            if action.binding == "count":
                return ActionResult(ok=True, value=resolution.match_count)
            if action.binding == "url":
                return ActionResult(ok=True, value=self.url)
        return ActionResult(ok=False, note=f"unsupported {t}")

    async def evaluate(self, condition: Condition, timeout_ms: int = 0) -> bool:
        k = condition.kind
        if k == "url_matches":
            return re.search(condition.value or "", self.url) is not None
        if k in ("text_present", "text_absent"):
            present = (condition.value or "") in self.screen.body_text
            return present if k == "text_present" else not present
        try:
            res = await self.resolve(condition.target, timeout_ms=0)
        except (ControlResolutionError, AmbiguousControlError):
            return k == "element_absent"
        if k == "element_absent":
            return False
        if k == "element_count":
            return res.match_count == (condition.count or 0)
        return res.handle.visible

    async def observe(self, with_screenshot: bool = False) -> Observation:
        return Observation(
            url=self.url,
            title=self.url,
            text=self.screen.body_text,
            controls=[
                {"tag": e.role, "name": e.name, "testid": e.testid, "role": e.role}
                for e in self.screen.elements
            ],
            screenshot_png=b"" if with_screenshot else None,
        )

    async def evidence(self) -> EvidenceBundle:
        return EvidenceBundle(url=self.url, dom_snapshot=self.screen.body_text)

    async def close(self) -> None:
        self.closed = True
