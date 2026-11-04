.PHONY: dev test unit unit-coverage lint compose-up compose-down deploy seed

dev:
	uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

test:
	pytest tests/unit -v

unit-coverage:
	python -m pytest tests/unit -v --cov --cov-report=term-missing --cov-report=xml --cov-report=html

it:
	pytest tests/integration -v

compose-up:
	docker compose up -d --build

compose-down:
	docker compose down

deploy:
	bash deploy.sh

# 重灌示例库。脚本自带 CREATE DATABASE / DROP TABLE IF EXISTS，可重复执行。
# 前置：示例库必须先起起来（compose-up 只含 web，需叠加夹具文件）：
#   docker compose -f docker-compose.yml -f docker-compose.mysql.yml up -d
seed:
	docker compose -f docker-compose.yml -f docker-compose.mysql.yml exec -T mysql \
		sh -c 'exec env MYSQL_PWD="$$MYSQL_ROOT_PASSWORD" mysql -uroot' < scripts/seed_demo.sql
