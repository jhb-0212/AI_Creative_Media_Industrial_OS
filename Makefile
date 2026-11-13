.PHONY: help install test lint typecheck contract-check up down build

help: ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## 安装依赖（uv 锁文件可复现）
	uv sync --frozen --extra dev

test: ## 运行测试
	pytest

lint: ## 代码风格检查
	ruff check .

format: ## 代码格式化
	ruff format .

typecheck: ## 类型检查
	mypy packages services apps

contract-check: ## 契约一致性校验（OpenAPI/Schema 派生一致性）
	python scripts/contract_check.py

up: ## 本地启动（docker-compose）
	docker compose -f infra/compose/docker-compose.yml up -d

down: ## 停止本地服务
	docker compose -f infra/compose/docker-compose.yml down

build: ## 构建镜像
	docker build -f infra/docker/Dockerfile.api -t ai-film-industrial-api:latest .
