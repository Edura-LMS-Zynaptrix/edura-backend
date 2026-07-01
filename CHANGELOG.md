# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `shared/auth.py`: `require_role(roles)` dependency factory — validates Bearer JWT (HS256),
  enforces role membership, raises structured 401/403 errors; importable by all 11 services
- `user_service`: full REST API for user profile management
  - `GET /` — admin paginated user list
  - `GET /{user_id}` — own record or admin access
  - `PUT /{user_id}` — update own profile or admin
  - `PUT /{user_id}/role` — admin role assignment
  - `DELETE /{user_id}` — admin soft-delete
- `user_service/schemas.py`: Pydantic v2 request/response schemas
- `user_service/database.py`: `get_db()` session dependency
- `user_service/tests/test_auth.py`: unit tests for `require_role` (missing token, expired, wrong role, payload injection)
- `user_service/tests/test_users.py`: integration tests for all user endpoints

## [0.1.0] - Sprint 5 baseline

### Added
- 11 FastAPI microservice skeletons with health endpoints
- SQLAlchemy ORM models for all services (from SDS ER diagram)
- Alembic migrations for all services
- Neon PostgreSQL integration (`pool_pre_ping`, SSL, per-service `.env` files)
- Docker Compose stack (Redis, RabbitMQ, 11 services, Nginx)
- GitHub Actions CI scaffold
