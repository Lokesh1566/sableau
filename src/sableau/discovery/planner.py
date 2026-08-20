"""Planners: the "decide" half of observe, decide, act.

Two implementations ship:

``AnthropicPlanner``
    A real language model driving the loop through tool use. This is the
    intended discovery path and needs ``ANTHROPIC_API_KEY``.

``HeuristicPlanner``
    A rule based planner that reads the same observation objects and makes the
    same kind of decisions without a network call. It exists so the loop, the
    UI interaction, the trace format and the compiler can be exercised and
    tested in environments with no API credential. Runs made with it are
    labelled ``planner="heuristic"`` in the artifact's provenance, so nobody can
    mistake one for a model driven discovery.

Neither planner is ever consulted during replay.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol

from ..surface.base import Observation
from .tools import TOOLS, Planned

SYSTEM_PROMPT = """You operate a business web application through its user interface.

You will be shown, each turn, a compact description of the current screen: its
URL, its visible text, and a list of interactive controls with their roles,
names, test ids and frames.

Work towards the stated goal one action at a time. Rules:

- Emit exactly one tool call per turn, and make it the NEXT step, not one you
  have already completed.
- Before acting, read `current_value` on the controls. A field that already holds
  the value you wanted is done: move on to the next step. Re-selecting a dropdown
  that is already set achieves nothing and wastes a turn.
- To replace text in a textbox, emit one `type` action containing the complete
  supplied value. The type primitive clears the existing value first. Never
  emit double-click, triple-click, select-all, or a preliminary clearing click.
- Consult STEPS ALREADY TAKEN every turn. If an action appears there and the
  screen reflects it, do not repeat it.
- Describe controls the way a person would: role plus visible name, or the test
  id when one exists. Never invent CSS selectors or XPath.
- Older, server-rendered screens often lay their forms out in tables, so a field
  has no label association and no accessible name. Those controls still carry a
  `name` attribute, shown as name_attr in the controls list. When you see one,
  pass it as `target_name_attr` - on those screens it is the only reliable way
  to address the field.
- Each control lists the `frame` it lives in. Pass that same value as `frame`
  when you act on it.
- Use `assert_state` EVERY time the screen changes, before your next action. A
  capability with no checkpoints cannot verify it reached the state it expected,
  and will be rejected. At minimum: assert the record is open once you reach it,
  and assert the confirmation screen once the work is saved.
- When you read a value that fills one of the declared outputs, use the `read`
  action and set `output` to that output's name.
- Static table cells and receipt values may have no role or label. They are
  still listed in controls with tag `td` and their visible value as `name`.
  Read those with `target_text` set to the exact visible value. Do not point a
  read at the page heading merely because the desired value appears elsewhere
  in visible text.
- A hidden `_token` control is shown with value `[opaque]`. Never copy or persist
  that token. Submit the live form normally; the browser carries the current
  per-transaction token with it, which prevents a recorded token from ever
  entering the artifact.
- Never use a value that was not supplied to you. Parameter values you are given
  are examples; the recorded workflow will be replayed with different ones.
- Call `finish` as soon as the goal is achieved, or if it clearly cannot be.
"""


class Planner(Protocol):
    name: str

    async def decide(
        self, goal: str, observation: Observation, history: list[dict[str, Any]], context: dict[str, Any]
    ) -> Planned: ...


# ---------------------------------------------------------------------------
# real model
# ---------------------------------------------------------------------------


class AnthropicPlanner:
    name = "anthropic"

    def __init__(self, model: str = "claude-sonnet-4-6", max_tokens: int = 1024):
        from anthropic import Anthropic  # imported lazily, never reachable from replay

        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it, or run discovery with "
                "--planner heuristic for an offline run."
            )
        self.client = Anthropic(api_key=key)
        self.model = model
        self.max_tokens = max_tokens
        self.calls = 0

    async def decide(self, goal, observation, history, context) -> Planned:
        user_block = _render_observation(goal, observation, history, context)
        self.calls += 1
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=[{"role": "user", "content": user_block}],
        )
        rationale = " ".join(b.text for b in msg.content if b.type == "text").strip()
        for block in msg.content:
            if block.type == "tool_use":
                return Planned(tool=block.name, args=dict(block.input), rationale=rationale)
        return Planned(tool="finish", args={"status": "give_up", "summary": rationale or "no tool call"},
                       rationale=rationale)


def _render_observation(goal, observation: Observation, history, context) -> str:
    controls = json.dumps(observation.controls[:60], indent=0)
    # Field state is what tells the planner whether its last action landed.
    done = "\n".join(f"{i+1}. {h}" for i, h in enumerate(history[-12:])) or "(nothing yet)"
    return f"""GOAL
{goal}

