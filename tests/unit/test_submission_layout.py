from pathlib import Path


def test_ci_discovery_cannot_pollute_the_production_catalog_or_evidence():
    workflow = Path(".github/workflows/tests.yml").read_text()

    assert "SABLEAU_EVIDENCE=/tmp/sableau-ci-discovery-evidence" in workflow
    assert "--out /tmp/legacy-claim-capability.json" in workflow
