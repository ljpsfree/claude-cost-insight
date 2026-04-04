.PHONY: up down restart logs status

up: ## Start the stack
	podman compose up -d
	@echo "Grafana: http://localhost:9847 (admin/admin)"

down: ## Stop the stack
	podman compose down

restart: ## Restart the stack
	podman compose restart

logs: ## Show all logs
	podman compose logs -f

status: ## Show status
	podman compose ps
