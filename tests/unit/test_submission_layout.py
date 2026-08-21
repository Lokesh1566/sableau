from pathlib import Path


def test_ci_discovery_cannot_pollute_the_production_catalog_or_evidence():
    workflow = Path(".github/workflows/tests.yml").read_text()

    assert "SABLEAU_EVIDENCE=/tmp/sableau-ci-discovery-evidence" in workflow
    assert "--out /tmp/legacy-claim-capability.json" in workflow


def test_dashboard_keeps_handoff_controls_stable_between_poll_updates():
    dashboard = Path("src/sableau/api/static/index.html").read_text()

    assert "existingOperatorPanel.dataset.controlState === incomingControlState" in dashboard
    assert 'data-control-state="PAUSED"' in dashboard
    assert 'data-control-state="HUMAN_CONTROL"' in dashboard


def test_dashboard_has_professional_three_surface_workspace():
    dashboard = Path("src/sableau/api/static/index.html").read_text()

    assert 'class="left-rail"' in dashboard
    assert 'class="workspace"' in dashboard
    assert 'class="chat-rail"' in dashboard
    assert 'id="capSearch"' in dashboard
    assert 'id="chatIn"' in dashboard
    assert 'id="live"' in dashboard
    assert 'id="chatLauncher"' in dashboard
    assert 'id="chatClose"' in dashboard
    assert 'function setChatOpen(open)' in dashboard
