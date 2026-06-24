# Mutemo Desk v2.0

**Zimbabwe Legal Practice Operating System**  
Built by [Tofamba Technology](https://tofamba.com), Harare, Zimbabwe.

> Mutemo Desk is a practice management platform built specifically for Zimbabwe law firms. It combines matter management, AI-assisted legal drafting, semantic search across firm precedents and case law, court calendar tracking, and automated daily digests — all in one system.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Version History](#version-history)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Getting Started](#getting-started)
- [Environment Variables](#environment-variables)
- [Deployment on Railway](#deployment-on-railway)
- [Migrating from v1](#migrating-from-v1)
- [API Reference](#api-reference)
- [Role-Based Access Control](#role-based-access-control)
- [Multi-Tenancy](#multi-tenancy)
- [Semantic Search](#semantic-search)
- [AlertEngine Integration](#alertengine-integration)
- [Contributing](#contributing)
- [Contact](#contact)

---

## What It Does

| Feature | Description |
|---|---|
| **Matter Management** | Create, track, and manage legal matters with status, client info, and internal references |
| **Progress Notes** | Timestamped notes per matter with AI-powered date detection |
| **Document Upload** | Upload PDFs, Word, and Excel files — OCR runs automatically in the background |
| **Semantic Search** | Search across firm precedents, legislation, and Zimbabwe case law using vector embeddings |
| **AI Drafting** | Generate Zimbabwe High Court affidavits using Claude, export as formatted Word documents |
| **ZLR Index** | Upload and search Zimbabwe Law Reports judgments with taxonomy classification |
| **Legal Updates** | Index legislation and statutory instruments from Veritas Zimbabwe and other sources |
| **Court Calendar** | Track hearings, deadlines, and filings with daily email digests and .ics export |
| **Date Extraction** | Upload a court order — AI extracts all actionable dates automatically |
| **Bulk Import** | Import matters from Word or Excel templates |
| **RBAC** | Role-based access: partner, associate, secretary, admin |
| **Multi-tenancy** | Each firm runs on its own isolated instance with `firm_id` on every record |
| **AlertEngine** | Integrated with Tofamba AlertEngine for incident governance and audit trails |

---

## Version History

### v2.0 — June 2026 *(current)*
The production rebuild. Every architectural decision in v2 was driven by lessons from the v1 pilot with Sawyer & Mkushi Legal Practitioners, Harare.

**What changed:**

| Area | v1 | v2 |
|---|---|---|
| **Database** | JSON file (`mutemo_state.json`) | PostgreSQL via asyncpg |
| **Multi-tenancy** | Single firm, hardcoded | `firm_id` on every table |
| **Authentication** | Password env var (`MUTEMO_PASSWORD`) | OTP via SMS (Twilio) + session cookies in DB |
| **Roles** | None — all users equal | `partner / associate / secretary / admin` |
| **OCR** | Synchronous — blocked the request | Background task — returns 202 immediately |
| **Legacy .doc support** | python-docx only (failed on binary .doc) | antiword fallback for legacy Word files |
| **Session storage** | In-memory dict (lost on restart) | PostgreSQL sessions table |
| **OTP storage** | In-memory dict (lost on restart) | PostgreSQL otp_store table |
| **Document status** | No visibility during processing | `/api/documents/{id}/status` polling endpoint |
| **Health check** | Basic | Reports DB, Tesseract, antiword, Node, embeddings |

**What stayed the same:**
- ChromaDB for vector storage (persisted on Railway volume)
- sentence-transformers `all-MiniLM-L6-v2` for embeddings
- Claude `claude-sonnet-4-5` for drafting and search synthesis
- Claude `claude-haiku-4-5` for document classification and note scanning
- Resend API for email (Railway blocks SMTP ports)
- Node.js + docx package for Word document export

---

### v1.1 — March 2026
- Added WhatsApp delivery via Meta Cloud API (bypassing Twilio, which had Zimbabwe coverage gaps)
- Cloudflare Access with OTP email login replacing password auth
- Bulk matter import from Word and Excel templates
- Extract Dates from court orders
- Progress notes with AI date detection
- Matter dashboard
- ICS calendar attachments in reminder emails
- ChromaDB semantic search
- ZLR subject index with taxonomy classification

### v1.0 — December 2025
Initial pilot deployment for Sawyer & Mkushi Legal Practitioners, Harare.
- Matter management (in-memory JSON)
- Document upload with OCR (Tesseract + pdftoppm)
- AI affidavit generator (Claude)
- Precedent Vault with keyword search
- Court Calendar with email reminders
- Legal Updates tab (manual upload)
- DOCX export via Node.js

---

## Architecture

```
MutemoOS-V2/
├── backend/
│   └── main.py              # FastAPI application — all routes and business logic
├── frontend/
│   └── index.html           # Single-page frontend (served by FastAPI)
├── scripts/
│   ├── migrate_to_postgres.py   # Migration script: v1 JSON → PostgreSQL
│   └── postgres_schema.sql      # Schema reference (migrations run automatically on startup)
├── data/                    # Railway persistent volume mount point
│   └── chroma/              # ChromaDB vector store (auto-created)
├── Dockerfile
├── requirements.txt
├── railway.toml
└── .env.example
```

**Request flow:**
```
Client → FastAPI → session_auth_middleware → RBAC check → handler
                                                              ↓
                                                    PostgreSQL (asyncpg)
                                                              ↓
                                              BackgroundTasks (OCR, chunking)
                                                              ↓
                                                    ChromaDB (embeddings)
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| **API** | FastAPI 0.115 + uvicorn |
| **Database** | PostgreSQL (asyncpg connection pool) |
| **AI** | Anthropic Claude (sonnet-4-5, haiku-4-5) |
| **Embeddings** | sentence-transformers `all-MiniLM-L6-v2` |
| **Vector store** | ChromaDB (persistent volume) |
| **OCR** | Tesseract + pdftoppm (poppler-utils) |
| **Legacy .doc** | antiword |
| **DOCX export** | Node.js 20 + docx ^8.5.0 |
| **Email** | Resend API |
| **Deployment** | Railway (Docker) |
| **Governance** | Tofamba AlertEngine |

---

## Getting Started

### Prerequisites

- Python 3.11+
- Node.js 20+
- PostgreSQL 14+
- Tesseract OCR
- poppler-utils (pdftoppm)
- antiword

### Local Development

```bash
# Clone the repo
git clone https://github.com/Tofamba/MutemoOS-V2.git
cd MutemoOS-V2

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy and fill in environment variables
cp .env.example .env

# Run the app
uvicorn backend.main:app --reload --port 8000
```

The app will be available at `http://localhost:8000`.  
On first startup, `run_migrations()` creates all tables automatically.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in the values.

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✓ | PostgreSQL connection string. Railway injects this automatically when you add the PostgreSQL plugin. |
| `ANTHROPIC_API_KEY` | ✓ | Claude API key from console.anthropic.com |
| `MUTEMO_FIRM_NAME` | | Display name of the firm (default: Sawyer & Mkushi Legal Practitioners) |
| `MUTEMO_FIRM_CITY` | | City and country (default: Harare, Zimbabwe) |
| `MUTEMO_FIRM_ID` | | Firm UUID. Change this for each new client deployment. Default: `a1b2c3d4-0000-0000-0000-000000000001` |
| `MUTEMO_ADMIN_TOKEN` | | Protects `/api/admin/*` endpoints. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `TWILIO_ACCOUNT_SID` | | OTP SMS authentication. Leave blank to run in open mode (all requests treated as partner). |
| `TWILIO_AUTH_TOKEN` | | OTP SMS |
| `TWILIO_FROM_NUMBER` | | OTP SMS sender (E.164 format) |
| `MUTEMO_ALLOWED_PHONES` | | Comma-separated E.164 numbers allowed to log in |
| `RESEND_API_KEY` | | Email reminders via Resend API |
| `RESEND_FROM_DOMAIN` | | Sender domain (default: tofamba.com) |

> **Note on AUTH_ENABLED:** OTP authentication is only active when all four Twilio variables are set. Without them the app runs in open mode — useful for development and initial testing, but not for production.

---

## Deployment on Railway

### Step 1 — Connect the repo
1. Go to your Railway project
2. **New Service** → **Deploy from GitHub repo**
3. Select `Tofamba/MutemoOS-V2`
4. Railway detects the Dockerfile automatically

### Step 2 — Add PostgreSQL
1. In the same Railway project → **New** → **Database** → **PostgreSQL**
2. Railway injects `DATABASE_URL` into your service automatically
3. No configuration needed — `run_migrations()` creates all tables on first startup

### Step 3 — Add persistent volume
1. Go to your MutemoOS-V2 service → **Volumes**
2. **Add Volume** → mount path: `/app/data`
3. This persists ChromaDB vector data across deployments

### Step 4 — Set environment variables
Go to your service → **Variables** and add the values from `.env.example`.  
Minimum required for production:
```
ANTHROPIC_API_KEY
MUTEMO_FIRM_NAME
MUTEMO_FIRM_ID
MUTEMO_ADMIN_TOKEN
RESEND_API_KEY
```

### Step 5 — Set a custom subdomain
1. Go to your service → **Settings** → **Networking**
2. Click **Generate Domain** for a Railway subdomain
3. Or add a custom domain (e.g. `app.tofamba.com`) by adding a CNAME record

### Step 6 — Verify deployment
```bash
curl https://your-app.up.railway.app/api/health
```

Expected response:
```json
{
  "status": "ok",
  "version": "2.0.0",
  "service": "Mutemo Desk",
  "dependencies": {
    "anthropic_key": true,
    "database": true,
    "tesseract": true,
    "pdftoppm": true,
    "antiword": true,
    "node": true,
    "smtp_configured": true,
    "semantic_search": true
  }
}
```

---

## Migrating from v1

If you have an existing `mutemo_state.json` from v1:

```bash
# Dry run first — see what will be migrated without writing anything
DATABASE_URL=postgresql://... python scripts/migrate_to_postgres.py \
  --state /path/to/mutemo_state.json \
  --dry-run

# Apply the migration
DATABASE_URL=postgresql://... python scripts/migrate_to_postgres.py \
  --state /path/to/mutemo_state.json
```

The migration script is **idempotent** — safe to run multiple times. UUIDs are generated deterministically from internal refs so re-runs do not create duplicates.

**ChromaDB vector data** on the persistent volume is preserved as-is. If you need to rebuild the vector index from the PostgreSQL chunks:

```bash
curl -X POST https://your-app.up.railway.app/api/admin/reindex \
  -H "X-Admin-Token: your-admin-token"
```

---

## API Reference

### Authentication
| Method | Path | Description |
|---|---|---|
| POST | `/api/auth/request-otp` | Request SMS login code |
| POST | `/api/auth/verify-otp` | Verify code, set session cookie |
| POST | `/api/auth/logout` | Clear session |
| GET | `/api/auth/status` | Check current session |

### Matters
| Method | Path | Description |
|---|---|---|
| GET | `/api/matters` | List all matters |
| POST | `/api/matters` | Create matter |
| PATCH | `/api/matters/{id}` | Update matter |
| DELETE | `/api/matters/{id}` | Delete matter |
| POST | `/api/matters/{id}/notes` | Add progress note (AI date scan included) |
| DELETE | `/api/matters/{id}/notes/{note_id}` | Delete note |
| POST | `/api/matters/bulk-import` | Import from Word/Excel template |
| GET | `/api/matters/template` | Download Word import template |
| GET | `/api/matters/template-excel` | Download Excel import template |

### Documents
| Method | Path | Description |
|---|---|---|
| GET | `/api/matters/{id}/documents` | List documents for a matter |
| POST | `/api/upload` | Upload document (returns 202, processes in background) |
| GET | `/api/documents/{id}/status` | Poll background processing status |

### Search & AI
| Method | Path | Description |
|---|---|---|
| POST | `/api/search` | Semantic search across all sources |
| POST | `/api/generate-affidavit` | Generate Zimbabwe High Court affidavit |
| POST | `/api/export-docx` | Export affidavit as Word document |
| POST | `/api/extract-dates` | Extract actionable dates from uploaded document |

### Case Law (ZLR)
| Method | Path | Description |
|---|---|---|
| GET | `/api/zlr` | List ZLR entries |
| GET | `/api/zlr/categories` | Taxonomy breakdown |
| POST | `/api/zlr/upload` | Upload judgment (returns 202, processes in background) |
| POST | `/api/zlr/bulk-import` | Bulk import ZLR subject index |
| POST | `/api/zlr/search` | Search case law |
| DELETE | `/api/zlr/{id}` | Delete entry |

### Legal Updates
| Method | Path | Description |
|---|---|---|
| GET | `/api/legal-updates` | List legislation and SIs |
| POST | `/api/legal-updates/upload` | Upload legislation (returns 202) |
| POST | `/api/legal-updates/search` | Search legislation |
| DELETE | `/api/legal-updates/{id}` | Delete entry |

### Calendar
| Method | Path | Description |
|---|---|---|
| GET | `/api/calendar` | List events |
| POST | `/api/calendar` | Add event |
| DELETE | `/api/calendar/{id}` | Delete event |
| GET | `/api/calendar/export-ics` | Export as .ics file |

### Reminders & Alerts
| Method | Path | Description |
|---|---|---|
| GET | `/api/reminders/settings` | Get reminder settings |
| POST | `/api/reminders/settings` | Update reminder settings |
| POST | `/api/reminders/test` | Send test email |
| POST | `/api/reminders/inactivity-check` | Alert on matters inactive 14+ days |

### Admin
| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | System status and dependency check |
| GET | `/api/users` | List users (admin only) |
| PATCH | `/api/users/{id}` | Update user role (admin only) |
| GET | `/api/settings` | Get firm settings |
| PATCH | `/api/settings` | Update firm settings |
| POST | `/api/admin/reindex` | Rebuild ChromaDB from PostgreSQL chunks |

---

## Role-Based Access Control

| Permission | admin | partner | associate | secretary |
|---|:---:|:---:|:---:|:---:|
| Read matters | ✓ | ✓ | ✓ | ✓ |
| Create/edit matters | ✓ | ✓ | ✓ | ✓ |
| Delete matters | ✓ | ✓ | | |
| Upload documents | ✓ | ✓ | ✓ | ✓ |
| Delete documents | ✓ | ✓ | | |
| Add notes | ✓ | ✓ | ✓ | ✓ |
| Delete notes | ✓ | ✓ | | |
| Draft affidavits | ✓ | ✓ | ✓ | |
| Upload legislation/ZLR | ✓ | ✓ | ✓ | |
| Delete legislation/ZLR | ✓ | ✓ | | |
| Search | ✓ | ✓ | ✓ | ✓ |
| Calendar (read/create) | ✓ | ✓ | ✓ | ✓ |
| Calendar (delete) | ✓ | ✓ | ✓ | |
| Settings | ✓ | ✓ | | |
| User management | ✓ | | | |

---

## Multi-Tenancy

MutemoOS v2 uses **instance-level multi-tenancy** — each client firm runs on its own Railway service with its own PostgreSQL database and persistent volume.

To deploy a second firm:
1. Create a new Railway service from the same repo
2. Add a new PostgreSQL plugin to that service
3. Set `MUTEMO_FIRM_ID` to a new UUID: `python3 -c "import uuid; print(uuid.uuid4())"`
4. Set `MUTEMO_FIRM_NAME` to the new firm name
5. Deploy

The two instances share no data. Each has its own database, vector store, and subdomain.

---

## Semantic Search

Documents uploaded to Mutemo Desk are automatically chunked (500 words, 50-word overlap) and embedded using `all-MiniLM-L6-v2`. Embeddings are stored in ChromaDB on the persistent volume.

Three separate collections are maintained:

| Collection | Contents |
|---|---|
| `firm_precedents` | Documents uploaded to matters |
| `legal_updates` | Legislation and statutory instruments |
| `zlr_index` | Zimbabwe case law judgments |

Search queries all three collections simultaneously and synthesise a single answer using Claude `claude-sonnet-4-5`.

If ChromaDB is unavailable, the system falls back to keyword search automatically.

---

## AlertEngine Integration

Mutemo Desk is instrumented with [Tofamba AlertEngine](https://github.com/Tofamba/AlertEngine) via the `fastapi-alertengine` package.

```python
from fastapi_alertengine import instrument
instrument(app)
```

AlertEngine provides:
- Incident detection and governance for the FastAPI application
- Human-in-the-loop authorization before any recovery action executes
- Shadow Mode for safe evaluation without external side effects
- Audit trail for all automated decisions

If `fastapi-alertengine` is not installed, the app starts normally without instrumentation.

---

## Contributing

Mutemo Desk is a proprietary product of Tofamba Technology. It is not open source.

If you are a developer working on this codebase under contract:

1. Branch from `main` — never commit directly to main
2. Branch naming: `feat/your-feature`, `fix/your-fix`, `chore/your-task`
3. All API changes must update this README
4. Test locally against a real PostgreSQL instance before pushing
5. The migration script must remain idempotent

---

## Contact

**Tofamba Technology**  
Harare, Zimbabwe  
tofambatech@outlook.com  
X: [@leoofharare](https://x.com/leoofharare)  
Dev.to: [@tandemmedia](https://dev.to/tandemmedia)

---

*Built in Harare. For Zimbabwe law firms.*
