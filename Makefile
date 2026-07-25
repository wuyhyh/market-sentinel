.PHONY: install test lint verify run docker-up docker-down

install:
	python -m pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check src tests

verify:
	pytest -q
	ruff check src tests
	mypy src tests
	python -m compileall -q src tests

run:
	uvicorn market_sentinel.app:app --reload

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down
