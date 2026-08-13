"""Surface abstraction.

A *surface* is anything a capability can be executed against. The replay engine
talks only to this protocol, so it contains no Playwright, no CDP, and no DOM
concepts. Adding accessibility-tree automation, screenshot plus coordinate
computer use, or a native desktop driver means writing one more class here and
declaring which features it supports.

Feature declaration is what makes the abstraction load bearing rather than
decorative: a capability states the features its locators need, and a surface
states what it can do. Mismatches are refused up front with
SURFACE_INCOMPATIBLE instead of failing halfway through a mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from ..schema import Action, Condition, TargetSpec


class SurfaceFeature(str, Enum):
    ROLE_QUERY = "role_query"        # can find by accessibility role plus name
    TEXT_QUERY = "text_query"        # can find by visible text
    LABEL_QUERY = "label_query"      # can find a form control by its label
    TESTID_QUERY = "testid_query"    # can find by test id attribute
    DOM_QUERY = "dom_query"          # can evaluate CSS selectors
    FRAMES = "frames"                # can descend into iframes or framesets
    COORDINATES = "coordinates"      # can click an x, y point
    SCREENSHOT = "screenshot"        # can capture pixels
    A11Y_TREE = "a11y_tree"          # can dump an accessibility tree
    DOM_SNAPSHOT = "dom_snapshot"    # can dump markup for evidence


#: Which surface feature each locator strategy needs. Used for compatibility checks.
STRATEGY_FEATURE: dict[str, SurfaceFeature] = {
    "role": SurfaceFeature.ROLE_QUERY,
    "testid": SurfaceFeature.TESTID_QUERY,
    "label": SurfaceFeature.LABEL_QUERY,
    "placeholder": SurfaceFeature.LABEL_QUERY,
    "text": SurfaceFeature.TEXT_QUERY,
    "css": SurfaceFeature.DOM_QUERY,
}


@dataclass
class Resolution:
    """A control the surface has located and is prepared to act on."""

    handle: Any
    strategy: str
    candidate_index: int
    match_count: int
    frame_path: tuple[str, ...] = ()
    describe: str = ""


@dataclass
class ActionResult:
    ok: bool = True
    value: Any = None
    note: str | None = None


@dataclass
class Observation:
    """A snapshot of the surface, used by the discovery loop to decide."""

    url: str
    title: str
    text: str = ""
    controls: list[dict[str, Any]] = field(default_factory=list)
    screenshot_png: bytes | None = None
    frames: list[str] = field(default_factory=list)


@dataclass
class EvidenceBundle:
    url: str | None = None
    screenshot_png: bytes | None = None
    dom_snapshot: str | None = None


@runtime_checkable
class Surface(Protocol):
    """The only thing the replay engine is allowed to talk to."""

    kind: str
    features: frozenset[SurfaceFeature]

    async def navigate(self, url: str) -> None: ...

    async def current_url(self) -> str: ...

    async def resolve(self, target: TargetSpec, timeout_ms: int = 8000) -> Resolution:
        """Find a control, trying candidates in order. Raises ControlResolutionError
        if nothing matches and AmbiguousControlError if the policy forbids
        multiple matches."""
        ...

    async def act(self, resolution: Resolution | None, action: Action) -> ActionResult: ...

    async def evaluate(self, condition: Condition, timeout_ms: int = 0) -> bool:
        """Evaluate a condition once (timeout_ms == 0) or poll until true."""
        ...

    async def observe(self, with_screenshot: bool = False) -> Observation: ...

    async def evidence(self) -> EvidenceBundle: ...

    async def close(self) -> None: ...


def missing_features(required: list[str], surface: Surface) -> list[str]:
    """Return the required feature names the surface does not provide."""
    have = {f.value for f in surface.features}
    return [r for r in required if r not in have]
