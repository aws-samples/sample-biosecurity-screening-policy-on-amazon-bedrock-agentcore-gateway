.PHONY: help sync test test-unit test-infra test-gateway-integration test-gateway-full synth diff deploy deploy-all clean

help:
	@echo "Targets:"
	@echo "  sync                     - uv sync dependencies"
	@echo "  test                     - run full pytest suite"
	@echo "  test-unit                - run tests/unit only"
	@echo "  test-infra               - run tests/infrastructure only"
	@echo "  test-gateway-integration - test a deployed gateway (MCP + SigV4)"
	@echo "  test-gateway-full        - also invoke deployed biosafety screeners"
	@echo "  synth                    - cdk synth"
	@echo "  diff                     - cdk diff"
	@echo "  deploy                   - cdk deploy ResearchGatewayStack"
	@echo "  deploy-all               - cdk deploy --all (BiosafetyStack + ResearchGatewayStack)"
	@echo "  clean                    - remove cdk.out and pytest caches"

sync:
	uv sync

test:
	uv run pytest

test-unit:
	uv run pytest tests/unit

test-gateway-integration:
	uv run pytest --run-gateway-integration tests/integration

test-gateway-full:
	uv run pytest --run-gateway-full tests/integration

test-infra:
	uv run pytest tests/infrastructure

synth:
	uv run cdk synth

diff:
	uv run cdk diff

deploy:
	uv run cdk deploy ResearchGatewayStack

deploy-all:
	uv run cdk deploy --all

clean:
	rm -rf cdk.out .pytest_cache

prepare-foldseek-structures:
	uv run python scripts/prepare_foldseek_structures.py