SUPPLIED PARAMETERS (examples for this discovery run)
{json.dumps(context.get('params', {}), indent=2)}

DECLARED OUTPUTS TO CAPTURE
{json.dumps(context.get('outputs', []), indent=2)}

OUTPUTS STILL OUTSTANDING: {context.get('outputs_remaining', 'unknown')}
OUTPUTS ALREADY CAPTURED:  {context.get('outputs_captured', {})}

STEPS ALREADY TAKEN
{done}

CURRENT SCREEN
url: {observation.url}
title: {observation.title}
frames: {observation.frames}

visible text:
{observation.text[:2500]}

controls:
{controls}
"""


# ---------------------------------------------------------------------------
# offline planner
# ---------------------------------------------------------------------------


class HeuristicPlanner:
    """Reads the live screen and decides what to do next, without a model.

    It is a genuine observe, decide, act agent: nothing is pre-recorded, and it
    inspects the same Observation the model would. It just reasons with rules.
    """

    name = "heuristic"

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, goal, observation: Observation, history, context) -> Planned:
        self.calls += 1
        if str(context.get("capability_id", "")).startswith("meridian_core."):
            return self._decide_meridian(observation, history, context)
        params = context.get("params", {})
        claim_id = params.get("claim_id", "")
        note = params.get("note", "")
        outcome = params.get("outcome", "APPROVED")
        taken = {h.split(" ", 1)[0] for h in history}
        url = observation.url
        controls = observation.controls

        def has(kind: str) -> bool:
            return kind in taken

        def find(**pred) -> dict[str, Any] | None:
            for c in controls:
                if all(str(c.get(k, "")).lower() == str(v).lower() for k, v in pred.items()):
                    return c
            return None

        # 1. land on the search screen
        if "/claims" not in url:
            return Planned("act", {
                "intent": "Open the claims search screen",
                "action": "navigate",
                "url": context["entry_url"],
            }, "not on the claims application yet")

        # 2. search for the claim
        if not has("type:search"):
            return Planned("act", {
                "intent": "Enter the claim reference in the search box",
                "action": "type", "text": claim_id, "frame": "main",
                "target_testid": "claim-search-input",
                "target_role": "textbox", "target_label": "Claim, member or provider",
            }, "the search box is the only way into a record")

        if not has("click:submit_search"):
            return Planned("act", {
                "intent": "Run the search",
                "action": "click", "frame": "main",
                "target_testid": "claim-search-submit",
                "target_role": "button", "target_name": "Search",
            }, "submit the search form")

        # 3. open the record from the results table
        row_link = next(
            (c for c in controls
             if c.get("tag") == "a" and (c.get("name") or "").strip() == claim_id),
            None,
        )
        if "no claims match" in observation.text.lower():
            return Planned("finish", {"status": "give_up",
                                      "summary": "the search returned no matching claim"})
        if row_link and f"/claims/{claim_id}" not in url:
            return Planned("act", {
                "intent": "Open the matching claim record from the results table",
                "action": "click", "frame": "main",
                "target_role": "link", "target_name": claim_id,
            }, "the claim reference in the results table links to the record")

        # 4. on the record: assert it, then fill the decision panel
        if f"/claims/{claim_id}" in url and not url.endswith("receipt"):
            if not has("assert:record_open"):
                return Planned("assert_state", {
                    "id": "record_open",
                    "description": "The requested claim record is open",
                    "kind": "text_present", "value": claim_id,
                }, "confirm we are on the right record before deciding")

            if "compliance notice" in observation.text.lower():
                return Planned("finish", {
                    "status": "give_up",
                    "summary": "a compliance notice is blocking the decision panel",
                }, "an unexpected dialog is covering the form")

            if not has("select:outcome"):
                return Planned("act", {
                    "intent": "Choose the decision outcome",
                    "action": "select", "value": outcome, "frame": "decision",
                    "target_testid": "decision-select",
                    "target_role": "combobox", "target_label": "Outcome",
                }, "the decision panel lives in the 'decision' iframe")

            if not has("type:note"):
                return Planned("act", {
                    "intent": "Record the decision note for the audit trail",
                    "action": "type", "text": note, "frame": "decision",
                    "target_role": "textbox", "target_label": "Decision note",
                }, "the note is mandatory")

            if not has("click:save"):
                return Planned("act", {
                    "intent": "Save the decision",
                    "action": "click", "frame": "decision",
                    "target_testid": "decision-submit",
                    "target_role": "button", "target_name": "Save decision",
                }, "this is the state changing step")

            return Planned("finish", {"status": "give_up",
                                      "summary": "saved the decision but no receipt appeared"})

        # 5. receipt screen: assert, capture outputs, finish
        if url.endswith("receipt"):
            if not has("assert:decision_recorded"):
                return Planned("assert_state", {
                    "id": "decision_recorded",
                    "description": "The receipt screen confirms the decision was recorded",
                    "kind": "text_present", "value": "Confirmation code",
                }, "the receipt is the proof the write succeeded")
            if not has("read:confirmation_code"):
                return Planned("act", {
                    "intent": "Capture the confirmation code from the receipt",
                    "action": "read", "frame": "main", "output": "confirmation_code",
                    "target_testid": "confirmation-code",
                }, "the confirmation code is the caller's receipt")
            if not has("read:decided_amount"):
                return Planned("act", {
                    "intent": "Capture the decided amount from the receipt",
                    "action": "read", "frame": "main", "output": "decided_amount",
                    "target_testid": "decided-amount",
                }, "the amount confirms which claim was acted on")
            return Planned("finish", {
                "status": "success",
                "summary": f"Recorded {outcome} on {claim_id} and captured the confirmation code",
            })

        return Planned("finish", {"status": "give_up", "summary": f"unexpected screen at {url}"})

    def _decide_meridian(self, observation: Observation, history, context) -> Planned:
        """Offline observe/decide/act fallback for the documented live target.

        This is deliberately labelled ``heuristic`` in provenance. It keeps the
        demo reproducible when no model credential is available; it is not used
        to claim model-driven discovery. The same policy, live DOM probing,
        compiler, and replay path still apply.
        """
        params = context.get("params", {})
        capability = str(context.get("capability_id", ""))
        url = observation.url
        controls = observation.controls
        taken = "\n".join(history).lower()

        def done(key: str) -> bool:
            return key.lower() in taken

        def control(name_attr: str) -> dict[str, Any] | None:
            return next((c for c in controls if c.get("name_attr") == name_attr), None)

        def current(name_attr: str) -> str:
            return str((control(name_attr) or {}).get("current_value") or "")

        def act(intent: str, action: str, **kwargs: Any) -> Planned:
            return Planned("act", {"intent": intent, "action": action, "frame": "main", **kwargs})

        def assertion(key: str, description: str, value: str, kind: str = "text_present") -> Planned:
            return Planned("assert_state", {
                "id": key, "description": description, "kind": kind, "value": value,
            })

        # Some legacy POST handlers render the confirmation at the same URL as
        # the edit form. Screen state, not a URL suffix, is authoritative.
        if (capability == "meridian_core.update_member_information"
                and "MEMBER INFORMATION UPDATED" in observation.text):
            if not done("assert:member_information_updated"):
                return assertion("member_information_updated", "The console confirms the member update",
                                 "MEMBER INFORMATION UPDATED")
            if not done("read:confirmation_message"):
                return act("Read the member update confirmation", "read",
                           output="confirmation_message", target_text="MEMBER INFORMATION UPDATED")
            return Planned("finish", {"status": "success", "summary": "Updated member contact information"})

        # Shared sign-on sequence.
        if "/signon" in url:
            if current("operator") != str(params.get("operator", "")):
                return act("Enter the supplied Operator ID", "type",
                           text=str(params.get("operator", "")), target_name_attr="operator")
            if current("password") != "[set]":
                return act("Enter the supplied password", "type",
                           text=str(params.get("password", "")), target_name_attr="password")
            if current("branch") != str(params.get("branch", "")):
                return act("Select the supplied branch", "select",
                           value=str(params.get("branch", "")), target_name_attr="branch")
            return act("Submit the sign-on form", "click",
                       target_role="button", target_name="Sign On")

        if url.rstrip("/").endswith("/menu"):
            if not done("assert:main_menu_loaded"):
                return assertion("main_menu_loaded", "An authenticated MAIN MENU is displayed", "MAIN MENU")
            if capability == "meridian_core.sign_on":
                if not done("read:session_status"):
                    return act("Read the authenticated session status", "read",
                               output="session_status", target_text="MAIN MENU")
                return Planned("finish", {"status": "success", "summary": "Signed on and verified MAIN MENU"})
            return act("Open Member Inquiry / Selection", "click",
                       target_role="link", target_name="Member Inquiry / Selection")

        # Search form versus result set are distinguished by the query string.
        if url.rstrip("/").endswith("/members") and "?" not in url:
            if not done("assert:member_inquiry_loaded"):
                return assertion("member_inquiry_loaded", "Member Inquiry / Selection is displayed",
                                 "MEMBER INQUIRY / SELECTION")
            search_by = str(params.get("search_by") or "number")
            query = str(params.get("query") or params.get("member_number") or "")
            if current("by") != search_by:
                return act("Choose how to search for the member", "select",
                           value=search_by, target_name_attr="by")
            if current("q") != query:
                return act("Enter the supplied member search value", "type",
                           text=query, target_name_attr="q")
            return act("Search for the member", "click", target_role="button", target_name="Search")

        if "/members?" in url:
            if not done("assert:search_results_loaded"):
                return assertion("search_results_loaded", "A selectable member result is displayed", "Select")
            return act("Open the first matching member record", "click",
                       target_role="link", target_name="Select")

        member = str(params.get("member_number") or params.get("query") or "")
        member_root = f"/members/{member}"

        if url.rstrip("/").endswith(member_root):
            if capability == "meridian_core.update_member_information" and done("click:save_changes"):
                if not done("assert:member_information_updated"):
                    return assertion("member_information_updated", "The console confirms the member update",
                                     "MEMBER INFORMATION UPDATED")
                if not done("read:confirmation_message"):
                    return act("Read the member update confirmation", "read",
                               output="confirmation_message", target_text="MEMBER INFORMATION UPDATED")
                return Planned("finish", {"status": "success", "summary": "Updated member contact information"})

            if not done("assert:member_record_loaded"):
                return assertion("member_record_loaded", "The requested MEMBER RECORD is displayed",
                                 "MEMBER RECORD")
            if capability == "meridian_core.update_member_information":
                return act("Open Update Member Information", "click",
                           target_role="link", target_name="Update Member Information")
            if capability == "meridian_core.place_account_hold":
                return act("Open Place Account Hold", "click",
                           target_role="link", target_name="Place Account Hold")
            return Planned("finish", {"status": "give_up", "summary": "unsupported offline member workflow"})

        if url.rstrip("/").endswith(f"{member_root}/update"):
            if not done("assert:update_form_loaded"):
                return assertion("update_form_loaded", "The Update Member Information form is displayed",
                                 "UPDATE MEMBER INFORMATION")
            for field_name, label in (
                ("email", "e-mail address"), ("phone", "phone number"), ("address", "mailing address")
            ):
                wanted = str(params.get(field_name, ""))
                if current(field_name) != wanted:
                    return act(f"Enter the supplied {label}", "type", text=wanted,
                               target_name_attr=field_name)
            return act("Save the member information changes", "click",
                       target_role="button", target_name="Save Changes")

        if url.rstrip("/").endswith(f"{member_root}/hold"):
            if not done("assert:hold_form_loaded"):
                return assertion("hold_form_loaded", "The Place Account Hold form is displayed",
                                 "PLACE ACCOUNT HOLD")
            if current("share") != str(params.get("share", "")):
                return act("Select the supplied share", "select", value=str(params.get("share", "")),
                           target_name_attr="share")
            if current("reason") != str(params.get("reason_code", "")):
                return act("Select the supplied hold reason", "select",
                           value=str(params.get("reason_code", "")), target_name_attr="reason")
            if current("notes") != str(params.get("notes", "")):
                return act("Enter the hold notes", "type", text=str(params.get("notes", "")),
                           target_name_attr="notes")
            return act("Continue to the hold review", "click", target_role="button", target_name="Continue")

        if url.rstrip("/").endswith(f"{member_root}/hold/review"):
            if not done("assert:hold_review_loaded"):
                return assertion("hold_review_loaded", "The hold review screen is displayed",
                                 "CONFIRM ACCOUNT HOLD")
            return act("Post the account hold", "click", target_role="button", target_name="Place Hold")

        if url.rstrip("/").endswith(f"{member_root}/hold/post"):
            if not done("assert:hold_posted"):
                return assertion("hold_posted", "The hold confirmation screen is displayed", "HOLD")
            remaining = context.get("outputs_remaining", [])
            if "confirmation_reference" in remaining:
                ref = next((str(c.get("name")) for c in controls
                            if re.fullmatch(r"CN\d+", str(c.get("name") or ""))), "")
                return act("Read the hold confirmation reference", "read",
                           output="confirmation_reference", target_text=ref)
            if "hold_status" in remaining:
                status = next((str(c.get("name")) for c in controls
                               if str(c.get("name") or "").strip().upper() == "HOLD"), "HOLD")
                return act("Read the resulting hold status", "read",
                           output="hold_status", target_text=status)
            return Planned("finish", {"status": "success", "summary": "Placed the account hold"})

        return Planned("finish", {"status": "give_up", "summary": f"unexpected MERIDIAN screen at {url}"})


def make_planner(kind: str, model: str | None = None) -> Planner:
    if kind == "anthropic":
        return AnthropicPlanner(model or os.environ.get("SABLEAU_MODEL", "claude-sonnet-4-6"))
    if kind == "heuristic":
        return HeuristicPlanner()
    raise ValueError(f"unknown planner: {kind}")


def history_key(planned: Planned) -> str:
    """Stable key used by the heuristic planner to know what it has already done."""
    a = planned.args
    if planned.tool == "assert_state":
        return f"assert:{a.get('id')}"
    if planned.tool != "act":
        return planned.tool
    action = a.get("action")
    if action == "read":
        return f"read:{a.get('output')}"
    if action == "type":
        if a.get("target_testid") == "claim-search-input":
            return "type:search"
        if a.get("target_label") == "Decision note":
            return "type:note"
        return f"type:{a.get('target_name_attr') or a.get('target_name') or a.get('target_label') or 'field'}"
    if action == "select":
        if a.get("target_testid") == "decision-select":
            return "select:outcome"
        return f"select:{a.get('target_name_attr') or a.get('target_name') or 'field'}"
    if action == "click":
        if a.get("target_testid") == "claim-search-submit":
            return "click:submit_search"
        if a.get("target_testid") == "decision-submit":
            return "click:save"
        name = str(a.get("target_name") or a.get("target_text") or "open_record")
        return f"click:{re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')}"
    return f"{action}:{re.sub(r'[^a-z]', '', str(a.get('target_name', ''))[:12].lower())}"
