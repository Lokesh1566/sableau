"""Durable state boundary for watchable dashboard runs."""

from __future__ import annotations

import json

from sableau.api.live_runs import LiveRunStore


def test_live_run_state_survives_store_restart(tmp_path):
    first = LiveRunStore(tmp_path)
    first.create(
        "api_demo",
        {
            "run_id": "api_demo",
            "capability_id": "meridian_core.sign_on",
            "status": "queued",
        },
    )
    first.update("api_demo", status="running")

    restarted = LiveRunStore(tmp_path)

    assert restarted.get("api_demo") == {
        "run_id": "api_demo",
        "capability_id": "meridian_core.sign_on",
        "status": "running",
    }
    persisted = json.loads((tmp_path / "api_demo" / "live_state.json").read_text())
    assert persisted["status"] == "running"


def test_corrupt_persisted_state_fails_closed(tmp_path):
    run_dir = tmp_path / "api_corrupt"
    run_dir.mkdir()
    (run_dir / "live_state.json").write_text("not-json")

    assert LiveRunStore(tmp_path).get("api_corrupt") is None
