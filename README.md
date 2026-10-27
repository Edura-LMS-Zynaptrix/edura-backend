# EDURA Backend — Microservices monorepo

[![Backend CI](https://github.com/Edura-LMS-Zynaptrix/edura-backend/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/Edura-LMS-Zynaptrix/edura-backend/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Edura-LMS-Zynaptrix/edura-backend/badges/coverage.json)](https://github.com/Edura-LMS-Zynaptrix/edura-backend/actions/workflows/ci.yml)

This repository contains the 11 Python (FastAPI) microservices that power the EDURA Learning Management System (LMS). It is one part of the EDURA project:

| Repository | Contents |
|---|---|
| **edura-backend** (this repo) | The 11 microservices and the shared helper code |
| [edura-frontend](https://github.com/Edura-LMS-Zynaptrix/edura-frontend) | Next.js web application |
| [edura-infra](https://github.com/Edura-LMS-Zynaptrix/edura-infra) | Docker Compose, Nginx gateway, RabbitMQ setup, Kubernetes manifests and deploy pipeline |
| [edura-tests](https://github.com/Edura-LMS-Zynaptrix/edura-tests) | Integration, smoke and performance tests |
| [edura-documents](https://github.com/Edura-LMS-Zynaptrix/edura-documents) | SRS, SDS, ADRs, retrospectives and test evidence |

## Deployed application

| Environment | URL |
|---|---|
| Staging | https://staging.eduralms.com |
| Production | https://eduralms.com |

Staging deploys automatically on every push to `develop`. Production deploys on a push to `main`, after approval in the GitHub `production` environment (see [edura-infra](https://github.com/Edura-LMS-Zynaptrix/edura-infra#deployment--azure-aks-ddp-12-ddp-18)). Health check: `https://staging.eduralms.com/api/auth/health`.

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

The `shared/` directory contains helper logic (such as authorization, messaging and database wrappers) shared by the microservices. Services do not call each other over HTTP; they communicate through RabbitMQ events (see ADR-12 in edura-documents).

## Prerequisites

* [Git](https://git-scm.com/)
* [Docker Desktop](https://www.docker.com/products/docker-desktop/) (includes Docker Compose v2)
* [Python 3.12](https://www.python.org/downloads/) — only needed to run the linters and tests outside Docker
* A PostgreSQL database for each service. The project uses [Neon](https://neon.tech/) (a free account is enough); you need a connection string for each service's `DATABASE_URL`
* Optional, only for the features that use them: Cloudinary (`content`, `assessment`, `payment`), PayHere sandbox (`payment`), Resend (`notification`)

## Run the system locally (step by step)

The backend runs together with the other repositories through Docker Compose in `edura-infra`. Docker Compose builds the images from sibling folders, so the repositories must sit **next to each other**.

1. **Clone the repositories into one parent folder:**
   ```bash
   mkdir edura && cd edura
   git clone https://github.com/Edura-LMS-Zynaptrix/edura-backend.git
   git clone https://github.com/Edura-LMS-Zynaptrix/edura-infra.git
   git clone https://github.com/Edura-LMS-Zynaptrix/edura-frontend.git
   ```
2. **Create a `.env` file for every service** from its example (run from `edura-backend`):
   ```bash
   cd edura-backend
   for s in *_service; do cp "$s/.env.example" "$s/.env"; done      # Windows PowerShell: Get-ChildItem -Directory *_service | % { Copy-Item "$($_.Name)\.env.example" "$($_.Name)\.env" }
   ```
3. **Fill in the values** in each `<service>/.env`:
   * `DATABASE_URL` — your Neon connection string
   * `SECRET_KEY` — any long random string; **use the same value in every service**, because they all verify the same JWTs
   * `REDIS_HOST=redis` and `RABBITMQ_HOST=rabbitmq` — keep as they are
   * the optional keys listed in the `.env.example` of `content`, `assessment`, `payment` and `notification` services, if you use those features
4. **Start the stack** from `edura-infra`:
   ```bash
   cd ../edura-infra
   docker compose up --build
   ```
   The first build takes several minutes.
5. **Check that it works:** open http://localhost/api/auth/health and http://localhost/api/users/health. Each returns `{"status": "ok"}`. RabbitMQ management UI: http://localhost:15672 (user `guest`, password `guest`).
6. **Run the frontend** (see the [edura-frontend README](https://github.com/Edura-LMS-Zynaptrix/edura-frontend#readme)) and open http://localhost:3000.

To stop the stack press `Ctrl+C`, then `docker compose down`.

## Development Setup

### Install and run the tests

From the repository root, for one service (replace `auth_service` with any service folder):

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install pytest pytest-cov httpx
pip install -r auth_service/requirements.txt
PYTHONPATH=".:auth_service" pytest auth_service --cov=auth_service --cov-report=term
```

Windows PowerShell: `$env:PYTHONPATH = ".;auth_service"; python -m pytest auth_service --cov=auth_service --cov-report=term`.

Each service's `tests/test_migrations.py` needs a real PostgreSQL database in `DATABASE_URL`; it is skipped when none is available. CI runs it against a PostgreSQL container. Coverage on new code must be at least 80% (enforced on pull requests by CI).

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

## Continuous integration

Every pull request and every push to `develop`, `main`, `feature/**` and `fix/**` runs the **Backend CI** workflow: one job per service (lint, format check, build check, tests with coverage) and a `Security Scan` job (`pip-audit`). Coverage reports are uploaded as artifacts and the changed-lines coverage gate applies to pull requests.

The coverage badge at the top is updated by the `Coverage Badge` job on every push to `develop`: it adds up the line coverage of all services and writes the result to `coverage.json` on the `badges` branch, which the badge reads.

## Branching and releases

`main` holds released code and `develop` is the integration branch. Work happens on `feature/<ticket-id>-<slug>` or `fix/<ticket-id>-<slug>` branches and reaches `develop` through a reviewed pull request. Releases follow Semantic Versioning (`vMAJOR.MINOR.PATCH`); see the [Releases](https://github.com/Edura-LMS-Zynaptrix/edura-backend/releases) page and `CHANGELOG.md`.
