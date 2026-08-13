"""The action vocabulary the planner may emit.

The planner does not write free text that we then parse, and it does not write
selectors. It emits one structured tool call per turn, drawn from exactly the
action types the policy layer and the replay engine already understand. That is
what makes the allowlist enforceable rather than advisory: an action outside the
vocabulary cannot be expressed, let alone executed.

Locator *hints* are intent level ("the link whose text is CLM-004211"). Turning
a hint into a durable, ranked locator is the compiler's job, done by inspecting
the element that was actually acted on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ACT_TOOL = {
    "name": "act",
    "description": (
        "Perform one interaction with the application. Describe the control by what a "
        "person would see, not by a CSS selector."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "One short sentence saying why this step is needed.",
            },
            "action": {
                "type": "string",
                "enum": ["navigate", "click", "type", "select", "press", "read"],
            },
            "url": {"type": "string", "description": "For navigate only."},
            "text": {"type": "string", "description": "For type, the text to enter."},
            "value": {"type": "string", "description": "For select, the option value."},
            "key": {"type": "string", "description": "For press, e.g. Enter."},
            "frame": {
                "type": "string",
                "description": "Name of the iframe holding the control, or 'main'.",
            },
            "target_role": {"type": "string", "description": "button, link, textbox, combobox, heading..."},
            "target_name": {"type": "string", "description": "Visible or accessible name of the control."},
            "target_testid": {"type": "string"},
            "target_label": {"type": "string"},
            "target_text": {"type": "string"},
            "output": {
                "type": "string",
                "description": "For read, the name of the declared output this value fills.",
            },
        },
        "required": ["intent", "action"],
    },
}

ASSERT_TOOL = {
    "name": "assert_state",
    "description": (
        "Record something that must be true at this point for the workflow to be on "
        "track. These become replay checkpoints."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "short snake_case id, e.g. record_open"},
            "description": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": ["element_visible", "text_present", "url_matches"],
            },
            "value": {"type": "string", "description": "Text or url regex for text/url kinds."},
            "target_role": {"type": "string"},
            "target_name": {"type": "string"},
            "target_testid": {"type": "string"},
            "frame": {"type": "string"},
        },
        "required": ["id", "description", "kind"],
    },
}

FINISH_TOOL = {
    "name": "finish",
    "description": "The goal is complete, or cannot be completed. Say which and why.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["success", "give_up"]},
            "summary": {"type": "string"},
        },
        "required": ["status", "summary"],
    },
}

TOOLS = [ACT_TOOL, ASSERT_TOOL, FINISH_TOOL]


@dataclass
class Planned:
    """One decision from the planner."""

    tool: Literal["act", "assert_state", "finish"]
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    @property
    def intent(self) -> str:
        return self.args.get("intent") or self.args.get("description") or self.args.get("summary", "")


def hint_summary(args: dict[str, Any]) -> str:
    bits = [f"{k.replace('target_', '')}={v}" for k, v in args.items() if k.startswith("target_") and v]
    return ", ".join(bits) or "(no target)"
