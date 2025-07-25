
# Check if running inside Docker
IS_DOCKER := $(shell [ -f /.dockerenv ] && echo "yes" || echo "no")

# Ensure running inside Docker
ifeq ($(IS_DOCKER),no)
$(warning "Warning: Not running inside Docker. Make sure this is intentional.")
endif

# Print help
help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  buildsh   - Go into bash inside docker container"
	@echo "  deps      - Install dependencies"
	@echo "  build     - Build the project"
	@echo "  run       - Run the executable"
	@echo "  test      - Run tests"
	@echo "  clean     - Clean the build artifacts"
	@echo "  reclaim   - Reclaim storage"
	@echo "  new       - Create new React project"
	@echo "  doc       - Generate and open documentation"
	@echo "  help      - Show this help message"

proj=lamilltools

# Default target: Build the project
all: build

buildsh:
	(bash ./dev_container.sh)

.PHONY: deps new

# Install global dependencies
deps:
	@echo "Installing pnpm and Vite dependencies..."
	@SHELL=/bin/bash pnpm setup
	@export PNPM_HOME=$(pnpm env get home) && export PATH="$$PNPM_HOME:$$PATH"
	@SHELL=/bin/bash pnpm install stable
	@source /root/.bashrc && pnpm add -g vite create-vite
	@echo "Dependencies installed."

build:
ifeq ($(IS_DOCKER),no)
	$(error "NOT in docker container")
endif
	pnpm install
	pnpm exec playwright install --with-deps chromium
	pnpm exec playwright test
	pnpm exec playwright show-report

# Run tests
test: run

reclaim:
	@echo "Cleaning up unused Docker resources..."
	docker container prune -f
	docker image prune -af
	docker volume prune -f
	docker network prune -f
	docker system df
	@echo "Cleanup complete!"

# Clean the project
clean:
	rm -rf package.json package-lock.json node_modules/

# Generate the documentation
doc:
	$(CARGO) doc --open

# Phony targets (not real files)
.PHONY: all build run test clean doc help
