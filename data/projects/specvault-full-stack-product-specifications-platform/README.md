# SpecVault

A full-stack product specifications platform with user authentication, searchable product catalog, and pagination.

![SpecVault](https://img.shields.io/badge/Status-Deployed-green) ![Python](https://img.shields.io/badge/Python-3.11+-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688) ![Next.js](https://img.shields.io/badge/Next.js-14-black) ![Supabase](https://img.shields.io/badge/Supabase-PostgreSQL-3FCF8E)

---

## Live Demo

- **Frontend:** [spec-vault-two.vercel.app](https://spec-vault-two.vercel.app/)
- **API Docs:** [specvault-pqq1.onrender.com/docs](https://specvault-pqq1.onrender.com/docs)

---

## Features

- User authentication (signup, login, JWT sessions via Supabase Auth)
- Full CRUD for product specifications
- Real-time name search
- Paginated product list (10 per page)
- Row-Level Security (users can only access their own products)
- Responsive design with Tailwind CSS
- Loading skeletons, empty states, error handling
- 10+ backend tests with pytest
- Interactive API documentation (Swagger UI)

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI, Pydantic v2 |
| Database | PostgreSQL via Supabase |
| Auth | Supabase Auth (JWT, JWKS verification) |
| Frontend | Next.js 14, React 18, TypeScript, Tailwind CSS |
| Deployment | Render (API), Vercel (Frontend) |
| Testing | pytest, httpx |

---

## Architecture

```
┌─────────────────────┐     HTTP/JSON     ┌─────────────────────┐
│                     │ ────────────────>  │                     │
│   Frontend          │                    │   Backend           │
│   (Next.js)         │ <────────────────  │   (FastAPI)         │
│   specvault.        │                    │   specvault-api.    │
│   vercel.app        │                    │   render.com        │
└─────────────────────┘                    └──────────┬──────────┘
                                                      │
                                                      │ Supabase Client
                                                      │
                                             ┌────────▼──────────┐
                                             │                   │
                                             │   Supabase        │
                                             │   PostgreSQL      │
                                             │   + Auth + RLS    │
                                             │                   │
                                             └───────────────────┘
```

---

## Local Setup

### Prerequisites

- Python 3.11+
- Node.js 18+
- A [Supabase](https://supabase.com) account (free tier)

### 1. Create Supabase Project

1. Go to [supabase.com](https://supabase.com) → New Project
2. Go to **Settings → API** → Copy **Project URL** and **Publishable Key**
3. Go to **SQL Editor** → Run the schema below

### 2. Database Schema

```sql
CREATE TABLE products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    brand TEXT NOT NULL,
    category TEXT NOT NULL,
    price DECIMAL(10,2) CHECK (price > 0),
    specs JSONB DEFAULT '{}',
    source_url TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_products_owner ON products(owner_id);
CREATE INDEX idx_products_brand ON products(brand);

ALTER TABLE products ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own products"
    ON products FOR SELECT
    USING (auth.uid() = owner_id);

CREATE POLICY "Users can insert own products"
    ON products FOR INSERT
    WITH CHECK (auth.uid() = owner_id);

CREATE POLICY "Users can update own products"
    ON products FOR UPDATE
    USING (auth.uid() = owner_id);

CREATE POLICY "Users can delete own products"
    ON products FOR DELETE
    USING (auth.uid() = owner_id);
```

### 3. Backend Setup

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

Create `backend/.env`:

```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=sb_publishable_xxxxxxxx
```

Run:

```bash
uvicorn src.main:app --reload
```

API docs: http://localhost:8000/docs

### 4. Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

App: http://localhost:3000

---

## API Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/auth/signup` | No | Create account |
| POST | `/api/auth/login` | No | Login |
| POST | `/api/auth/logout` | Yes | Logout |
| GET | `/api/auth/me` | Yes | Get current user |
| POST | `/api/products` | Yes | Create product |
| GET | `/api/products` | No | List products (paginated, searchable) |
| GET | `/api/products/{id}` | No | Get product detail |
| PUT | `/api/products/{id}` | Yes | Update product (owner only) |
| DELETE | `/api/products/{id}` | Yes | Delete product (owner only) |
| GET | `/api/health` | No | Health check |

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | int | 1 | Page number (min: 1) |
| `limit` | int | 10 | Items per page (1-100) |
| `search` | string | - | Search by product name |

---

## Project Structure

```
SpecValut_Ai/
├── backend/
│   ├── src/
│   │   ├── main.py              # App entry point
│   │   ├── config.py            # Environment variables
│   │   ├── database.py          # Supabase client
│   │   ├── models.py            # Pydantic models
│   │   ├── middleware/auth.py    # JWT verification (JWKS)
│   │   └── routers/
│   │       ├── auth.py          # Auth endpoints
│   │       └── products.py      # Product CRUD endpoints
│   └── tests/
│       ├── conftest.py
│       ├── test_health.py
│       └── test_products.py
├── frontend/
│   └── src/
│       ├── app/                 # Pages (file-based routing)
│       │   ├── page.tsx         # Home
│       │   ├── login/page.tsx   # Login
│       │   ├── signup/page.tsx  # Signup
│       │   ├── dashboard/page.tsx # Product list
│       │   └── products/
│       │       ├── new/page.tsx # Create product
│       │       └── [id]/page.tsx # Product detail
│       ├── components/          # Reusable UI
│       └── lib/api.ts           # API client
├── docs/
│   └── database_setup.md        # Database setup guide
└── learner/                     # 35-day learning guide
```

---

## Running Tests

```bash
cd backend
pytest -v
```

---

## Deployment

### Backend (Render)

1. Push to GitHub
2. Create Web Service on [render.com](https://render.com)
3. Build: `pip install -r backend/requirements.txt`
4. Start: `cd backend && uvicorn src.main:app --host 0.0.0.0 --port $PORT`
5. Add env vars: `SUPABASE_URL`, `SUPABASE_KEY`

### Frontend (Vercel)

1. Create `frontend/.env.production`:
   ```
   NEXT_PUBLIC_API_URL=https://your-api.onrender.com
   ```
2. Deploy on [vercel.com](https://vercel.com)

---

## What I Learned

Building SpecVault taught me the full stack: from designing a database schema with PostgreSQL, to building a REST API with FastAPI, to consuming it with a React frontend. The hardest part was understanding authentication — how JWTs work with JWKS verification, how Row-Level Security adds a second layer of protection at the database level, and how CORS controls which websites can talk to which servers.

The most valuable skill I gained was thinking about edge cases: what happens when the database is down? When a user tries to delete someone else's product? When the search returns nothing? These "sad paths" are what separate tutorial projects from production code.

---

## License

MIT
