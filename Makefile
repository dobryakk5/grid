up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api worker

status:
	curl -s http://127.0.0.1:8000/api/bot/status | python -m json.tool

start-bot:
	curl -s -X POST http://127.0.0.1:8000/api/bot/start \
	  -H 'Content-Type: application/json' \
	  -d '{"symbol":"BTCUSDT","levels":3,"step_pct":"0.005","quote_per_level":"25"}' | python -m json.tool

stop-bot:
	curl -s -X POST http://127.0.0.1:8000/api/bot/stop | python -m json.tool
