# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-31

### Added
- Event-driven communication between services (ADR-12): every change is announced on the RabbitMQ topic exchange `edura.events`, and services keep read-only `replica_*` tables updated by their event consumers (`shared/events.py`, `shared/messaging.py`, `shared/replicas.py`)
- `auth_service`: verification link email on registration (`auth.email-verification-requested`)
- Test suites for every service: 778 test cases, named Given-When-Then with Arrange-Act-Assert markers (DDP-#67), plus a test for `GET /api/assessments/{id}/my-last-attempt`
- CI: changed-lines coverage gate (80% on pull requests), build check, missing-tests failure, per-service `coverage-<service>` artifacts and a separate `Security Scan` job (DDP-#35)
- CI: branch coverage, measured on production code only (test files and Alembic migrations excluded) (DDP-#63)
- CI: combined coverage badge published from the `Coverage Badge` job
- README: project structure, prerequisites, step-by-step local setup, test coverage table, CI description and API documentation links

### Changed
- `notification_service`: email is sent through the Resend API (`RESEND_API_KEY`, `RESEND_FROM_EMAIL`, sender `noreply@eduralms.com`) instead of SMTP; the `SMTP_*` settings are removed
- Services no longer call one another over HTTP: access checks use local data kept current by `access.changed` events (the HTTP enrollment client of `content_service` is replaced)
- CI workflow also runs on pushes to `develop`, `main`, `feature/**`, `fix/**`, `hotfix/**` and `release/**`
- Docker images exclude `.env`; a push to `develop` triggers the staging deployment

### Fixed
- `notification_service` and `progress_service`: the RabbitMQ consumers and the certificate publisher connect with `RABBITMQ_USER` and `RABBITMQ_PASSWORD` instead of the removed `guest` account, so staging emails and progress events work again (DDP-#79)

## [0.3.0] - 2026-10-15

### Added
- `auth_service`: Google sign-in (`POST /api/auth/google`), password set, change and reset with notification emails, and email verification with a resend endpoint
- `auth_service`: sign-up profile creation event and secure admin role assignment
- `user_service`: student profile columns, `streams` and `grade_subjects` tables, internal user endpoints, referral system and wallet records
- `course_service`, `payment_service`, `content_service`: public ID fields for course, lesson, payment and profile images and files; receipt upload
- `enrollment_service`, `progress_service`: free enrolment and lesson progress tracking
- `assessment_service`, `analytics_service`, `admin_service`: exam rankings, admin dashboard and analytics endpoints, payment service integration
- Start-up event in every service that creates the database tables and sets up logging

## [0.2.0] - 2026-08-26

### Added
- `auth_service`: login with a 15-minute access token and a 7-day HttpOnly refresh token cookie, refresh token rotation with reuse detection (`REFRESH_TOKEN_REUSE`), Redis sessions, and `POST /api/auth/otp/request` and `/otp/verify` (6-digit OTP, 5-minute Redis lifetime) (DDP-#10). The first version used Asgardeo OAuth2 PKCE; it was replaced in 0.3.0 by the Auth Service's own tokens and Google sign-in (ADR-12 and the final report)
- `payment_service`: PayHere checkout initialisation, HMAC webhook handler and manual receipt upload (DDP-#15)
- `enrollment_service`: consumes `payment.success` to create or extend `ACTIVE` enrollments by 30 days and publishes `enrollment.activated`; `subscription.expired` suspends the enrollment; `GET /api/enrollments` (DDP-#16)
- `assessment_service`: timed quiz sessions in Redis, MCQ and True/False auto-grading, short answers flagged for manual review, tab-switch and focus-loss violation log, `assessment.graded` event (DDP-#17)
- `progress_service`: lesson watch tracking, course completion, certificate eligibility and leaderboard (DDP-#19)
- `notification_service`: RabbitMQ consumer for OTP, payment, certificate and violation emails with Jinja2 templates, failure log and requeue (DDP-#20)
- CI: GitHub Actions pipeline with linting, format check and pytest

### Changed
- `python-jose` replaced by `PyJWT` and the FastAPI version pinned to a stable release in all services (dependency vulnerabilities)
- All services formatted with Ruff and Black

## [0.1.0] - 2026-07-11

### Added
- 11 FastAPI microservice skeletons with health endpoints, Dockerfiles and `create_tables.py`
- SQLAlchemy ORM models for all services (from the SDS ER diagram), Alembic migrations and pytest smoke tests
- Neon PostgreSQL integration (`pool_pre_ping`, SSL, per-service `.env` files)
- Docker Compose stack (Redis, RabbitMQ, 11 services, Nginx)
- `shared/auth.py`: `require_role(roles)` dependency validating the Bearer JWT (HS256), enforcing roles and returning structured 401 and 403 errors (DDP-#11)
- `user_service`: user REST API (paginated list, own record or admin access, update, role assignment, soft delete) (DDP-#11)
- `course_service`: Course, Module and Lesson REST API with `DRAFT`, `PUBLISHED` and `ARCHIVED` status; publishing requires at least one module with a lesson; `course.published` event; owner check for teachers (DDP-#13)
- `content_service`: Cloudinary signed URLs (1-hour expiry), YouTube stream parameters (youtube-nocookie.com) and validated file upload (25 MB, PDF, PNG and JPEG with byte-signature check) (DDP-#14)

[Unreleased]: https://github.com/Edura-LMS-Zynaptrix/edura-backend/compare/v1.0.0...develop
[1.0.0]: https://github.com/Edura-LMS-Zynaptrix/edura-backend/compare/v0.3.0...v1.0.0
[0.3.0]: https://github.com/Edura-LMS-Zynaptrix/edura-backend/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Edura-LMS-Zynaptrix/edura-backend/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Edura-LMS-Zynaptrix/edura-backend/releases/tag/v0.1.0
