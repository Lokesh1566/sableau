.PHONY: install browser up down discover replay errors handoff test test-unit test-integration evidence schema clean

CAP := capabilities/meridian.record_claim_decision.v1.0.0.json
NOTE := Reviewed against the plan schedule, provider in network, no duplicate.

install:            ## install the package and dev dependencies
	pip install -e ".[dev]"
	-python -m playwright install chromium

browser:            ## fallback Chromium when the Playwright CDN is unreachable
	cd browser && npm install

up:                 ## start the target application and the shared browser
	./scripts/up.sh

down:               ## stop them
	-pkill -f "targetapp.app"
	-pkill -f "browser_host.py"
	-pkill -f "electron .*browser"

discover:           ## LLM discovery; set PLANNER=heuristic to run without a key
	python -m sableau.cli discover --job jobs/approve_claim.json \
	  --planner $${PLANNER:-anthropic} \
	  --param claim_id=CLM-004211 --param outcome=APPROVED --param "note=$(NOTE)"

replay:             ## deterministic replay with parameters discovery never saw
	python -m sableau.cli replay --capability $(CAP) --confirm-risky \
	  --param claim_id=CLM-004212 --param outcome=APPROVED --param "note=$(NOTE)"

errors:             ## every runtime condition, classified not raised
	./scripts/demo_errors.sh

handoff:            ## pause, hand the live session to a person, resume
	python scripts/demo_handoff.py

test-unit:          ## no browser required
	python -m pytest tests/unit tests/test_no_llm_in_replay.py -v

test-integration:   ## real Chromium against the live application
	./scripts/up.sh && python -m pytest tests/integration -v

test: test-unit test-integration

evidence:           ## rebuild evidence/ from real runs
	./scripts/make_evidence.sh

schema:             ## export the capability JSON Schema
	python -m sableau.cli schema --out capabilities/capability.schema.json

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache *.egg-info src/*.egg-info
