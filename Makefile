.PHONY: test test-unit test-integration lint build run-api run-worker docker-build docker-up docker-down clean

test: test-unit test-integration

test-unit:
	python3 -m unittest discover -s tests/unit -v

test-integration:
	python3 -m unittest discover -s tests/integration -v

lint:
	python3 -m py_compile src/issue_to_pr_agent/__init__.py
	@echo "Syntax check passed."

build:
	pip install .

run-api:
	issue-to-pr-api --host 0.0.0.0 --port 8080

run-worker:
	issue-to-pr worker-run --worker-id worker-local --max-jobs 5

docker-build:
	docker build -t issue-to-pr-agent .

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
