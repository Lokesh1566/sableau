from __future__ import annotations

from sableau.kernel import MASK, Policy
from sableau.operator.app import perform_operator_action
from sableau.surface.null_surface import FakeElement, FakeScreen, NullSurface


async def test_typed_operator_value_reaches_surface_but_is_redacted_from_audit():
    url = "https://example.test/signon"
    surface = NullSurface(
        {
            url: FakeScreen(
                url,
                elements=[FakeElement("password", role="textbox", name="Password")],
            )
        },
        url,
    )

    detail = await perform_operator_action(
        surface,
        {
            "action": "type",
            "target": "textbox:Password",
            "value": "top-secret-value",
            "frame": "main",
        },
        Policy(allowed_hosts=["example.test"]),
    )

    assert surface.typed["password"] == "top-secret-value"
    assert detail["value"] == MASK
    assert "top-secret-value" not in str(detail)
