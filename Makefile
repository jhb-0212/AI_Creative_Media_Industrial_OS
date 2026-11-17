.PHONY: help install test lint typecheck freeze-check contract-check arch-check release-check release up down build

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

freeze-check: ## 契约冻结校验（基线未变 / 十一类产物哈希 / 演进有据）
	python scripts/freeze_manifest.py --check

contract-check: ## 契约派生一致性（Action/OpenAPI/Schema/状态机/事件交叉校验）
	python scripts/contract_check.py

arch-check: ## 分层依赖校验（铁律 1：依赖方向/服务间直连/导入期循环）
	python scripts/architecture_check.py

release-check: ## 发布包闸门（受控来源/禁止路径/密钥扫描/哈希验证，临时包不落盘）
	python scripts/release_package.py --check

release: ## 构建可复现 Release 源码包（输出 dist/）
	python scripts/release_package.py --output dist/ai-creative-media-industrial-os.zip

up: ## 本地启动（docker-compose）
	docker compose -f infra/compose/docker-compose.yml up -d

down: ## 停止本地服务
	docker compose -f infra/compose/docker-compose.yml down

build: ## 构建镜像
	docker build -f infra/docker/Dockerfile.api -t ai-film-industrial-api:latest .
