"""Playwright DOM surface.

The only surface implemented today. It attaches to an already running Chromium
over CDP, which matters for the human handoff story: automation and operator
share one browser process and one live session, so transferring control is a
question of who holds the ownership token, not of re-creating state.
"""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Browser, Frame, Page, Playwright, async_playwright

from ..schema import Action, Condition, TargetSpec
from ..schema.errors import (
    AmbiguousControlError,
    ControlResolutionError,
    SableauError,
    TransientFailure,
)
from ..schema.errors import ErrorCode
from .base import ActionResult, EvidenceBundle, Observation, Resolution, SurfaceFeature


class PlaywrightDomSurface:
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
            SurfaceFeature.COORDINATES,
        }
    )

    def __init__(self, page: Page, browser: Browser | None = None, pw: Playwright | None = None):
        self.page = page
        self._browser = browser
        self._pw = pw
        self._owns_process = False

    # -- lifecycle -------------------------------------------------------

    @classmethod
    async def connect(cls, cdp_url: str, viewport_note: str = "") -> "PlaywrightDomSurface":
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(8000)
        return cls(page, browser, pw)

    async def close(self) -> None:
        # We do not kill the browser: the operator console may still be attached
        # to the same live session.
        if self._pw is not None:
            await self._pw.stop()

    # -- navigation ------------------------------------------------------

    async def navigate(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:  # noqa: BLE001
            raise TransientFailure(f"navigation to {url} failed: {exc}") from exc

    async def current_url(self) -> str:
        return self.page.url

    # -- frames ----------------------------------------------------------

    def _scope(self, frame_path: list[str] | tuple[str, ...]) -> Page | Frame:
        scope: Page | Frame = self.page
        for name in frame_path:
            if name in ("main", ""):
                continue
            found = None
            for fr in self.page.frames:
                if fr.name == name or (fr.url and fr.url.rstrip("/").endswith(name)):
                    found = fr
                    break
            if found is None:
                raise ControlResolutionError(f"frame not found: {name}")
            scope = found
        return scope

    # -- resolution ------------------------------------------------------

    def _locator_for(self, scope: Page | Frame, cand: Any):
        s = cand.strategy
        if s == "role":
            kw: dict[str, Any] = {}
            if cand.name_equals is not None:
                kw["name"] = cand.name_equals
                kw["exact"] = True
            elif cand.name_matches is not None:
                kw["name"] = re.compile(cand.name_matches)
            return scope.get_by_role(cand.role, **kw)
        if s == "testid":
            return scope.locator(f'[data-testid="{cand.value}"]')
        if s == "label":
            return scope.get_by_label(cand.text, exact=cand.exact)
        if s == "placeholder":
            return scope.get_by_placeholder(cand.text)
        if s == "text":
            base = scope.locator(cand.within_css) if cand.within_css else scope
            return base.get_by_text(cand.text, exact=cand.exact)
        if s == "css":
            return scope.locator(cand.value)
        raise SableauError(f"unknown locator strategy {s}", ErrorCode.SURFACE_INCOMPATIBLE)

    async def _verify(self, loc, verify) -> bool:
        if verify is None:
            return True
        if verify.kind == "attribute_contains":
            got = await loc.get_attribute(verify.attr or "")
            return bool(got and (verify.value or "") in got)
        if verify.kind == "text_contains":
            got = (await loc.inner_text()) or ""
            return (verify.value or "") in got
        if verify.kind == "role_equals":
            got = await loc.get_attribute("role")
            return got == verify.value
        if verify.kind == "enabled":
            return await loc.is_enabled()
        return True

    async def resolve(self, target: TargetSpec, timeout_ms: int = 8000) -> Resolution:
        scope = self._scope(target.frame_path)
        per_candidate = max(150, timeout_ms // max(1, len(target.candidates)))
        problems: list[str] = []
        ambiguous_at: int | None = None

        for idx, cand in enumerate(target.candidates):
            try:
                loc = self._locator_for(scope, cand)
                await loc.first.wait_for(state="attached", timeout=per_candidate)
                count = await loc.count()
                if count == 0:
                    problems.append(f"{cand.strategy}: no match")
                    continue
                if count > 1 and target.ambiguity_policy == "fail_if_multiple":
                    problems.append(f"{cand.strategy}: {count} matches")
                    ambiguous_at = count
                    continue
                chosen = loc.first
                if not await self._verify(chosen, target.verify):
                    problems.append(f"{cand.strategy}: verify failed")
                    continue
                return Resolution(
                    handle=chosen,
                    strategy=cand.strategy,
                    candidate_index=idx,
                    match_count=count,
                    frame_path=tuple(target.frame_path),
                    describe=target.description or cand.strategy,
                )
            except (ControlResolutionError, AmbiguousControlError):
                raise
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{cand.strategy}: {type(exc).__name__}")
                continue

        detail = f"{target.description or 'control'} :: " + "; ".join(problems)
        if ambiguous_at is not None and all("no match" not in p for p in problems):
            raise AmbiguousControlError(f"ambiguous control ({detail})")
        raise ControlResolutionError(f"could not resolve control ({detail})")

    # -- acting ----------------------------------------------------------

    async def act(self, resolution: Resolution | None, action: Action) -> ActionResult:
        t = action.type
        try:
            if t == "navigate":
                await self.navigate(action.url)
                return ActionResult(ok=True, value=self.page.url)
            if t == "wait":
                ok = await self.evaluate(action.until, timeout_ms=action.timeout_ms)
                return ActionResult(ok=ok, note="wait satisfied" if ok else "wait timed out")

            if resolution is None:
                if t == "read" and action.binding == "url":
                    return ActionResult(ok=True, value=self.page.url)
                raise ControlResolutionError(f"action {t} requires a resolved control")

            loc = resolution.handle
            if t == "click":
                await loc.click()
                return ActionResult(ok=True)
            if t == "type":
                if action.clear_first:
                    await loc.fill("")
                await loc.fill(action.text)
                return ActionResult(ok=True)
            if t == "select":
                await loc.select_option(action.value)
                return ActionResult(ok=True)
            if t == "press":
                await loc.press(action.key)
                return ActionResult(ok=True)
            if t == "read":
                if action.binding == "text":
                    return ActionResult(ok=True, value=(await loc.inner_text()).strip())
                if action.binding == "value":
                    return ActionResult(ok=True, value=await loc.input_value())
                if action.binding == "attribute":
                    return ActionResult(ok=True, value=await loc.get_attribute(action.attr or ""))
                if action.binding == "count":
                    return ActionResult(ok=True, value=await loc.count())
                if action.binding == "url":
                    return ActionResult(ok=True, value=self.page.url)
        except SableauError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransientFailure(f"action {t} failed: {type(exc).__name__}: {exc}") from exc
        raise SableauError(f"unsupported action {t}", ErrorCode.SURFACE_INCOMPATIBLE)

    # -- conditions ------------------------------------------------------

    async def evaluate(self, condition: Condition, timeout_ms: int = 0) -> bool:
        import time

        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while True:
            if await self._evaluate_once(condition):
                return True
            if time.monotonic() >= deadline:
                return False
            await self.page.wait_for_timeout(200)

    async def _evaluate_once(self, condition: Condition) -> bool:
        k = condition.kind
        try:
            if k == "url_matches":
                return re.search(condition.value or "", self.page.url) is not None
            scope = self._scope(condition.frame_path)
            if k in ("text_present", "text_absent"):
                body = await scope.locator("body").inner_text()
                present = (condition.value or "") in body
                return present if k == "text_present" else not present
            if k in ("element_visible", "element_absent", "element_count"):
                assert condition.target is not None
                try:
                    res = await self.resolve(condition.target, timeout_ms=250)
                except (ControlResolutionError, AmbiguousControlError):
                    return k == "element_absent"
                if k == "element_absent":
                    return False
                if k == "element_count":
                    return res.match_count == (condition.count or 0)
                return await res.handle.is_visible()
        except Exception:  # noqa: BLE001
            return False
        return False

    # -- observation and evidence ---------------------------------------

    _CONTROL_JS = """() => {
              const out = [];
              const sel = 'a,button,input,select,textarea,[role="button"],[role="link"],[role="dialog"],h1,h2';
              const labelFor = (el) => {
                if (el.id) {
                  const l = document.querySelector('label[for="' + el.id + '"]');
                  if (l) return l.innerText.trim();
                }
                const p = el.closest('label');
                return p ? p.innerText.trim() : '';
              };
              document.querySelectorAll(sel).forEach((el) => {
                const r = el.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) return;
                if (out.length > 70) return;
                const tag = el.tagName.toLowerCase();
                const isField = tag === 'input' || tag === 'select' || tag === 'textarea';
                // A field's identity is its label; its *state* is its value. Reporting
                // a <select>'s innerText would hand back the whole option list and make
                // an already-set control look untouched.
                let value = '';
                if (tag === 'select') {
                  const o = el.options[el.selectedIndex];
                  value = o ? o.text.trim() : '';
                } else if (isField) {
                  value = (el.value || '').slice(0, 60);
                }
                out.push({
                  tag,
                  role: el.getAttribute('role') || '',
                  type: el.getAttribute('type') || '',
                  name: isField
                    ? (el.getAttribute('aria-label') || labelFor(el) ||
                       el.getAttribute('placeholder') || el.getAttribute('name') || '')
                    : (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 70),
                  current_value: value,
                  label: labelFor(el),
                  testid: el.getAttribute('data-testid') || '',
                  placeholder: el.getAttribute('placeholder') || '',
                  href: el.getAttribute('href') || ''
                });
              });
              return out;
            }"""

    async def observe(self, with_screenshot: bool = False) -> Observation:
        """Compact, planner friendly view of the page, including every frame.

        Deliberately a *summary*, not raw markup: the planner should reason about
        controls, and raw HTML both blows the context budget and tempts a model
        into writing brittle selectors.
        """
        controls: list[dict[str, Any]] = []
        frame_names: list[str] = []
        for fr in self.page.frames:
            fname = fr.name or ("main" if fr.parent_frame is None else fr.url.rsplit("/", 1)[-1])
            frame_names.append(fname)
            try:
                found = await fr.evaluate(self._CONTROL_JS)
            except Exception:  # noqa: BLE001
                continue
            for c in found:
                c["frame"] = fname
                controls.append(c)
        try:
            text = await self.page.locator("body").inner_text()
            for fr in self.page.frames[1:]:
                text += "\n[frame " + (fr.name or "?") + "]\n" + await fr.locator("body").inner_text()
        except Exception:  # noqa: BLE001
            text = ""
        shot = await self.page.screenshot() if with_screenshot else None
        return Observation(
            url=self.page.url,
            title=await self.page.title(),
            text=text[:4000],
            controls=controls,
            screenshot_png=shot,
            frames=frame_names,
        )

    async def describe(self, resolution: Resolution) -> dict[str, Any]:
        """Everything we know about the element that was just acted on.

        Called at the moment of a successful action, which is the only moment the
        real element is available. The compiler turns this into ranked locators.
        """
        return await resolution.handle.evaluate(
            r"""(el) => {
              const labelFor = (n) => {
                if (n.id) {
                  const l = document.querySelector('label[for="' + n.id + '"]');
                  if (l) return l.innerText.trim();
                }
                const p = n.closest('label');
                return p ? p.innerText.trim() : '';
              };
              const path = (n) => {
                const parts = [];
                while (n && n.nodeType === 1 && parts.length < 5) {
                  let s = n.tagName.toLowerCase();
                  if (n.id) { parts.unshift(s + '#' + n.id); break; }
                  const cls = (n.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean);
                  if (cls.length) s += '.' + cls[0];
                  const sibs = n.parentNode ? Array.from(n.parentNode.children).filter(x => x.tagName === n.tagName) : [];
                  if (sibs.length > 1) s += ':nth-of-type(' + (sibs.indexOf(n) + 1) + ')';
                  parts.unshift(s);
                  n = n.parentElement;
                }
                return parts.join(' > ');
              };
              return {
                tag: el.tagName.toLowerCase(),
                explicit_role: el.getAttribute('role') || '',
                type: el.getAttribute('type') || '',
                aria_label: el.getAttribute('aria-label') || '',
                text: (el.innerText || '').trim().slice(0, 120),
                value: el.value || '',
                testid: el.getAttribute('data-testid') || '',
                label: labelFor(el),
                placeholder: el.getAttribute('placeholder') || '',
                href: el.getAttribute('href') || '',
                css_path: path(el)
              };
            }"""
        )

    async def evidence(self) -> EvidenceBundle:
        try:
            return EvidenceBundle(
                url=self.page.url,
                screenshot_png=await self.page.screenshot(full_page=False),
                dom_snapshot=await self.page.content(),
            )
        except Exception:  # noqa: BLE001
            return EvidenceBundle(url=None)
