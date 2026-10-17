# Reco.ai — AI-Powered Finance Reconciliation

> Automated reconciliation across Razorpay settlements, bank statements, and internal ledgers. Deterministic matching first, AI for the hard cases, humans always in control.

---

## What it does

Finance teams waste hours manually matching settlement IDs across systems. Reco.ai automates this with a three-layer approach:

1. **Deterministic matching** — exact UTR + amount, date-window heuristics, and ledger equations handle ~75% of cases instantly
2. **AI classification** — Groq-powered LLM classifies unmatched residue into actionable categories (fee deltas, refund lags, duplicates, etc.)
3. **Human approval** — every AI decision requires human review with full audit trail

**Result:** 75 auto-matched, 25 exceptions surfaced, all classified .

## Key Features

- **Razorpay API integration** — fetch settlements live from Razorpay's API (test mode)
- **Smart upload pipeline** — select files and hit "Run pipeline" — uploads, reconciles, and classifies in one click
- **4 difficulty test sets** — clean (15% issues), balanced (40%), messy (65%), stress (80%)
- **AI hypothesis cards** — plain-language explanations with resolved evidence (external ID, amount, date)
- **Append-only audit log** — every approve/reject/override preserves the AI hypothesis and human reason
- **CSV export** — export exception list with full context for external review
- **Live evaluation metrics** — deterministic match rate, exception recall, AI accuracy, faithfulness score

## How it works

```
┌─────────────────────────────────────────────────────────┐
│                    DATA INGESTION                        │
│  Razorpay API ──→ Settlements                           │
│  Bank CSV     ──→ Bank statements                       │
│  Ledger CSV   ──→ Internal ledger entries                │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│               DETERMINISTIC MATCHING                     │
│  Step 1: EXACT_UTR_AMOUNT     (unique UTR + amount)    │
│  Step 2: AMOUNT_DATE_WINDOW   (±2 day heuristics)      │
│  Step 3: LEDGER_NET_EXACT     (gross - fee - tax = net) │
│                                                         │
│  ~75% auto-matched ──→ Match Groups                     │
│  ~25% residue      ──→ Exceptions                       │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│                 AI CLASSIFICATION                        │
│  Groq LLM (or offline FakeClassifier)                   │
│  Categories: FEE_DELTA | REFUND_LAG | DUPLICATE_UTR     │
│              UNRECOGNIZED_CREDIT | AMBIGUOUS_MATCH       │
│                                                         │
│  Pydantic-validated JSON output                         │
│  Plain-language explanations + resolved evidence         │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│                  HUMAN REVIEW                            │
│  Approve  ──→ exception resolved                        │
│  Reject   ──→ exception closed with reason              │
│  Override ──→ reclassify with human judgment             │
│                                                         │
│  Full audit trail: who, what, when, AI hypothesis       │
└─────────────────────────────────────────────────────────┘
```

## Quick Start

```bash
# One command — full demo with synthetic data
python scripts/run_demo.py --skip-docker

# Blank slate — upload your own files
python scripts/run_demo.py --clean --skip-docker

# Razorpay live — auto-fetch from API
python scripts/run_demo.py --razorpay --skip-docker
```

**Prerequisites:** Docker (for Postgres), Python 3.12+, Node 22+

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI, SQLAlchemy 2.x, Alembic, Pydantic, Pandas |
| Database | PostgreSQL 16 |
| Frontend | Next.js 16, Tailwind CSS, shadcn/ui, lucide-react |
| AI | Groq (llama-3.3-70b-versatile), behind `ExceptionClassifier` interface |
| API | Razorpay Settlements API (test mode) |

## Testing

```bash
cd backend && python -m pytest tests -v    # 98 tests, no live API calls
cd web && npm run lint && npm run build
```

CI-safe: webhooks tested with local HMAC signatures, LLM uses `FakeExceptionClassifier`.

## Evaluation Metrics

| Metric | What it measures |
|--------|-----------------|
| Deterministic Match Rate | Correct auto-matches / total auto-matches |
| Deterministic Coverage | Auto-matches / eligible cases |
| Exception Recall | Known exceptions surfaced / total known |
| AI Classification Accuracy | Correct AI labels / AI-classified cases |
| LLM Faithfulness Score | Evidence-support across explanations |

Ground truth is isolated in `fixtures/synthetic/ground_truth.json` — never leaked to prompts or matchers.

## Repository Layout

```
backend/app          FastAPI app (routers, models, services)
backend/alembic      Database migrations
web                  Next.js dashboard
fixtures/synthetic   Demo batch (100 cases) + ground_truth.json
fixtures/test_sets   Clean / balanced / messy / stress difficulty sets
scripts              run_demo.py · generate_test_sets.py · reset_demo_data.py
docs/deployment.md   Vercel + Render + Supabase deploy guide
```

## License

Demo project — no production use. All data is synthetic.
