.PHONY: install test api worker bybit-status health

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements-dev.txt

test:
	.venv/bin/pytest

api:
	./scripts/run-api.sh

worker:
	./scripts/run-worker.sh

bybit-status:
	curl -s http://127.0.0.1:8000/api/bybit/status | python3 -m json.tool

health:
	curl -s http://127.0.0.1:8000/health | python3 -m json.tool
