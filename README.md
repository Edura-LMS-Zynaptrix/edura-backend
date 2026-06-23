# EDURA Backend — Microservices monorepo

This repository contains the 11 Python microservices that power the EDURA LMS backend.

## Structure
Each microservice is a separate folder containing its independent FastAPI implementation:
* `auth_service` — Port 8001
* `user_service` — Port 8002
* `course_service` — Port 8003
* `content_service` — Port 8004
* `enrollment_service` — Port 8005
* `payment_service` — Port 8006
* `assessment_service` — Port 8007
* `progress_service` — Port 8008
* `notification_service` — Port 8009 (RabbitMQ consumer loop only)
* `analytics_service` — Port 8010
* `admin_service` — Port 8011

The `shared/` directory contains helper logic (such as authorization and database wrappers) shared by the microservices.

## Development Setup

### Formatting & Linting
We use **Ruff** for linting and **Black** for code formatting.
To run Ruff:
```bash
ruff check .
```

To run Black formatter check:
```bash
black --check .
```
