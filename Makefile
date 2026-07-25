.PHONY: install test lint run docker-up docker-down

install:
	python -m pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check src tests

run:
	uvicorn market_sentinel.app:app --reload

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down
