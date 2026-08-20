.PHONY: help install run test test-cov test-e2e lint format check clean pre-commit-install pre-commit-run pre-commit-update docker-setup docker-build docker-run docker-push k8s-deploy k8s-logs k8s-manual k8s-status k8s-delete

help:
	@echo "jellyfin-toggl-sync - Available Commands"
	@echo ""
	@echo "Development:"
	@echo "  make install            Install dependencies"
	@echo "  make run                Run the sync script"
	@echo "  make test               Run unit tests"
	@echo "  make test-cov           Run tests with coverage"
	@echo "  make test-e2e           Run end-to-end tests (requires setup)"
	@echo "  make lint                Run ruff lint"
	@echo "  make format              Run ruff format"
	@echo "  make check              Run lint + format check"
	@echo "  make clean              Clean cache and temp files"
	@echo "  make pre-commit-install  Install pre-commit hooks"
	@echo "  make pre-commit-run      Run pre-commit hooks on all files"
	@echo "  make pre-commit-update   Update pre-commit hooks to latest versions"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-setup       Setup buildx for multi-platform (one-time)"
	@echo "  make docker-build       Build Docker image (local arch only)"
	@echo "  make docker-run         Run a one-shot sync via docker-compose"
	@echo "  make docker-push        Build & push multi-platform to Docker Hub"
	@echo ""
	@echo "Kubernetes:"
	@echo "  make k8s-deploy         Deploy to Kubernetes"
	@echo "  make k8s-logs           View logs from latest job"
	@echo "  make k8s-manual         Manually trigger a sync"
	@echo "  make k8s-status         Show K8s status"
	@echo "  make k8s-delete         Delete all K8s resources"

install:
	@echo "Installing dependencies..."
	uv sync --all-groups
	@echo ""
	@echo "✓ Dependencies installed"
	@echo ""
	@echo "Optional: Install pre-commit hooks with 'make pre-commit-install'"

run:
	@echo "Running sync..."
	cd src && uv run python -u sync.py

test:
	@echo "Running unit tests..."
	uv run --group dev pytest tests/ -v --ignore=tests/test_e2e.py

test-cov:
	@echo "Running tests with coverage..."
	uv run --group dev pytest tests/ -v --ignore=tests/test_e2e.py --cov=src --cov-report=html --cov-report=term
	@echo ""
	@echo "✓ Coverage report: htmlcov/index.html"

test-e2e:
	@echo "Running end-to-end tests..."
	@if [ "$(E2E_TEST_ENABLED)" != "true" ]; then \
		echo "⚠ E2E tests are disabled. Set E2E_TEST_ENABLED=true to run them."; \
		echo ""; \
		echo "Setup instructions:"; \
		echo "  1. Export E2E_JELLYFIN_URL, E2E_JELLYFIN_API_KEY"; \
		echo "  2. Export E2E_TOGGL_API_TOKEN, E2E_TOGGL_WORKSPACE_ID, E2E_TOGGL_PROJECT_ID"; \
		echo "  3. Export E2E_TEST_ENABLED=true"; \
		echo "  4. Run: make test-e2e"; \
		echo ""; \
		exit 1; \
	fi
	PYTHONUNBUFFERED=1 uv run --group dev pytest -c tests/pytest.e2e.ini tests/test_e2e.py

lint:
	@echo "Running lint..."
	uv run --group dev ruff check src/ tests/

format:
	@echo "Running formatter..."
	uv run --group dev ruff format src/ tests/

check:
	@echo "Running lint + format check..."
	uv run --group dev ruff check src/ tests/
	uv run --group dev ruff format --check src/ tests/

clean:
	@echo "Cleaning up..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name ".DS_Store" -delete
	rm -rf htmlcov .coverage .pytest_cache .ruff_cache
	@echo "✓ Clean complete"

pre-commit-install:
	@echo "Installing pre-commit hooks..."
	uv run --group dev pre-commit install --install-hooks

pre-commit-run:
	@echo "Running pre-commit hooks on all files..."
	uv run --group dev pre-commit run --all-files

pre-commit-update:
	@echo "Updating pre-commit hooks..."
	uv run --group dev pre-commit autoupdate

