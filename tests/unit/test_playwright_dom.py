from __future__ import annotations

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from sableau.schema import ClickAction
from sableau.schema.errors import TransientFailure
from sableau.surface.base import Resolution
from sableau.surface.playwright_dom import PlaywrightDomSurface


class _ClickLocator:
    def __init__(self, fallback_note: str | None):
        self.fallback_note = fallback_note
        self.clicks = 0
        self.evaluations = 0

    async def click(self) -> None:
        self.clicks += 1
        raise PlaywrightTimeoutError("legacy control never became stable")

    async def evaluate(self, script: str) -> str | None:
        self.evaluations += 1
        assert "requestSubmit" in script
        return self.fallback_note


class _TestSurface(PlaywrightDomSurface):
    def __init__(self):
        super().__init__(page=object())
        self.settles = 0

    async def _page_fingerprint(self) -> tuple[str, int]:
        return ("https://example.test/signon", 100)

    async def _settle(self, timeout_ms: int = 2500) -> None:
        self.settles += 1


def _resolution(locator: _ClickLocator) -> Resolution:
    return Resolution(
        handle=locator,
        strategy="css",
        candidate_index=0,
        match_count=1,
    )


async def test_click_timeout_uses_narrow_legacy_control_fallback():
    surface = _TestSurface()
    locator = _ClickLocator("submitted via requestSubmit")

    result = await surface.act(_resolution(locator), ClickAction())

    assert result.ok is True
    assert result.note == "submitted via requestSubmit after pointer click timed out"
    assert locator.clicks == 1
    assert locator.evaluations == 1
    assert surface.settles == 1


async def test_click_timeout_still_fails_for_non_legacy_controls():
    surface = _TestSurface()
    locator = _ClickLocator(None)

    with pytest.raises(TransientFailure, match="legacy control never became stable"):
        await surface.act(_resolution(locator), ClickAction())

    assert locator.clicks == 1
    assert locator.evaluations == 1
    assert surface.settles == 0
