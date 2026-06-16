.PHONY: build run run-demo test tidy migrate-up migrate-down docker-up docker-down

BINARY := bin/trading

build:
	go build -o $(BINARY) ./cmd/trading

run: build
	./$(BINARY) -config config.yaml

# Run with synthetic signals so the pipeline + dashboard are observable.
run-demo: build
	./$(BINARY) -config config.yaml -demo

test:
	go test ./...

tidy:
	go mod tidy

# Migrations are applied automatically via gorm AutoMigrate on startup when a
# database is configured. These targets are provided for explicit control.
migrate-up:
	@echo "AutoMigrate runs on startup when database.host is set in config.yaml"

migrate-down:
	@echo "Drop tables manually or via your migration tool of choice"

docker-up:
	docker compose up -d

docker-down:
	docker compose down
