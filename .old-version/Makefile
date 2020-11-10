help:
	@echo "Usage:"
	@echo "    make help             prints this help."
	@echo "    make deploy           deploy the project."
	@echo "    make fix              fix formatting and import sort ordering."
	@echo "    make format           run the format checker (black)."
	@echo "    make lint             run the linter (flake8)."
	@echo "    make run              run the dev server."
	@echo "    make setup            set up/update the local dev env."
	@echo "    make sort             run the sort checker (isort)."
	@echo "    make test             run the test suite."

.PHONY: deploy
deploy:
	fab deploy

.PHONY: fix
fix:
	black .
	isort .

.PHONY: format
format:
	@echo "Running black" && \
		black --check . \
		|| exit 1

.PHONY: lint
lint:
	@echo "Running flake8" && \
		flake8 \
		|| exit 1

.PHONY: run
run:
	python run.py watch https://jobs.opensafely.org/api/jobs

.PHONY: setup
setup:
	pip install -r requirements.txt
	pre-commit install

.PHONY: sort
sort:
	@echo "Running Isort" && \
		isort --check-only --diff . \
		|| exit 1

.PHONY: test
test:
	pytest --cov=runner
