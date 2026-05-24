.PHONY: install test lint typecheck run dashboard scheduler docker-build

install:
	python -m pip install -r requirements.txt

test:
	python -m pytest

lint:
	python -m ruff check .

typecheck:
	python -m mypy app

run:
	python -m app.main run-once

dashboard:
	python -m app.main dashboard

scheduler:
	python -m app.main scheduler

docker-build:
	docker build -t domain-hunter-bot .
