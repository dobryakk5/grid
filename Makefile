.PHONY: install test db-init api worker market-data dex-sampler dex-worker bybit-status health fomo-registry fomo-seed chain-tape chain-tape-bench chain-tape-rebuild

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements-dev.txt

test:
	.venv/bin/pytest

db-init:
	.venv/bin/python scripts/init_db.py

api:
	./scripts/run-api.sh

worker:
	./scripts/run-worker.sh

market-data:
	./scripts/run-market-data.sh

dex-sampler:
	./scripts/run-dex-sampler.sh

dex-worker:
	./scripts/run-dex-worker.sh

fomo-registry:
	./scripts/run-fomo-registry.sh

fomo-seed:
	.venv/bin/python scripts/seed_fomo_wallets.py

chain-tape:
	./scripts/run-chain-tape.sh

chain-tape-bench:
	.venv/bin/python scripts/chain_tape_bench.py

chain-tape-rebuild:
	.venv/bin/python scripts/rebuild-chain-swaps.py

bybit-status:
	curl -s http://127.0.0.1:8000/api/bybit/status | python3 -m json.tool

health:
	curl -s http://127.0.0.1:8000/health | python3 -m json.tool
