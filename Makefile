.PHONY: help install test lint typecheck freeze-check contract-check arch-check merge-check merge-check-full up down build dev-keys

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

typecheck: ## 类型检查（与 CI 保持一致，含 workers）
	mypy packages services apps workers

freeze-check: ## 契约冻结校验（基线未变 / 十一类产物哈希 / 演进有据）
	python scripts/freeze_manifest.py --check

contract-check: ## 契约派生一致性（Action/OpenAPI/Schema/状态机/事件交叉校验）
	python scripts/contract_check.py

arch-check: ## 分层依赖校验（铁律 1：依赖方向/服务间直连/导入期循环）
	python scripts/architecture_check.py

merge-check: ## 合并前自检（轻量）：字典重复键/同名定义/覆盖面防呆
	python scripts/merge_readiness.py

merge-check-full: ## 合并前自检（完整）：轻量 + git 前置 + CI 全口径
	python scripts/merge_readiness.py --git --full

up: ## 本地启动（docker-compose）
	docker compose -f infra/compose/docker-compose.yml up -d

down: ## 停止本地服务
	docker compose -f infra/compose/docker-compose.yml down

build: ## 构建镜像
	docker build -f infra/docker/Dockerfile.api -t ai-film-industrial-api:latest .

dev-keys: ## 生成开发用身份密钥对（落盘到 infra/compose/.dev-keys/，已 gitignore）
	@python -c "from pathlib import Path; from packages.identity.keys import generate_signing_key as g; d=Path('infra/compose/.dev-keys'); d.mkdir(parents=True, exist_ok=True); k=g(); (d/'private.pem').write_bytes(k.private_pem); (d/'public.pem').write_bytes(k.public_pem); print('已生成：', d/'private.pem', '与', d/'public.pem')"
	@echo '用法：export IDENTITY_PRIVATE_KEY_PEM="$$(cat infra/compose/.dev-keys/private.pem)" && export IDENTITY_PUBLIC_KEY_PEM="$$(cat infra/compose/.dev-keys/public.pem)" && make up'
	@echo '注意：私钥只注入 identity 进程；API 只拿公钥（走 .dev-keys/public.pem）。切勿提交 .dev-keys/。'
