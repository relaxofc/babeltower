.PHONY: up down test lint migrate shell

up:
	docker compose up -d

down:
	docker compose down

test:
	ENV=test pytest

lint:
	ruff check .

migrate:
	alembic upgrade head

shell:
	python

