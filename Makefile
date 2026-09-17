.PHONY: dev test unit lint compose-up compose-down deploy seed

dev:
	uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

test:
	pytest tests/unit -v

it:
	pytest tests/integration -v

compose-up:
	docker compose up -d --build

compose-down:
	docker compose down

deploy:
	bash deploy.sh

seed:
	mysql -h 127.0.0.1 -P 3306 -u root -p < scripts/seed_demo.sql
