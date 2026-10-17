# Interview Prep Tracker API

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Django 5.2](https://img.shields.io/badge/Django-5.2-green.svg)](https://www.djangoproject.com/)
[![Django REST Framework](https://img.shields.io/badge/DRF-3.18-red.svg)](https://www.django-rest-framework.org/)
[![Tests](https://img.shields.io/badge/Tests-19%20Passing-success.svg)](tracker/tests/)

A backend REST API built with **Django** and **Django REST Framework (DRF)** to track coding interview preparation, problem attempts, time metrics, and performance analytics.

Features JWT authentication, owner-scoped querysets, problem filtering, search, pagination, and OpenAPI 3.0 documentation.

---

## Key Features

- **JWT Authentication (`djangorestframework-simplejwt`)**:
  - Registration, login, and token refresh endpoints.
  - Stateless Bearer token verification.
- **Data Models & Constraints**:
  - `Problem`: Categorized by topic and difficulty, storing external problem URLs.
  - `Attempt`: Tracks solve status (`solved`, `attempted`, `failed`, `reviewing`), duration in minutes, personal notes, and dates.
  - Database-level `UniqueConstraint(fields=['user', 'problem'])` preventing duplicate attempt records per candidate.
- **Owner-Scoped QuerySets & Object Permissions**:
  - `get_queryset()` strictly filters records scoped to `request.user`.
  - Object-level `IsOwner` permission ensures User A receives `404 Not Found` when trying to view, update, or delete User B's attempts.
- **Filtering, Search & Pagination**:
  - Topic and difficulty filtering via `django-filter` (`?topic=arrays&difficulty=easy`).
  - Search by problem title and attempt notes (`?search=tree`).
  - Configurable page-based pagination (`?page=2&page_size=10`).
- **Aggregated Analytics**:
  - Dedicated `/api/attempts/stats/` endpoint computed with ORM aggregation (`annotate`, `Count`, `Sum`) to report solved counts per topic, difficulty breakdowns, and total practice time.
- **Database & Deployment**:
  - SQLite for local REDACTED, with support for PostgreSQL via `dj-database-url`.
  - Static files served via WhiteNoise.
  - Deployment configuration for Render (`render.yaml`, `build.sh`, `Procfile`).
- **Interactive Documentation**:
  - Swagger UI: `/api/docs/`
  - ReDoc: `/api/redoc/`
  - Raw OpenAPI 3 schema: `/api/schema/`
- **Automated Test Suite**:
  - 19 automated tests covering authentication, CRUD, permissions, unique constraints, and analytics with 100% pass rate.

---

## Architecture & Data Model

```mermaid
erDiagram
    USER ||--o{ ATTEMPT : "records"
    PROBLEM ||--o{ ATTEMPT : "referenced in"

    USER {
        int id PK
        string username
        string email
        string password
    }

    PROBLEM {
        int id PK
        string title
        string topic "arrays, strings, trees, graphs, dp, etc."
        string difficulty "easy, medium, hard"
        string link
        datetime created_at
    }

    ATTEMPT {
        int id PK
        int user_id FK
        int problem_id FK
        string status "solved, attempted, failed, reviewing"
        int time_taken "minutes"
        text notes
        date date
        datetime updated_at
    }
```

---

## API Endpoints Reference

### Authentication (`/api/auth/`)
| Method | Endpoint | Description | Auth Required |
|---|---|---|---|
| `POST` | `/api/auth/register/` | Register new user; returns user object + JWT tokens | No |
| `POST` | `/api/auth/login/` | Obtain JWT access & refresh token pair | No |
| `POST` | `/api/auth/token/refresh/` | Refresh expired access token | No |
| `GET` | `/api/auth/me/` | Retrieve currently authenticated user profile | **Yes (Bearer)** |

### Problems (`/api/problems/`) — Read-Only
| Method | Endpoint | Description | Query Parameters |
|---|---|---|---|
| `GET` | `/api/problems/` | List paginated interview problems | `?topic=`, `?difficulty=`, `?search=`, `?page=` |
| `GET` | `/api/problems/{id}/` | Retrieve single problem detail | None |

### Practice Attempts (`/api/attempts/`) — Owner Scoped
| Method | Endpoint | Description | Auth Required |
|---|---|---|---|
| `GET` | `/api/attempts/` | List user's attempts (scoped to authenticated owner) | **Yes (Bearer)** |
| `POST` | `/api/attempts/` | Record new attempt (user automatically attached) | **Yes (Bearer)** |
| `GET` | `/api/attempts/{id}/` | Retrieve attempt detail (only if owner) | **Yes (Bearer)** |
| `PUT` | `/api/attempts/{id}/` | Replace attempt record (only if owner) | **Yes (Bearer)** |
| `PATCH` | `/api/attempts/{id}/` | Partially update attempt status, notes, or time | **Yes (Bearer)** |
| `DELETE` | `/api/attempts/{id}/` | Delete attempt record (only if owner) | **Yes (Bearer)** |
| `GET` | `/api/attempts/stats/` | Aggregated analytics: solved per topic/difficulty & total time | **Yes (Bearer)** |

### Documentation & Health
| Endpoint | Description |
|---|---|
| `/` | Health check & API root discovery |
| `/api/docs/` | Interactive Swagger UI |
| `/api/redoc/` | ReDoc API documentation |
| `/api/schema/` | OpenAPI 3.0 YAML/JSON Schema |

---

## Local Setup & Quickstart

### Prerequisites
- Python 3.10+
- Git

### 1. Clone & Set Up Virtual Environment
```bash
git clone https://github.com/Punit-154/interview-prep-tracker-api.git
cd interview-prep-tracker-api

# Create and activate virtual environment
python -m venv .venv

# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1

# On macOS/Linux:
source .venv/bin/activate
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

### 4. Apply Database Migrations & Seed Problems
```bash
python manage.py migrate
python manage.py seed_problems
```
> The `seed_problems` command automatically populates 25 classic interview problems (Two Sum, LRU Cache, Number of Islands, etc.).

### 5. Run Development Server
```bash
python manage.py runserver
```
Visit `http://127.0.0.1:8000/api/docs/` to view and interact with the Swagger documentation.

---

## Running Automated Tests

Run the test suite using Django's runner:
```bash
python manage.py test tracker accounts --verbosity=2
```

Or using `pytest`:
```bash
pytest -v
```

All 19 tests cover:
- Registration, validation, and JWT token issuance
- Read-only protection on problems (405 Method Not Allowed)
- Filtering and search accuracy
- Owner isolation (User A cannot view, edit, or delete User B's attempts)
- Unique constraint enforcement
- ORM `annotate()` and `Count()` analytics calculations

---

## Deployment (Render)

This repository includes a native **Render Blueprint** (`render.yaml`):

1. Push your repository to GitHub.
2. In the Render Dashboard, click **New > Blueprint**.
3. Select your repository.
4. Render will automatically:
   - Provision a managed **PostgreSQL** database.
   - Run `./build.sh` (installs requirements, collects static files, runs migrations, and seeds problems).
   - Start the **Gunicorn** WSGI web service.
