.PHONY: install discover replay serve errors test test-unit test-live schema legacy-up legacy-handoff clean

CAP := capabilities/meridian_core.check_member_balance.v1.0.0.json

install:             ## install package, test dependencies, and Chromium
	python -m pip install ".[dev]"
	python -m playwright install chromium

discover:            ## record live balance capability; PLANNER=heuristic is the offline fallback
	SABLEAU_PLANNER=$${PLANNER:-anthropic} bash core/record.sh balance

replay:              ## deterministic balance replay with parameters discovery did not use
	SABLEAU_POLICY=policy-core.json python -m sableau.cli replay \
	  --capability $(CAP) --param operator=teller1 --param password=password \
	  --param branch=MAIN-001 --param member_number=102777

serve:               ## run dashboard and capability API on port 8800
	SABLEAU_POLICY=policy-core.json python -m sableau.cli serve

errors:              ## invalid input, not-found business outcome, teller escalation
	bash core/demo_errors.sh

test-unit:           ## browser-free suite; live cases skip unless explicitly enabled
	python -m pytest -q

test-live:           ## live MERIDIAN API tests; shared browser/target must be reachable
	RUN_LIVE_MERIDIAN_TESTS=1 python -m pytest tests/integration/test_api.py -q

test: test-unit

schema:              ## export the capability JSON Schema
	python -m sableau.cli schema --out capabilities/capability.schema.json

legacy-up:           ## optional original local claims fixture and shared browser
	./scripts/up.sh

legacy-handoff: legacy-up  ## optional full pause/take-control/resume demonstration
	python scripts/demo_handoff.py

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache *.egg-info src/*.egg-info
