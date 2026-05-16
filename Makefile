# ── Makefile — playlistrec ────────────────────────────────────────────────────
#
# Self-bootstrapping — on a fresh Debian server with Docker installed:
#   git clone https://github.com/kylebrothers/playlistrec
#   cd playlistrec
#   make up
# ─────────────────────────────────────────────────────────────────────────────

# ── Infrastructure ────────────────────────────────────────────────────────────
NAS_IP         := 192.168.0.134
NAS_MOUNT_PATH := /mnt/nas

# ── App identity ──────────────────────────────────────────────────────────────
APP_NAME       := playlistrec

# ── Derived ───────────────────────────────────────────────────────────────────
ENV_FILE_PATH  := $(NAS_MOUNT_PATH)/Docker/$(APP_NAME)/config/.env
CONTAINER_NAME := $(APP_NAME)

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  := \033[0;32m
YELLOW := \033[1;33m
RED    := \033[0;31m
NC     := \033[0m

.PHONY: help check-deps mount-nas check-env setup build up down restart \
        logs shell clean status pull dev

help:
	@echo ""
	@echo "  $(GREEN)$(APP_NAME)$(NC) — available make targets"
	@echo ""
	@echo "  $(YELLOW)Setup & deployment$(NC)"
	@echo "    make up        — full bootstrap + build + start"
	@echo "    make pull      — git pull + restart"
	@echo "    make restart   — stop + start without rebuild"
	@echo "    make build     — build Docker image only"
	@echo "    make down      — stop container"
	@echo ""
	@echo "  $(YELLOW)Development$(NC)"
	@echo "    make dev       — start with FLASK_DEBUG=true"
	@echo "    make logs      — tail container logs"
	@echo "    make shell     — bash shell inside container"
	@echo "    make status    — show container status + health"
	@echo ""
	@echo "  $(YELLOW)Maintenance$(NC)"
	@echo "    make clean     — remove container and image"
	@echo "    make setup     — run checks without starting container"
	@echo ""

check-deps:
	@printf "Checking nfs-common... "
	@if ! which mount.nfs4 > /dev/null 2>&1; then \
		echo "$(YELLOW)not found — installing$(NC)"; \
		sudo apt-get install -y nfs-common; \
	else \
		echo "$(GREEN)ok$(NC)"; \
	fi

mount-nas: check-deps
	@printf "Checking NAS mount at $(NAS_MOUNT_PATH)... "
	@if ! mountpoint -q $(NAS_MOUNT_PATH); then \
		echo "$(YELLOW)not mounted — mounting$(NC)"; \
		sudo mkdir -p $(NAS_MOUNT_PATH); \
		sudo mount -t nfs4 $(NAS_IP):/ $(NAS_MOUNT_PATH) \
			-o nfsvers=4,nolock,soft,rw || \
			(echo "$(RED)ERROR: Could not mount NAS. Check that $(NAS_IP) is reachable.$(NC)" && exit 1); \
		echo "$(GREEN)NAS mounted at $(NAS_MOUNT_PATH)$(NC)"; \
	else \
		echo "$(GREEN)already mounted$(NC)"; \
	fi

check-env: mount-nas
	@printf "Checking .env at $(ENV_FILE_PATH)... "
	@if [ ! -f "$(ENV_FILE_PATH)" ]; then \
		echo "$(RED)NOT FOUND$(NC)"; \
		echo ""; \
		echo "  Create $(ENV_FILE_PATH) on the NAS using .env.example as a guide."; \
		echo "  Required NAS directories:"; \
		echo "    NAS:/Docker/$(APP_NAME)/config"; \
		echo "    NAS:/Docker/$(APP_NAME)/logs"; \
		echo "    NAS:/Docker/$(APP_NAME)/server_files"; \
		echo "    NAS:/Docker/$(APP_NAME)/database"; \
		echo ""; \
		exit 1; \
	else \
		echo "$(GREEN)ok$(NC)"; \
	fi
	@printf "Checking CLAUDE_API_KEY... "
	@if grep -q "^CLAUDE_API_KEY=sk-" "$(ENV_FILE_PATH)"; then \
		echo "$(GREEN)set$(NC)"; \
	else \
		echo "$(RED)missing or malformed$(NC)"; \
		exit 1; \
	fi
	@printf "Checking SECRET_KEY... "
	@if grep -qE "^SECRET_KEY=.{10,}" "$(ENV_FILE_PATH)"; then \
		echo "$(GREEN)set$(NC)"; \
	else \
		echo "$(RED)missing or too short$(NC)"; \
		exit 1; \
	fi

setup: check-env
	@printf "Checking SECRET_KEY... "
	@if grep -q "^SECRET_KEY=$$" "$(ENV_FILE_PATH)" 2>/dev/null; then \
		echo "$(YELLOW)not set — generating$(NC)"; \
		SECRET=$$(python3 -c "import secrets; print(secrets.token_hex(32))"); \
		sed -i "s/^SECRET_KEY=$$/SECRET_KEY=$$SECRET/" "$(ENV_FILE_PATH)"; \
		echo "$(GREEN)SECRET_KEY written to $(ENV_FILE_PATH)$(NC)"; \
	else \
		echo "$(GREEN)already set$(NC)"; \
	fi
	@echo "$(GREEN)Setup complete. Run 'make build' or 'make up' to continue.$(NC)"

build: check-env
	@echo "$(YELLOW)Building $(APP_NAME)...$(NC)"
	@cp $(ENV_FILE_PATH) .env
	NAS_IP=$(NAS_IP) docker-compose build
	@echo "$(GREEN)Build complete.$(NC)"

up: setup
	@echo "$(YELLOW)Starting $(APP_NAME)...$(NC)"
	@cp $(ENV_FILE_PATH) .env
	NAS_IP=$(NAS_IP) docker-compose up -d --build
	@echo "$(GREEN)$(APP_NAME) started. http://localhost:$$(grep HOST_PORT .env | cut -d= -f2 || echo 5000)$(NC)"

down:
	@echo "$(YELLOW)Stopping $(APP_NAME)...$(NC)"
	docker-compose down
	@echo "$(GREEN)Stopped.$(NC)"

restart: check-env
	@echo "$(YELLOW)Restarting $(APP_NAME)...$(NC)"
	@cp $(ENV_FILE_PATH) .env
	docker-compose down
	NAS_IP=$(NAS_IP) docker-compose up -d
	@echo "$(GREEN)Restarted.$(NC)"

pull: check-env
	@echo "$(YELLOW)Pulling latest changes...$(NC)"
	git pull
	@echo "$(YELLOW)Rebuilding and restarting...$(NC)"
	@cp $(ENV_FILE_PATH) .env
	NAS_IP=$(NAS_IP) docker-compose up -d --build
	@echo "$(GREEN)Deployment complete.$(NC)"

dev: check-env
	@echo "$(YELLOW)Starting $(APP_NAME) in development mode (FLASK_DEBUG=true)...$(NC)"
	@cp $(ENV_FILE_PATH) .env
	NAS_IP=$(NAS_IP) FLASK_DEBUG=true docker-compose up -d --build
	@echo "$(GREEN)Dev server started. Logs: make logs$(NC)"

logs:
	docker-compose logs -f $(CONTAINER_NAME)

shell:
	docker-compose exec $(CONTAINER_NAME) bash

status:
	@echo "$(YELLOW)Container status:$(NC)"
	docker-compose ps
	@echo ""
	@echo "$(YELLOW)Health check:$(NC)"
	@curl -sf http://localhost:$$(grep HOST_PORT .env 2>/dev/null | cut -d= -f2 || echo 5000)/health \
		| python3 -m json.tool 2>/dev/null || echo "Container not responding"

clean:
	@echo "$(YELLOW)Removing container and image for $(APP_NAME)...$(NC)"
	docker-compose down --rmi local -v
	@echo "$(GREEN)Clean complete.$(NC)"
