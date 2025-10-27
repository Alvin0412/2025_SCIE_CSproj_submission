# Repository Guidelines

## Project Structure & Module Organization
Backend code lives under `apps/`: `service` exposes API endpoints, `pastpaper` and `indexing` handle document ingestion, and `retrieval` wraps search logic. Supporting orchestrators and realtime helpers sit in `service/`. Configuration and ASGI wiring are in `config/`, while shared helpers live in `utils/`. Templated responses reside in `templates/`. Deployment assets (Dockerfile, supervisor config, requirements) are under `deploy/`. Test data and exploratory notebooks are in `tests/`.

## Build, Test, and Development Commands
Create a virtual environment and install dependencies with `pip install -r deploy/requirements.txt`. Run the API locally via `python manage.py runserver`. Apply migrations with `python manage.py migrate`. Start the background worker alongside the app with `python manage.py rundramatiq`. For a full stack (Postgres, Redis, Qdrant) run `docker compose up --build` from the repository root; health checks wait for the database before starting Uvicorn and Dramatiq.

## Coding Style & Naming Conventions
Use Python 3.12 and adhere to PEP 8 (four-space indentation, snake_case for modules and functions, PascalCase for classes). Keep Django apps modular: serializers, views, and tasks belong in the corresponding app submodules. When adding async workers, group them under `service/` orchestrator modules. Prefer explicit imports and keep module-level constants in ALL_CAPS. Document non-obvious logic with concise docstrings.

## Testing Guidelines
Write Django `TestCase` classes under each app’s `tests.py` or a dedicated `tests/` package, naming methods `test_*`. Load JSON fixtures from `tests/*.json` when verifying indexing or retrieval flows. Run suites with `python manage.py test apps.service` (or another app) and ensure new endpoints include at least one integration test covering both happy and edge paths.

## Commit & Pull Request Guidelines
Follow the existing prefix convention (e.g., `fix: …`, `upd: …`) and keep messages imperative and scoped to a single concern. Reference issue IDs when applicable. Pull requests should summarize the change, list manual or automated test commands run, and include screenshots or sample payloads for API changes. Call out configuration or migration steps so reviewers can reproduce the setup quickly.

## Runtime Services & Secrets
The stack depends on Postgres, Redis, and Qdrant; keep `.env` aligned with `docker-compose.yml` and never commit secrets. When modifying background processing, update `deploy/supervisord.conf` so Uvicorn and Dramatiq stay in sync, and document any new environment variables.
