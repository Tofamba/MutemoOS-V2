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
- [Document Storage (Cloudflare R2)](#document-storage-cloudflare-r2)
- [Deployment on Railway](#deployment-on-railway)
- [Migrating from v1](#migrating-from-v1)
- [API Reference](#api-reference)
- [Role-Based Access Control](#role-based-access-control)
- [Multi-Tenancy](#multi-tenancy)
- [Multi-Firm / Sub-Organisation Model](#multi-firm--sub-organisation-model)
- [Semantic Search](#semantic-search)
- [Known Architecture Debt](#known-architecture-debt)
- [AlertEngine Integration](#alertengine-integration)
- [Contributing](#contributing)
- [Contact](#contact)

---

## What It Does

| Feature | Description |
|---|---|
| **Matter Management** | Create, track, and manage legal matters with status, client info, and internal references |
| **Conflict-of-Interest Checking** | New matters are checked against every existing matter (including closed ones) for similar names, including near-variations, before creation |
| **Matter Deadline Tracking** | A matter can carry a critical deadline, shown as a colour-coded marker on the matter list and included in the daily reminder email |
| **Progress Notes** | Timestamped notes per matter with AI-powered date detection |
| **Document Upload** | Upload PDFs, Word, and Excel files — OCR runs automatically in the background, with confidence flagging for manual review on low-quality scans |
| **Semantic Search** | Search across firm precedents, legislation, and Zimbabwe case law using vector embeddings, with an honest grounding indicator distinguishing genuinely retrieved sources from general knowledge |
| **Contract Review** | Upload a contract for structured findings (missing clauses, risky terms, compliance concerns), with every quoted finding independently verified against the actual document text before being shown |
| **AI Drafting** | Generate over 20 document types (litigation, conveyancing, commercial agreements, correspondence) with Zimbabwe-specific structural guidance, plus smart precedent matching against the firm's own past documents |
| **ZLR Index** | Upload and search Zimbabwe Law Reports judgments with taxonomy classification |
| **Legal Updates** | Index legislation and statutory instruments from Veritas Zimbabwe, ZimLII, Laws.Africa, and other sources (fed by the companion `mutemo-legal-feed` service) |
| **Court Calendar** | Track hearings, deadlines, and filings with daily email digests, attendee invites (proper Accept/Decline via ICS), and .ics export |
| **Date Extraction** | Upload a court order — AI extracts all actionable dates automatically |
| **Bulk Import** | Import matters from Word or Excel templates |
| **RBAC** | Role-based access: partner, associate, secretary, admin |
| **Multi-tenancy** | Each firm runs on its own isolated instance with `firm_id` on every record |
| **AlertEngine** | Integrated with Tofamba AlertEngine for incident governance and audit trails |

---

## Version History

### v2.1 — July 2026 *(current)*
A substantial round of feature and reliability work, largely driven by real use against live matters.

**Added:**
- **Contract Review** — structured, two-stage findings (flag, then independently verify each quoted claim against the actual document text before showing it)
- **Draft Document backend** — the frontend's 20+ document-type drafting workflow existed with no backend behind it; built `/api/generate-document` with Zimbabwe-specific guidance per type, including a new Vehicle/Equipment sale type covering everything from a single car to industrial/mining plant
- **Smart precedent matching** — replaced manual matter-browsing for drafting precedent with a real search combining document-type matching and semantic similarity against the firm's own vault
- **Conflict-of-interest checking** — fuzzy name matching (including shared-word/surname detection) against all existing matters, including closed ones, surfaced live when creating a new matter
- **Matter-level deadline tracking** — critical deadlines now live on the matter itself, shown as a colour-coded chip on the matter list and folded into the existing daily reminder email
- **Daily vault digest** — a separate daily email summarising new news, legislation, and judgments added to the vault
- **OCR confidence flagging** — low-confidence OCR (e.g. a poor phone photo) is now flagged for manual review rather than silently trusted downstream
- **Authentication overhaul** — see below; Twilio SMS OTP replaced

**Fixed:**
- **ChromaDB was not persistent** — the vector index lived on the container's ephemeral filesystem and was silently wiped on every redeploy, even though Postgres (the real source of truth for chunk text) survived. Added a persistent volume plus a startup reconciliation check that rebuilds only what's actually out of sync, rather than a full reindex on every boot
- **Grounding-warning UI was dead code** — the frontend had a "no sources found" warning that no backend endpoint had ever actually populated, since it was originally built. A well-grounded and a zero-source answer were visually indistinguishable. Now genuinely wired up
- **Search Vault responses were being truncated mid-sentence** — a token limit that was fine for short answers was too low for a thorough document review; raised, and a `stop_reason` check now flags truncation explicitly if it ever happens again rather than silently returning an incomplete answer
- **Legal feed content-extraction bug** — a content filter meant to strip navigation/boilerplate from scraped pages was instead scoring the boilerplate higher than the actual article, causing every scraped item in a run to end up with near-identical, wrong content. Switched to raw content extraction with the model instructed to identify the real heading directly

### v2.0 — June 2026
The production rebuild. Every architectural decision in v2 was driven by lessons from the v1 pilot with Sawyer & Mkushi Legal Practitioners, Harare.

**What changed:**

| Area | v1 | v2 |
|---|---|---|
| **Database** | JSON file (`mutemo_state.json`) | PostgreSQL via asyncpg |
| **Multi-tenancy** | Single firm, hardcoded | `firm_id` on every table |
| **Authentication** | Password env var (`MUTEMO_PASSWORD`) | OTP via SMS (Twilio) + session cookies in DB *(superseded in v2.1 — see above)* |
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
| **Vector store** | ChromaDB (persistent volume — see [Known Architecture Debt](#known-architecture-debt) history on why this must be explicitly mounted) |
| **Document storage** | Cloudflare R2 (S3-compatible) — see [below](#document-storage-cloudflare-r2) |
| **OCR** | Tesseract + pdftoppm (poppler-utils), with confidence scoring |
| **Legacy .doc** | antiword |
| **DOCX export** | Node.js 20 + docx ^8.5.0 / ^9.x |
| **Email** | Resend API |
| **OTP delivery** | WhatsApp (Meta Business Cloud API), falling back to email automatically if not configured |
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
| `CHROMA_DATA_DIR` | ✓ (production) | Persistent path for the ChromaDB vector index — **must point at a mounted volume**. Without this, the index lives on the container's ephemeral filesystem and is silently lost on every redeploy (see v2.1 fix above). |
| `MUTEMO_FIRM_NAME` | | Display name of the firm (default: Sawyer & Mkushi Legal Practitioners) |
| `MUTEMO_FIRM_CITY` | | City and country (default: Harare, Zimbabwe) |
| `MUTEMO_FIRM_ID` | | Firm UUID. Change this for each new client deployment. Default: `a1b2c3d4-0000-0000-0000-000000000001` |
| `MUTEMO_ADMIN_TOKEN` | | Protects `/api/admin/*` endpoints, and is what `mutemo-legal-feed` uses to push content in. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | | OTP login delivery via Meta Business Cloud API (preferred channel — see Authentication note below) |
| `WHATSAPP_OTP_TEMPLATE_NAME`, `WHATSAPP_OTP_TEMPLATE_LANG` | | Name/language of the approved Meta AUTHENTICATION-category message template used for OTP delivery |
| `RESEND_API_KEY` | | Email — calendar invites, reminders, daily digest, and OTP fallback if WhatsApp isn't configured |
| `RESEND_FROM` | | Sender address used for Resend email |
| `SMTP_HOST` | | Alternative email delivery path, if not using Resend |
| `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | | Cloudflare R2 document storage — see [below](#document-storage-cloudflare-r2) |
| `R2_BUCKET` | | R2 bucket name (defaults to `mutemoos-documents` if unset) |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCESS_APP_ID` | | Syncing invited users to Cloudflare Access automatically on invite |

> **Note on authentication (updated in v2.1):** Twilio SMS has been removed entirely. OTP delivery now prefers WhatsApp if `WHATSAPP_ACCESS_TOKEN`/`WHATSAPP_PHONE_NUMBER_ID` are set, and falls back to email via Resend automatically otherwise — no code change is needed to switch once WhatsApp credentials are added. Account creation is invite-gated: a phone number must match a pending, unaccepted invite (or an existing active user) to receive a code at all — this replaced the previous static `MUTEMO_ALLOWED_PHONES` allowlist, which is no longer used. If **neither** WhatsApp nor email is configured, auth is disabled and the app falls back to a synthetic development user — this is a dev/demo fallback only and must not be relied on for a deployment holding real client data.

---

## Document Storage (Cloudflare R2)

Original uploaded files (PDFs, Word documents) are stored in Cloudflare R2 — an S3-compatible object store — separately from the extracted text, which is chunked and indexed in PostgreSQL/ChromaDB for search. R2 holds the file for viewing/downloading the original; it is not used for search.

- Objects are keyed as `{firm_id}/{matter_id}/{doc_id}/{filename}`
- Viewing a document generates a short-lived (1 hour) presigned URL on demand — the app never proxies file bytes directly
- If R2 isn't configured, uploads still work (text extraction/search is unaffected), but the "view original file" action will not be available for documents uploaded while it was disabled
- Required environment variables: `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`

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

### Step 3 — Add a persistent volume for ChromaDB
1. Go to your MutemoOS-V2 service → **Volumes**
2. **Add Volume** → mount path of your choice (e.g. `/data`)
3. Set `CHROMA_DATA_DIR` to a path inside that mount (e.g. `/data/chroma`) — this is required as of v2.1; without it the vector index is lost on every redeploy

### Step 4 — Set environment variables
Go to your service → **Variables** and add the values from `.env.example`.
Minimum required for production:
```
ANTHROPIC_API_KEY
MUTEMO_FIRM_NAME
MUTEMO_FIRM_ID
MUTEMO_ADMIN_TOKEN
CHROMA_DATA_DIR
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

As of v2.1, this reconciliation also runs automatically on every startup — comparing chunk counts between Postgres and ChromaDB and only rebuilding what's actually out of sync, so a fresh/reset volume self-heals without manual intervention.

---

## API Reference

### Authentication
| Method | Path | Description |
|---|---|---|
| POST | `/api/auth/request-otp` | Request a login code — WhatsApp if configured, email otherwise. Requires the phone to match a pending invite or existing user; silently no-ops for unrecognised numbers |
| POST | `/api/auth/verify-otp` | Verify code, set session cookie. Provisions a new account from the matching invite on first successful login |
| POST | `/api/auth/logout` | Clear session |
| GET | `/api/auth/status` | Check current session |

### Matters
| Method | Path | Description |
|---|---|---|
| GET | `/api/matters` | List all matters |
| POST | `/api/matters` | Create matter (accepts optional `next_deadline` / `next_deadline_note`) |
| PATCH | `/api/matters/{id}` | Update matter, including deadline fields |
| DELETE | `/api/matters/{id}` | Delete matter |
| GET | `/api/matters/check-conflict` | Fuzzy-match a proposed matter name/client against all existing matters (including closed) — *added v2.1* |
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
| GET | `/api/documents/{id}/text` | Get the document's extracted text, reconstructed from its indexed chunks — used for real precedent loading in drafting — *added v2.1* |
| GET | `/api/documents/{id}/view-url` | Get a short-lived presigned R2 URL to view/download the original file |
| GET | `/api/documents/find-precedents` | Find real, similar existing documents by type + semantic similarity for drafting — *added v2.1* |

### Search & AI
| Method | Path | Description |
|---|---|---|
| POST | `/api/search` | Semantic search across all sources |
| POST | `/api/search/document` | Semantic search with an attached document for a grounded opinion on that document specifically — *added v2.1* |
| POST | `/api/contract-review` | Structured contract review with quote-verified findings — *added v2.1* |
| POST | `/api/generate-affidavit` | Generate Zimbabwe High Court affidavit |
| POST | `/api/generate-document` | Generate one of 20+ document types with Zimbabwe-specific guidance — *added v2.1* |
| POST | `/api/export-docx` | Export a document as Word |
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
| PATCH | `/api/calendar/{id}` | Update/reschedule an event, sending an updated notice to attendees |
| POST | `/api/calendar/{id}/invite` | Send/resend an attendee invite with a proper ICS attachment |
| DELETE | `/api/calendar/{id}` | Delete event, sending a cancellation notice to attendees |
| GET | `/api/calendar/export-ics` | Export as .ics file |

### Reminders & Digest
| Method | Path | Description |
|---|---|---|
| GET | `/api/reminders/settings` | Get reminder settings |
| POST | `/api/reminders/settings` | Update reminder settings |
| POST | `/api/reminders/test` | Send test reminder email |
| POST | `/api/reminders/inactivity-check` | Alert on matters inactive 14+ days |
| GET | `/api/digest/settings` | Get daily vault digest settings — *added v2.1* |
| POST | `/api/digest/settings` | Update daily vault digest settings — *added v2.1* |
| POST | `/api/digest/test` | Send test digest email — *added v2.1* |

### Admin & Users
| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | System status and dependency check |
| GET | `/api/users` | List users (admin only) |
| PATCH | `/api/users/{id}` | Update user role, display name, active status, email, or phone (admin only) — email/phone editing *added v2.1* |
| POST | `/api/admin/invite` | Invite a new user by phone + email — the phone is what actually gates account creation |
| GET | `/api/settings` | Get firm settings |
| PATCH | `/api/settings` | Update firm settings |
| POST | `/api/admin/reindex` | Rebuild ChromaDB from PostgreSQL chunks |

---

## Role-Based Access Control

| Permission | admin | partner | associate | secretary |
|---|:---:|:---:|:---:|:---:|
| Read matters (incl. conflict checking) | ✓ | ✓ | ✓ | ✓ |
| Create/edit matters (incl. deadlines) | ✓ | ✓ | ✓ | ✓ |
| Delete matters | ✓ | ✓ | | |
| Upload documents | ✓ | ✓ | ✓ | ✓ |
| Delete documents | ✓ | ✓ | | |
| Add notes | ✓ | ✓ | ✓ | ✓ |
| Delete notes | ✓ | ✓ | | |
| Draft documents / affidavits / Contract Review | ✓ | ✓ | ✓ | |
| Upload legislation/ZLR | ✓ | ✓ | ✓ | |
| Delete legislation/ZLR | ✓ | ✓ | | |
| Search | ✓ | ✓ | ✓ | ✓ |
| Calendar (read/create) | ✓ | ✓ | ✓ | ✓ |
| Calendar (delete) | ✓ | ✓ | ✓ | |
| Settings | ✓ | ✓ | | |
| User management (incl. invites) | ✓ | | | |

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

> The `mutemo-legal-feed` companion service already has example configuration for a second firm (Legal Corner) documented in `pusher.py`'s `FIRM_2_*` variables — worth checking there before assuming this needs to be set up from scratch.

---

## Multi-Firm / Sub-Organisation Model

Separate from multi-tenancy above: a single firm instance can have an internal structure beyond simple staff roles. This matters specifically for firms like **Legal Corner**, which has a panel of affiliated lawyers alongside firm operations staff.

- Standard per-user roles (`admin`, `partner`, `associate`, `secretary`) live on the `users` table and control what a user can do in the system, per the RBAC table above
- A separate `organisation_roles` table layers an additional, optional role — `ops_manager` or `panel_lawyer` — on top of a user's standard role, for firms that need this distinction
- This is set at invite time (`organisation_role` field on the invite) and does not require a separate firm/tenant — it's a within-firm distinction, not a multi-tenant one

---

## Semantic Search

Documents uploaded to Mutemo Desk are automatically chunked and embedded using `all-MiniLM-L6-v2`. Embeddings are stored in ChromaDB on the persistent volume (see [Environment Variables](#environment-variables) — `CHROMA_DATA_DIR` is required for this to actually persist across deployments).

Three separate collections are maintained:

| Collection | Contents |
|---|---|
| `firm_precedents` | Documents uploaded to matters |
| `legal_updates` | Legislation, statutory instruments, and news |
| `zlr_index` | Zimbabwe case law judgments |

Search queries all relevant collections simultaneously and synthesises a single answer using Claude `claude-sonnet-4-5`, with an honest indicator of whether the answer is genuinely grounded in retrieved sources or reflects general knowledge with nothing specific found.

If ChromaDB is unavailable, the system falls back to keyword search automatically.

---

## Known Architecture Debt

`main.py` is a single large file (several thousand lines) covering routing, business logic, and data access together. Splitting this into `/routers`, `/services`, `/models` has been discussed and **deliberately deferred**, pending a planned PostgreSQL schema migration to a proper workspace model — the split is intended to happen alongside that migration rather than twice. This is a known, accepted tradeoff, not an oversight.

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
