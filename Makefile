.PHONY: install test lint typecheck format generate materialize report run-all dev clean

install:
	pip install -e ".[dev,metricflow]"

test:
	pytest --cov-fail-under=80

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy src

format:
	ruff format .
	ruff check --fix .

generate:
	subs generate

# all Dagster assets in-process for the latest month (raw -> dbt build -> metrics -> report)
materialize:
	subs materialize

report:
	subs report

# generate -> materialize -> docs/results.md, charts, reports/<month>/report.html
run-all:
	subs run-all

# Dagster UI on http://localhost:3000 (run `make generate` first)
dev:
	dagster dev

clean:
	rm -rf data reports dbt/target dbt/logs
