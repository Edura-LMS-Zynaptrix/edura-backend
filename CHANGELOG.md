# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `auth_service`: OAuth2 PKCE login, JWT management, Redis sessions, and OTP verification (DDP-#10)
  - `POST /api/auth/login` — validates user credentials, issues 15-minute access token and 7-day HttpOnly refresh token cookie, tracks Redis session.
  - `POST /api/auth/refresh` — refresh token rotation with reuse detection (`REFRESH_TOKEN_REUSE`).
  - `POST /api/auth/otp/request` & `POST /api/auth/otp/verify` — 6-digit OTP generation with 5-minute (300s) TTL in Redis.
  - `shared/auth.py` — finalized `require_role` decorator supporting FastAPI `Depends()` and direct call invocations.
  - `auth_service/tests/test_auth_service.py` — unit and integration tests achieving 93% overall code coverage.
- `content_service`: media delivery endpoints (DDP-#19)
  - `POST /signed-url` — Cloudinary signed URL (1-hour expiry); students must be enrolled, teachers/admins unrestricted
  - `GET /lessons/{lesson_id}/stream` — YouTube embed parameters (youtube-nocookie.com, rel=0, modestbranding=1); enrollment enforced for students
  - `POST /upload` — file upload validation: size ≤ 25 MB, MIME whitelist (pdf/png/jpeg), python-magic byte-signature verification
- `content_service/cloudinary_utils.py`: `get_signed_url(public_id)` wrapper using `cloudinary.utils.cloudinary_url` with `sign_url=True`
- `content_service/enrollment_client.py`: async httpx client — calls `enrollment_service /enrollments` and checks `status == "active"`
- `content_service/tests/test_content.py`: 20 tests covering all ACs (router 100% coverage)
- `course_service`: full Course → Module → Lesson REST API (DDP-#17)
  - `POST /` — teacher creates course (status: DRAFT)
  - `GET /` — list courses; students see only PUBLISHED
  - `GET /{course_id}` — get course detail
  - `PUT /{course_id}` — update course; publishing requires ≥1 module with ≥1 lesson
  - `DELETE /{course_id}` — teacher or admin deletes own course
  - `POST /{course_id}/modules` — create module
  - `GET /{course_id}/modules` — list modules ordered by position
  - `POST /{course_id}/modules/{module_id}/lessons` — create lesson (youtube_video_id + cloudinary_asset_url)
  - `GET /{course_id}/modules/{module_id}/lessons` — list lessons
- `course_service/events.py`: `publish_course_published()` — publishes `course.published` event to RabbitMQ exchange `edura.events`
- `course_service/router.py`: `assert_course_owner()` helper — teachers can only mutate their own courses; admin bypasses check
- `course_service/alembic/versions/a1b2c3d4e5f6`: migration adding `status` ENUM (DRAFT/PUBLISHED/ARCHIVED) to courses and `youtube_video_id`, `cloudinary_asset_url` to lessons
- `course_service/tests/test_courses.py`: 17 tests covering all ACs (91% coverage)
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
