.PHONY: install test lint typecheck backend frontend compose-up compose-down migrate

install:
	python3 -m pip install -e 'backend[dev]'
	cd frontend && npm install

test:
	pytest

lint:
	ruff check backend
	cd frontend && npm run lint

typecheck:
	mypy backend/app
	cd frontend && npm run lint

backend:
	uvicorn app.main:app --app-dir backend --reload

frontend:
	cd frontend && npm run dev

compose-up:
	docker compose up -d --build

compose-down:
	docker compose down

migrate:
	cd backend && alembic upgrade head
