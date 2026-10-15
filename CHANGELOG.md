# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `notification_service`: RabbitMQ consumer for OTP, payment, certificate, and violation email dispatch via SMTP (DDP-#20)
  - `consumer.py` — RabbitMQ worker consuming from `notification.queue` bound to exchange `edura.events` for routing keys `auth.otp-requested`, `payment.success`, `certificate.issued`, and `proctoring.violation`.
  - `email_service.py` & `templates/` — Jinja2 HTML email rendering and SMTP email dispatch for `otp_email.html`, `payment_confirm.html`, `cert_issued.html`, and `violation_alert.html`.
  - Fault tolerance & persistence — catches SMTP errors gracefully, records delivery failures as `NotificationLog(status=FAILED, error_message=...)`, and nacks messages (`requeue=True`) without crashing the consumer.
  - Test suite (`test_templates.py`, `test_consumer.py`, `test_email_service.py`) achieving 92% code coverage on `notification_service`.
- `assessment_service`: timed quiz sessions, Redis caching, MCQ auto-grading, and anti-cheat tab-switch detection (DDP-#17)
  - `POST /api/assessments/{id}/start` — initializes timed session in Redis (`session:assessment:{session_id}`) with TTL equal to assessment time limit, prevents duplicate active sessions (409 Conflict), and returns shuffled questions with answer key obfuscated.
  - `POST /api/assessments/{id}/submit` — auto-grades MCQ and True/False questions, flags Short Answer for manual review, handles auto-submit on timer expiry, updates DB `Submission` record, and publishes `assessment.graded` event to RabbitMQ.
  - `POST /api/assessments/{id}/violations` — logs tab-switch and focus-lost anti-cheat events into `ViolationLog` table, returning 204 No Content.
  - `assessment_service/tests/test_assessment.py` — unit and integration test suite achieving 84% overall code coverage on `assessment_service`.
- `enrollment_service`: consume `payment.success` RabbitMQ event and grant/revoke course access (DDP-#16)
  - RabbitMQ Consumer (`consumer.py`) on queue `enrollment.queue` bound to exchange `edura.events`:
    - Processes `payment.success` events to create or extend `ACTIVE` enrollments by 30 days and publishes `enrollment.activated` event.
    - Processes `subscription.expired` events to update enrollment status to `SUSPENDED`.
  - Endpoint `GET /api/enrollments` query endpoint returning enrollment status for external service access checks.
  - Test suite (`tests/test_enrollment.py`) covering all acceptance criteria with 87% code coverage on `enrollment_service`.
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

