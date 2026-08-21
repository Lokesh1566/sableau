"""The load bearing invariant: replay cannot reach a language model.

Asserted structurally rather than by convention. If someone later imports an
LLM client into the replay package, this fails.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "sableau"
FORBIDDEN = {"anthropic", "openai", "sableau.discovery", "httpx", "requests"}
PRODUCTION_PATH = ["replay", "schema", "surface", "kernel"]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import
                found.add("." * node.level + (node.module or ""))
            elif node.module:
                found.add(node.module)
    return found


@pytest.mark.parametrize("package", PRODUCTION_PATH)
def test_production_path_never_imports_an_llm_client(package):
    offenders = []
    for path in (SRC / package).rglob("*.py"):
        for name in _imports(path):
            root = name.split(".")[0]
            if root in FORBIDDEN or name in FORBIDDEN:
                offenders.append(f"{path.relative_to(SRC)} imports {name}")
            if name.startswith(".discovery") or name.startswith("..discovery"):
                offenders.append(f"{path.relative_to(SRC)} imports {name}")
    assert offenders == [], "the deterministic path reached for an LLM: " + "; ".join(offenders)


def test_replay_can_be_imported_with_no_llm_sdk_available(monkeypatch):
    """Simulate a deployment that does not even ship the Anthropic SDK."""
    for name in list(sys.modules):
        if name.startswith(("sableau.replay", "anthropic")):
            del sys.modules[name]

    import builtins

    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name.split(".")[0] == "anthropic":
            raise ImportError("anthropic is not installed in this deployment")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    import sableau.replay.engine as engine  # noqa: F401

    assert engine.ReplayEngine is not None


async def test_a_full_replay_completes_with_the_llm_sdk_poisoned(
    monkeypatch, surface, capability, params, tmp_path
):
    """Any attempt to call a model during replay raises, and the run still passes."""
    import builtins

    real_import = builtins.__import__

    class Poison:
        def __getattr__(self, item):
            raise AssertionError(f"replay tried to use an LLM client: .{item}")

    def guard(name, *args, **kwargs):
        if name.split(".")[0] in ("anthropic", "openai"):
            return Poison()
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)

    from sableau.kernel.observability import RunRecorder
    from sableau.kernel.policy import Policy
    from sableau.replay import ReplayEngine

    eng = ReplayEngine(
        surface,
        RunRecorder("t_poison", root=str(tmp_path), echo=False),
        Policy(),
        confirm_risky=True,
    )
    result = await eng.run(capability, params)
    assert result.ok and result.llm_calls == 0