docker-setup:
	@echo "Setting up Docker buildx for multi-platform builds..."
	@if ! docker buildx ls | grep -q "multiplatform"; then \
		echo "Creating multiplatform builder..."; \
		docker buildx create --name multiplatform --driver docker-container --use --bootstrap; \
	else \
		echo "Multiplatform builder already exists"; \
		docker buildx use multiplatform; \
	fi
	@echo "✓ Builder ready. Run 'docker buildx ls' to verify."

docker-build:
	@echo "Building Docker image for local architecture..."
	docker buildx build --load -t jellyfin-toggl-sync:latest .

docker-run:
	@echo "Running one-shot sync via docker-compose..."
	docker compose run --rm jellyfin-toggl-sync python src/sync.py

docker-push:
	@echo "Building and pushing multi-platform images to Docker Hub..."
	@if ! docker buildx ls | grep -q "multiplatform"; then \
		echo "❌ buildx not set up. Run 'make docker-setup' first."; \
		exit 1; \
	fi
	@read -p "Enter Docker Hub username: " USERNAME; \
	echo ""; \
	echo "Logging in to Docker Hub..."; \
	docker login; \
	echo ""; \
	echo "Building for linux/amd64 and linux/arm64..."; \
	docker buildx build \
		--platform linux/amd64,linux/arm64 \
		-t $$USERNAME/jellyfin-toggl-sync:latest \
		--push \
		.; \
	echo ""; \
	echo "✓ Pushed to: $$USERNAME/jellyfin-toggl-sync:latest"

k8s-deploy:
	@echo "Deploying to Kubernetes..."
	@echo "NOTE: Make sure you've completed the setup steps in the README!"
	@echo ""
	kubectl apply -f k8s/base/namespace.yaml
	kubectl apply -f k8s/base/pvc.yaml
	@if [ ! -f k8s/secrets/configmap.yaml ]; then \
		echo "❌ k8s/secrets/configmap.yaml not found"; \
		echo "Copy and edit k8s/base/configmap-template.yaml first"; \
		exit 1; \
	fi
	@if [ ! -f k8s/secrets/secret.yaml ]; then \
		echo "❌ k8s/secrets/secret.yaml not found"; \
		echo "Copy and edit k8s/base/secret-template.yaml first"; \
		exit 1; \
	fi
	kubectl apply -f k8s/secrets/configmap.yaml
	kubectl apply -f k8s/secrets/secret.yaml
	kubectl apply -f k8s/base/cronjob.yaml
	@echo ""
	@echo "✓ Deployed to Kubernetes"
	@echo ""
	@echo "Next steps:"
	@echo "  1. Check status: make k8s-status"
	@echo "  2. View logs: make k8s-logs"

k8s-logs:
	@echo "Fetching logs from latest job..."
	@LATEST_JOB=$$(kubectl get jobs -n jellyfin-toggl --sort-by=.metadata.creationTimestamp -o name 2>/dev/null | tail -1); \
	if [ -z "$$LATEST_JOB" ]; then \
		echo "No jobs found in namespace jellyfin-toggl"; \
	else \
		kubectl logs -n jellyfin-toggl $$LATEST_JOB --tail=100; \
	fi

k8s-manual:
	@echo "Triggering manual sync..."
	kubectl create job -n jellyfin-toggl --from=cronjob/jellyfin-toggl-sync manual-sync-$$(date +%s)
	@echo "✓ Job created. Use 'make k8s-logs' to view progress."

k8s-status:
	@echo "Kubernetes Status:"
	@echo ""
	@echo "Namespace:"
	@kubectl get namespace jellyfin-toggl 2>/dev/null || echo "  Namespace not found"
	@echo ""
	@echo "CronJob:"
	@kubectl get cronjobs -n jellyfin-toggl 2>/dev/null || echo "  No CronJobs found"
	@echo ""
	@echo "Recent Jobs:"
	@kubectl get jobs -n jellyfin-toggl --sort-by=.metadata.creationTimestamp 2>/dev/null | tail -5 || echo "  No jobs found"

k8s-delete:
	@echo "⚠ This will delete the entire jellyfin-toggl namespace and all resources."
	@read -p "Are you sure? (y/N): " confirm; \
	if [ "$$confirm" = "y" ] || [ "$$confirm" = "Y" ]; then \
		kubectl delete namespace jellyfin-toggl; \
		echo "✓ Deleted"; \
	else \
		echo "Cancelled"; \
	fi
