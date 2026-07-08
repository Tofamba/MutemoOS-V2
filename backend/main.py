"""
Mutemo Desk — Zimbabwe Legal Practice Operating System
FastAPI backend v2.0 — Production
Changes from v1.1:
  - PostgreSQL persistence (asyncpg) replaces JSON file store
  - firm_id on every data model — multi-tenancy foundation
  - Role-based access control: partner | associate | secretary | admin
  - Background OCR via FastAPI BackgroundTasks (all three upload endpoints)
  - OTP/session data migrated to DB (no more in-memory dicts)
  - Admin endpoints protected by X-Admin-Token header
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, Response, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List
from contextlib import asynccontextmanager
import anthropic
import subprocess
import tempfile
import os
import json
import uuid
import re
import asyncio
import secrets
import time
import hmac
from datetime import datetime, timedelta

# ── Load .env file if present ─────────────────────────────────────────────────
def _load_dotenv():
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value

_load_dotenv()

# ── Database ──────────────────────────────────────────────────────────────────
import asyncpg

_db_pool: asyncpg.Pool = None

async def get_db() -> asyncpg.Pool:
    return _db_pool

async def init_db():
    """Create connection pool and run schema migrations."""
    global _db_pool
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is not set. Add the Railway PostgreSQL plugin.")
    # asyncpg requires postgresql:// not postgres://
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    _db_pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    await run_migrations()
    print("[db] PostgreSQL connection pool ready")

async def run_migrations():
    """Idempotent schema creation — safe to run on every startup."""
    async with _db_pool.acquire() as conn:
        await conn.execute("""
        CREATE EXTENSION IF NOT EXISTS pgcrypto;

        CREATE TABLE IF NOT EXISTS firms (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT NOT NULL,
            short_name  TEXT,
            city        TEXT DEFAULT 'Harare',
            country     TEXT DEFAULT 'Zimbabwe',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS users (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            phone           TEXT NOT NULL,
            display_name    TEXT NOT NULL,
            role            TEXT NOT NULL CHECK (role IN ('partner','associate','secretary','admin')),
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (firm_id, phone)
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token       TEXT PRIMARY KEY,
            user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            expires_at  TIMESTAMPTZ NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

        CREATE TABLE IF NOT EXISTS otp_store (
            phone       TEXT PRIMARY KEY,
            code        TEXT NOT NULL,
            attempts    INT NOT NULL DEFAULT 0,
            expires_at  TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS matters (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            name            TEXT NOT NULL,
            number          TEXT,
            internal_ref    TEXT,
            external_ref    TEXT,
            client_name     TEXT,
            matter_type     TEXT,
            status          TEXT NOT NULL DEFAULT 'Active',
            custom_status   TEXT,
            document_count  INT NOT NULL DEFAULT 0,
            last_activity   TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_by      UUID REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_matters_firm ON matters(firm_id);
        CREATE INDEX IF NOT EXISTS idx_matters_status ON matters(firm_id, status);

        CREATE TABLE IF NOT EXISTS progress_notes (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            matter_id   UUID NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
            firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            text        TEXT NOT NULL,
            author      TEXT NOT NULL DEFAULT 'Unknown',
            user_id     UUID REFERENCES users(id),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_notes_matter ON progress_notes(matter_id);

        CREATE TABLE IF NOT EXISTS documents (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            matter_id       UUID NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
            firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            filename        TEXT NOT NULL,
            document_type   TEXT,
            matter_type     TEXT,
            parties         TEXT,
            doc_date        DATE,
            court           TEXT,
            word_count      INT DEFAULT 0,
            page_count      INT DEFAULT 1,
            chunk_count     INT DEFAULT 0,
            ocr_used        BOOLEAN DEFAULT FALSE,
            status          TEXT DEFAULT 'processing',
            error_message   TEXT,
            uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            uploaded_by     UUID REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_documents_matter ON documents(matter_id);
        CREATE INDEX IF NOT EXISTS idx_documents_firm ON documents(firm_id);

        CREATE TABLE IF NOT EXISTS legal_updates (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            filename        TEXT NOT NULL,
            source_type     TEXT,
            source_name     TEXT,
            reference       TEXT,
            document_type   TEXT,
            matter_type     TEXT,
            doc_date        DATE,
            court           TEXT,
            word_count      INT DEFAULT 0,
            chunk_count     INT DEFAULT 0,
            status          TEXT DEFAULT 'processing',
            ocr_used        BOOLEAN DEFAULT FALSE,
            error_message   TEXT,
            uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source_url      TEXT,
            scraped_at      TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_legal_updates_firm ON legal_updates(firm_id);
        -- Migration: add source_url and scraped_at to existing deployments
        ALTER TABLE legal_updates ADD COLUMN IF NOT EXISTS source_url TEXT;
        ALTER TABLE legal_updates ADD COLUMN IF NOT EXISTS scraped_at TIMESTAMPTZ;

        CREATE TABLE IF NOT EXISTS zlr_entries (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id             UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            filename            TEXT,
            source              TEXT DEFAULT 'ZLR',
            jurisdiction        TEXT,
            authority_weight    TEXT,
            volume_year         TEXT,
            zimlii_url          TEXT,
            case_name           TEXT,
            citation            TEXT,
            judgment_number     TEXT,
            court               TEXT,
            judge               TEXT,
            case_type           TEXT,
            hearing_date        TEXT,
            judgment_date       TEXT,
            subject_chains      JSONB DEFAULT '[]',
            taxonomy_category   TEXT DEFAULT 'General',
            summary             TEXT,
            raw_text            TEXT,
            word_count          INT DEFAULT 0,
            chunk_count         INT DEFAULT 0,
            ocr_used            BOOLEAN DEFAULT FALSE,
            uploaded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_zlr_firm ON zlr_entries(firm_id);
        CREATE INDEX IF NOT EXISTS idx_zlr_category ON zlr_entries(firm_id, taxonomy_category);

        CREATE TABLE IF NOT EXISTS calendar_events (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            matter_id   UUID REFERENCES matters(id) ON DELETE SET NULL,
            title       TEXT NOT NULL,
            date        DATE NOT NULL,
            time        TIME,
            event_type  TEXT DEFAULT 'other',
            court       TEXT,
            matter_name TEXT,
            notes       TEXT,
            source      TEXT DEFAULT 'manual',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_by  UUID REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_calendar_firm ON calendar_events(firm_id);
        CREATE INDEX IF NOT EXISTS idx_calendar_date ON calendar_events(firm_id, date);

        CREATE TABLE IF NOT EXISTS reminder_settings (
            firm_id             UUID PRIMARY KEY REFERENCES firms(id) ON DELETE CASCADE,
            enabled             BOOLEAN NOT NULL DEFAULT FALSE,
            recipient_email     TEXT,
            send_hour_utc       INT NOT NULL DEFAULT 5,
            last_run_date       DATE
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id              TEXT PRIMARY KEY,
            firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            document_id     UUID NOT NULL,
            matter_id       TEXT,
            chunk_source    TEXT NOT NULL,
            text            TEXT NOT NULL,
            chunk_index     INT NOT NULL DEFAULT 0,
            page_number     INT DEFAULT 1,
            zlr_item_id     TEXT,
            citation        TEXT,
            case_name       TEXT,
            taxonomy_category TEXT,
            source_type     TEXT,
            source_name     TEXT,
            reference       TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_firm ON chunks(firm_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(firm_id, chunk_source);
        CREATE TABLE IF NOT EXISTS invites (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            email       TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role        TEXT NOT NULL DEFAULT 'associate',
            invited_by  UUID REFERENCES users(id),
            cf_rule_id  TEXT,
            sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            accepted_at TIMESTAMPTZ,
            UNIQUE (firm_id, email)
        );
        CREATE INDEX IF NOT EXISTS idx_invites_firm ON invites(firm_id);

        -- ── Legal Corner spec-correct schema additions ─────────────────────────────────
        -- Branding and feature flags on firms
        ALTER TABLE firms ADD COLUMN IF NOT EXISTS firm_logo_url TEXT;
        ALTER TABLE firms ADD COLUMN IF NOT EXISTS features JSONB DEFAULT '[]'::jsonb;

        -- Organisation roles (ops_manager / panel_lawyer) — separate from firm-level role
        CREATE TABLE IF NOT EXISTS organisation_roles (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role        TEXT NOT NULL CHECK (role IN ('ops_manager', 'panel_lawyer')),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (firm_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_org_roles_firm ON organisation_roles(firm_id);
        CREATE INDEX IF NOT EXISTS idx_org_roles_user ON organisation_roles(user_id);

        -- Matter SLA and assignment fields for Legal Corner workflow
        ALTER TABLE matters ADD COLUMN IF NOT EXISTS assigned_lawyer_id UUID REFERENCES users(id);
        ALTER TABLE matters ADD COLUMN IF NOT EXISTS coverage_tier TEXT;
        ALTER TABLE matters ADD COLUMN IF NOT EXISTS sla_deadline TIMESTAMPTZ;
        ALTER TABLE matters ADD COLUMN IF NOT EXISTS assigned_by_id UUID REFERENCES users(id);
        ALTER TABLE matters ADD COLUMN IF NOT EXISTS service_type TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_matters_external_ref
            ON matters(firm_id, external_ref) WHERE external_ref IS NOT NULL;

        -- Reassignment audit trail
        CREATE TABLE IF NOT EXISTS matter_reassignments (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            matter_id           UUID NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
            from_lawyer_id      UUID REFERENCES users(id),
            to_lawyer_id        UUID NOT NULL REFERENCES users(id),
            reassigned_by_id    UUID NOT NULL REFERENCES users(id),
            reason              TEXT,
            reassigned_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_reassignments_matter ON matter_reassignments(matter_id);

        -- API keys for server-to-server auth (Legal Corner subscriber platform)
        CREATE TABLE IF NOT EXISTS firm_api_keys (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            key_hash    TEXT NOT NULL,
            label       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            revoked_at  TIMESTAMPTZ,
            UNIQUE (firm_id, label)
        );

        -- Read-optimised SLA status view for ops dashboard
        -- Note: u.full_name falls back to display_name since users table uses display_name
        CREATE OR REPLACE VIEW v_legal_corner_sla_status AS
        SELECT
            m.id AS matter_id,
            m.firm_id,
            m.name AS matter_name,
            m.client_name,
            m.assigned_lawyer_id,
            u.display_name AS lawyer_name,
            m.coverage_tier,
            m.service_type,
            m.sla_deadline,
            m.status,
            m.external_ref,
            CASE
                WHEN m.sla_deadline IS NULL THEN NULL
                WHEN m.status = 'complete' THEN false
                WHEN now() > m.sla_deadline THEN true
                ELSE false
            END AS is_overdue,
            (SELECT count(*) FROM matter_reassignments r WHERE r.matter_id = m.id) AS reassignment_count
        FROM matters m
        LEFT JOIN users u ON u.id = m.assigned_lawyer_id;

        -- Migrate invites: add organisation_role column (spec-correct name, no FK)
        ALTER TABLE invites ADD COLUMN IF NOT EXISTS organisation_role TEXT;
        """)

        # Seed Nyari's firm if not present
        await conn.execute("""
        INSERT INTO firms (id, name, short_name, city, country)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (id) DO NOTHING
        """,
        FIRM_ID, FIRM_NAME, "S&M", "Harare", "Zimbabwe")

        await conn.execute("""
        INSERT INTO reminder_settings (firm_id)
        VALUES ($1) ON CONFLICT (firm_id) DO NOTHING
        """, FIRM_ID)

    print("[db] schema migrations complete")

# ── Firm identity ─────────────────────────────────────────────────────────────
FIRM_NAME = os.environ.get("MUTEMO_FIRM_NAME", "Sawyer & Mkushi Legal Practitioners")
FIRM_CITY = os.environ.get("MUTEMO_FIRM_CITY", "Harare, Zimbabwe")
# Fixed firm UUID for Nyari's deployment. Second firm gets its own Railway instance + its own FIRM_ID.
FIRM_ID_STR = os.environ.get("MUTEMO_FIRM_ID", "a1b2c3d4-0000-0000-0000-000000000001")
import uuid as _uuid_mod
FIRM_ID = _uuid_mod.UUID(FIRM_ID_STR)

# ── RBAC helpers ──────────────────────────────────────────────────────────────
ROLE_WEIGHTS = {"admin": 4, "partner": 3, "associate": 2, "secretary": 1}

# Permission matrix
PERMISSIONS = {
    # Matters
    "matter:read":          {"admin", "partner", "associate", "secretary"},
    "matter:create":        {"admin", "partner", "associate", "secretary"},
    "matter:edit":          {"admin", "partner", "associate", "secretary"},
    "matter:delete":        {"admin", "partner"},
    # Documents
    "document:upload":      {"admin", "partner", "associate", "secretary"},
    "document:delete":      {"admin", "partner"},
    # Notes
    "note:create":          {"admin", "partner", "associate", "secretary"},
    "note:delete":          {"admin", "partner"},
    # Drafting (lawyer functions)
    "draft:affidavit":      {"admin", "partner", "associate"},
    "draft:document":       {"admin", "partner", "associate"},
    # Calendar
    "calendar:read":        {"admin", "partner", "associate", "secretary"},
    "calendar:create":      {"admin", "partner", "associate", "secretary"},
    "calendar:delete":      {"admin", "partner", "associate"},
    # Legal updates / ZLR
    "legal:upload":         {"admin", "partner", "associate"},
    "legal:delete":         {"admin", "partner"},
    # Search
    "search":               {"admin", "partner", "associate", "secretary"},
    # Admin
    "admin:settings":       {"admin", "partner"},
    "admin:users":          {"admin", "partner"},
    "admin:reindex":        {"admin"},
}

def _check_permission(user: dict, permission: str):
    """Raise 403 if user's role does not have the required permission."""
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    role = user.get("role", "secretary")
    allowed = PERMISSIONS.get(permission, set())
    if role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Your role ({role}) does not have permission for this action."
        )

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(reminder_scheduler_loop())
    async def warm_up():
        try:
            await asyncio.to_thread(get_embedding_model)
            await asyncio.to_thread(get_chroma_collections)
            print("[startup] semantic search ready")
        except Exception as e:
            print(f"[startup] semantic search unavailable, will use keyword fallback: {e}")
    asyncio.create_task(warm_up())
    yield
    if _db_pool:
        await _db_pool.close()
        print("[db] connection pool closed")

app = FastAPI(title="Mutemo Desk", version="2.0.0", lifespan=lifespan)

# ── AlertEngine instrumentation ───────────────────────────────────────────────
try:
    from fastapi_alertengine import instrument
    instrument(app)
    print("[startup] AlertEngine instrumentation active")
except Exception as e:
    print(f"[startup] AlertEngine instrumentation unavailable: {e}")

# ── AlertEngine health metrics ─────────────────────────────────────────────────
import time as _time
from collections import deque

_request_latencies: deque = deque(maxlen=200)
_request_errors: deque = deque(maxlen=200)

def _p95_latency() -> float:
    if not _request_latencies:
        return 0.0
    sorted_latencies = sorted(_request_latencies)
    idx = int(len(sorted_latencies) * 0.95)
    return round(sorted_latencies[min(idx, len(sorted_latencies) - 1)], 3)

def _error_rate() -> float:
    if not _request_errors:
        return 0.0
    return round(sum(_request_errors) / len(_request_errors), 3)

def _health_score() -> float:
    latency = _p95_latency()
    err = _error_rate()
    score = 100.0
    if latency > 2.0:
        score -= min(40, (latency - 2.0) * 10)
    if err > 0.01:
        score -= min(60, err * 200)
    return round(max(0.0, score), 1)

@app.get("/health/alerts")
async def health_alerts():
    """AlertEngine health endpoint — real-time API health metrics."""
    return {
        "status": "ok",
        "score": _health_score(),
        "p95_latency": _p95_latency(),
        "error_rate": _error_rate(),
        "timestamp": _time.time(),
    }
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ── Request size limit ────────────────────────────────────────────────────────
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB

@app.middleware("http")
async def metrics_middleware(request, call_next):
    start = _time.time()
    response = await call_next(request)
    duration = _time.time() - start
    _request_latencies.append(duration)
    _request_errors.append(1 if response.status_code >= 500 else 0)
    return response
@app.middleware("http")
async def size_limit_middleware(request, call_next):
    if request.method == "POST":
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_UPLOAD_SIZE:
            return JSONResponse(
                status_code=413,
                content={"detail": "File too large. Maximum upload size is 50MB."}
            )
    return await call_next(request)

# ── Admin token ───────────────────────────────────────────────────────────────
ADMIN_TOKEN = os.environ.get("MUTEMO_ADMIN_TOKEN")

def require_admin_token(request: Request):
    if ADMIN_TOKEN:
        token = request.headers.get("X-Admin-Token", "")
        if token != ADMIN_TOKEN:
            raise HTTPException(status_code=403, detail="Admin access required")

# ── OTP Authentication ────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
ALLOWED_PHONE_NUMBERS = set(
    n.strip() for n in os.environ.get("MUTEMO_ALLOWED_PHONES", "").split(",") if n.strip()
)
AUTH_ENABLED = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER and ALLOWED_PHONE_NUMBERS)

OTP_TTL_SECONDS     = 300
SESSION_TTL_SECONDS = 86400 * 7
MAX_OTP_ATTEMPTS    = 5

def _send_sms_otp(phone: str, code: str) -> bool:
    try:
        from twilio.rest import Client as TwilioClient
        tc = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        tc.messages.create(
            body=f"Your Mutemo Desk login code is {code}. It expires in 5 minutes.",
            from_=TWILIO_FROM_NUMBER,
            to=phone,
        )
        return True
    except Exception as e:
        print(f"[otp] Twilio send failed: {e}")
        return False

class OTPRequestBody(BaseModel):
    phone: str

class OTPVerifyBody(BaseModel):
    phone: str
    code: str

@app.post("/api/auth/request-otp")
async def request_otp(req: OTPRequestBody):
    if not AUTH_ENABLED:
        raise HTTPException(status_code=503, detail="OTP login is not configured on this server.")
    phone = req.phone.strip()
    if phone not in ALLOWED_PHONE_NUMBERS:
        return {"sent": True, "message": "If this number is registered, a code has been sent."}

    code = f"{secrets.randbelow(1000000):06d}"
    expires = datetime.utcnow() + timedelta(seconds=OTP_TTL_SECONDS)
    async with _db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO otp_store (phone, code, attempts, expires_at)
            VALUES ($1, $2, 0, $3)
            ON CONFLICT (phone) DO UPDATE SET code=$2, attempts=0, expires_at=$3
        """, phone, code, expires)

    sent = await asyncio.to_thread(_send_sms_otp, phone, code)
    if not sent:
        raise HTTPException(status_code=500, detail="Failed to send SMS. Please try again.")
    return {"sent": True, "message": "If this number is registered, a code has been sent."}

@app.post("/api/auth/verify-otp")
async def verify_otp(req: OTPVerifyBody, response: Response):
    if not AUTH_ENABLED:
        raise HTTPException(status_code=503, detail="OTP login is not configured on this server.")
    phone = req.phone.strip()

    async with _db_pool.acquire() as conn:
        # Clean expired entries
        await conn.execute("DELETE FROM otp_store WHERE expires_at < NOW()")
        entry = await conn.fetchrow("SELECT * FROM otp_store WHERE phone=$1", phone)

        if not entry:
            raise HTTPException(status_code=401, detail="No active code for this number. Request a new one.")

        new_attempts = entry["attempts"] + 1
        if new_attempts > MAX_OTP_ATTEMPTS:
            await conn.execute("DELETE FROM otp_store WHERE phone=$1", phone)
            raise HTTPException(status_code=429, detail="Too many attempts. Request a new code.")

        await conn.execute("UPDATE otp_store SET attempts=$1 WHERE phone=$2", new_attempts, phone)

        if not hmac.compare_digest(entry["code"], req.code.strip()):
            raise HTTPException(status_code=401, detail="Incorrect code.")

        # Success — look up or create user
        await conn.execute("DELETE FROM otp_store WHERE phone=$1", phone)
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE firm_id=$1 AND phone=$2 AND is_active=TRUE",
            FIRM_ID, phone
        )
        if not user:
            # Auto-create user as secretary (admin can promote later)
            user = await conn.fetchrow("""
                INSERT INTO users (firm_id, phone, display_name, role)
                VALUES ($1, $2, $3, 'secretary')
                RETURNING *
            """, FIRM_ID, phone, phone)

        token = secrets.token_urlsafe(32)
        expires = datetime.utcnow() + timedelta(seconds=SESSION_TTL_SECONDS)
        await conn.execute("""
            INSERT INTO sessions (token, user_id, firm_id, expires_at)
            VALUES ($1, $2, $3, $4)
        """, token, user["id"], FIRM_ID, expires)

    response.set_cookie(
        key="mutemo_session", value=token,
        max_age=SESSION_TTL_SECONDS, httponly=True, secure=True, samesite="lax",
    )
    return {"verified": True, "phone": phone, "role": user["role"], "display_name": user["display_name"]}

@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("mutemo_session")
    if token and _db_pool:
        async with _db_pool.acquire() as conn:
            await conn.execute("DELETE FROM sessions WHERE token=$1", token)
    response.delete_cookie("mutemo_session")
    return {"logged_out": True}

@app.get("/api/auth/status")
async def auth_status(request: Request):
    if not AUTH_ENABLED:
        return {"auth_enabled": False, "authenticated": True}
    token = request.cookies.get("mutemo_session")
    if token and _db_pool:
        async with _db_pool.acquire() as conn:
            await conn.execute("DELETE FROM sessions WHERE expires_at < NOW()")
            row = await conn.fetchrow("""
                SELECT s.token, u.phone, u.role, u.display_name
                FROM sessions s JOIN users u ON s.user_id = u.id
                WHERE s.token=$1 AND s.expires_at > NOW()
            """, token)
            if row:
                return {
                    "auth_enabled": True, "authenticated": True,
                    "phone": row["phone"], "role": row["role"],
                    "display_name": row["display_name"]
                }
    return {"auth_enabled": True, "authenticated": False}

async def get_current_user(request: Request) -> Optional[dict]:
    """Return the current user dict, or None if not authenticated."""
    if not AUTH_ENABLED:
        # Return a synthetic partner user when auth is disabled (dev/demo mode)
        return {"id": None, "firm_id": FIRM_ID, "phone": None, "role": "partner", "display_name": "NGM"}
    token = request.cookies.get("mutemo_session")
    if not token or not _db_pool:
        return None
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT u.id, u.firm_id, u.phone, u.role, u.display_name
            FROM sessions s JOIN users u ON s.user_id = u.id
            WHERE s.token=$1 AND s.expires_at > NOW()
        """, token)
        if row:
            return dict(row)
    return None

@app.middleware("http")
async def session_auth_middleware(request, call_next):
    if not AUTH_ENABLED:
        return await call_next(request)
    open_paths = (
        "/api/health", "/api/auth/request-otp", "/api/auth/verify-otp",
        "/api/auth/status", "/api/matters/template", "/api/matters/template-excel"
    )
    if request.url.path in open_paths or not request.url.path.startswith("/api/"):
        return await call_next(request)

    token = request.cookies.get("mutemo_session")
    if token and _db_pool:
        async with _db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT token FROM sessions WHERE token=$1 AND expires_at > NOW()", token
            )
            if row:
                return await call_next(request)

    return JSONResponse(status_code=401, content={"detail": "Authentication required"})

# ── User management endpoints ─────────────────────────────────────────────────

@app.get("/api/users")
async def list_users(request: Request):
    user = await get_current_user(request)
    _check_permission(user, "admin:users")
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, phone, display_name, role, is_active, created_at FROM users WHERE firm_id=$1 ORDER BY created_at",
            FIRM_ID
        )
    return [dict(r) for r in rows]

@app.patch("/api/users/{user_id}")
async def update_user(user_id: str, body: dict, request: Request):
    """Admin only: update a user's role or display_name."""
    user = await get_current_user(request)
    _check_permission(user, "admin:users")
    allowed_fields = {"role", "display_name", "is_active"}
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    if "role" in updates and updates["role"] not in ("partner", "associate", "secretary", "admin"):
        raise HTTPException(status_code=400, detail="Invalid role")
    set_clauses = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates.keys()))
    values = list(updates.values())
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE users SET {set_clauses} WHERE id=$1 AND firm_id=${len(values)+2} RETURNING *",
            _uuid_mod.UUID(user_id), *values, FIRM_ID
        )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return dict(row)

# ── Invites ───────────────────────────────────────────────────────────────────

def _get_cf_vars():
    return (
        os.environ.get("CLOUDFLARE_API_TOKEN"),
        os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
        os.environ.get("CLOUDFLARE_ACCESS_APP_ID"),
    )

async def _add_email_to_cloudflare_access(email: str) -> Optional[str]:
    """Add an email to the Cloudflare Access policy. Returns the rule ID or None."""
    CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_ACCESS_APP_ID = _get_cf_vars()
    if not all([CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_ACCESS_APP_ID]):
        print("[invite] Cloudflare vars not set — skipping CF Access update")
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/access/apps/{CLOUDFLARE_ACCESS_APP_ID}/policies",
                headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
            )
            resp.raise_for_status()
            policies = resp.json().get("result", [])

            allow_policy = next((p for p in policies if p.get("decision") == "allow"), None)
            if not allow_policy:
                print("[invite] No Allow policy found in Cloudflare Access app")
                return None

            policy_id = allow_policy["id"]
            existing_include = allow_policy.get("include", [])

            already_there = any(
                r.get("email", {}).get("email") == email
                for r in existing_include
            )
            if already_there:
                print(f"[invite] {email} already in Cloudflare Access policy")
                return policy_id

            new_include = existing_include + [{"email": {"email": email}}]

            update_resp = await http.put(
                f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/access/apps/{CLOUDFLARE_ACCESS_APP_ID}/policies/{policy_id}",
                headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}", "Content-Type": "application/json"},
                json={
                    "name": allow_policy["name"],
                    "decision": "allow",
                    "include": new_include,
                    "exclude": allow_policy.get("exclude", []),
                    "require": allow_policy.get("require", []),
                },
            )
            update_resp.raise_for_status()
            print(f"[invite] Added {email} to Cloudflare Access policy")
            return policy_id

    except Exception as e:
        print(f"[invite] Cloudflare Access update failed: {e}")
        return None

async def _send_invite_email(email: str, display_name: str, invited_by_name: str) -> bool:
    """Send welcome invite email via Resend."""
    try:
        html = f"""
        <div style="font-family:Georgia,serif;max-width:560px;margin:0 auto">
            <div style="background:#1b4d2e;color:white;padding:16px 20px;border-radius:6px 6px 0 0">
                <strong style="font-size:18px">&#9878; Mutemo Desk</strong><br/>
                <span style="font-size:13px;opacity:0.8">You have been invited</span>
            </div>
            <div style="padding:24px 20px;border:1px solid #d8d3c8;border-top:none;border-radius:0 0 6px 6px">
                <p>Hi {display_name},</p>
                <p>{invited_by_name} has invited you to access <strong>Mutemo Desk</strong> — the legal practice management system for {FIRM_NAME}.</p>
                <p style="margin:24px 0">
                    <a href="https://mutemo.tofamba.com"
                       style="background:#1b4d2e;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:bold">
                        Access Mutemo Desk
                    </a>
                </p>
                <p style="font-size:13px;color:#666">
                    When prompted, enter your email address <strong>{email}</strong> to receive a one-time login code.
                </p>
                <p style="font-size:13px;color:#666">
                    If you have any issues accessing the system, contact {invited_by_name}.
                </p>
            </div>
        </div>
        """
        text = f"Hi {display_name},\n\n{invited_by_name} has invited you to Mutemo Desk.\n\nAccess it at: https://mutemo.tofamba.com\n\nUse your email {email} to log in.\n\n— Mutemo Desk"
        await asyncio.to_thread(
            _send_via_resend_sync,
            email,
            f"You've been invited to Mutemo Desk — {FIRM_NAME}",
            html,
            text,
        )
        return True
    except Exception as e:
        print(f"[invite] email send failed: {e}")
        return False

class InviteRequest(BaseModel):
    email: str
    display_name: str
    role: str = "associate"
    organisation_role: Optional[str] = None  # ops_manager | panel_lawyer

@app.post("/api/admin/invite")
async def invite_user(req: InviteRequest, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "admin:users")

    if req.role not in ("partner", "associate", "secretary", "admin"):
        raise HTTPException(status_code=400, detail="Invalid role")

    if req.organisation_role and req.organisation_role not in ("ops_manager", "panel_lawyer"):
        raise HTTPException(status_code=400, detail="Invalid organisation_role")

    try:
        cf_rule_id = await _add_email_to_cloudflare_access(req.email)
    except Exception as e:
        print(f"[invite] CF access error (non-fatal): {e}")
        cf_rule_id = None

    async with _db_pool.acquire() as conn:
        try:
            row = await conn.fetchrow("""
                INSERT INTO invites
                    (firm_id, email, display_name, role, invited_by, cf_rule_id,
                     organisation_role)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (firm_id, email) DO UPDATE SET
                    display_name=$3, role=$4, sent_at=NOW(), cf_rule_id=$6,
                    organisation_role=$7
                RETURNING *
            """,
            FIRM_ID, req.email, req.display_name, req.role,
            _uuid_mod.UUID(str(user["id"])) if user.get("id") else None,
            str(cf_rule_id) if cf_rule_id else None,
            req.organisation_role
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not create invite: {e}")

    invited_by_name = user.get("display_name") or "Your administrator"
    email_sent = await _send_invite_email(req.email, req.display_name, invited_by_name)

    return {
        "invited": True,
        "email": req.email,
        "display_name": req.display_name,
        "role": req.role,
        "organisation_role": req.organisation_role,
        "cloudflare_updated": cf_rule_id is not None,
        "email_sent": email_sent,
    }

@app.get("/api/admin/invites")
async def list_invites(request: Request):
    user = await get_current_user(request)
    _check_permission(user, "admin:users")
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM invites WHERE firm_id=$1 ORDER BY sent_at DESC",
            FIRM_ID
        )
    result = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["firm_id"] = str(d["firm_id"])
        if d.get("invited_by"):
            d["invited_by"] = str(d["invited_by"])
        if d.get("sent_at"):
            d["sent_at"] = d["sent_at"].isoformat()
        if d.get("accepted_at"):
            d["accepted_at"] = d["accepted_at"].isoformat()
        result.append(d)
    return result

@app.delete("/api/admin/invites/{invite_id}")
async def cancel_invite(invite_id: str, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "admin:users")
    async with _db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM invites WHERE id=$1 AND firm_id=$2",
            _uuid_mod.UUID(invite_id), FIRM_ID
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Invite not found")
    return {"deleted": True}

@app.patch("/api/invites/{invite_id}/accept")
async def accept_invite(invite_id: str, request: Request):
    """
    Mark an invite as accepted and create the user account.
    Called by the onboarding flow after OTP verification.
    The authenticated user's session must already exist (they logged in via OTP).
    """
    user = await get_current_user(request)

    try:
        invite_uuid = _uuid_mod.UUID(invite_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid invite_id")

    async with _db_pool.acquire() as conn:
        invite = await conn.fetchrow(
            "SELECT * FROM invites WHERE id=$1 AND firm_id=$2",
            invite_uuid, FIRM_ID
        )
        if not invite:
            raise HTTPException(status_code=404, detail="Invite not found")
        if invite["accepted_at"]:
            return {"already_accepted": True, "accepted_at": invite["accepted_at"].isoformat()}

        # Mark invite as accepted
        await conn.execute(
            "UPDATE invites SET accepted_at=NOW() WHERE id=$1",
            invite_uuid
        )

        # If invite has an org role, insert into organisation_roles (spec-correct)
        if invite.get("organisation_role"):
            user_id = _uuid_mod.UUID(str(user["id"])) if user.get("id") else None
            if user_id:
                await conn.execute("""
                    INSERT INTO organisation_roles (firm_id, user_id, role)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (firm_id, user_id) DO UPDATE SET role = $3
                """,
                FIRM_ID, user_id, invite["organisation_role"]
                )

    return {
        "accepted": True,
        "invite_id": invite_id,
        "role": invite["role"],
        "organisation_role": invite.get("organisation_role"),
    }

class UpdateInviteRequest(BaseModel):
    organisation_role: Optional[str] = None
    role: Optional[str] = None  # firm-level role

@app.patch("/api/admin/invites/{invite_id}")
async def update_invite(invite_id: str, req: UpdateInviteRequest, request: Request):
    """
    Update a pending invite's organisation_role or firm role before it is accepted.
    Requires admin:users permission.
    """
    user = await get_current_user(request)
    _check_permission(user, "admin:users")

    try:
        invite_uuid = _uuid_mod.UUID(invite_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid invite_id")

    if req.organisation_role and req.organisation_role not in ("ops_manager", "panel_lawyer",
                                                                "org_admin", "lawyer", "paralegal"):
        raise HTTPException(status_code=400, detail="Invalid organisation_role")
    if req.role and req.role not in ("partner", "associate", "secretary", "admin"):
        raise HTTPException(status_code=400, detail="Invalid role")

    async with _db_pool.acquire() as conn:
        invite = await conn.fetchrow(
            "SELECT * FROM invites WHERE id=$1 AND firm_id=$2",
            invite_uuid, FIRM_ID
        )
        if not invite:
            raise HTTPException(status_code=404, detail="Invite not found")
        if invite["accepted_at"]:
            raise HTTPException(status_code=409, detail="Invite already accepted — cannot modify")

        updates = []
        params = []
        param_idx = 1
        if req.organisation_role is not None:
            updates.append(f"organisation_role=${param_idx}")
            params.append(req.organisation_role)
            param_idx += 1
        if req.role is not None:
            updates.append(f"role=${param_idx}")
            params.append(req.role)
            param_idx += 1

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        params.append(invite_uuid)
        await conn.execute(
            f"UPDATE invites SET {', '.join(updates)} WHERE id=${param_idx}",
            *params
        )

        updated = await conn.fetchrow("SELECT * FROM invites WHERE id=$1", invite_uuid)

    d = dict(updated)
    d["id"] = str(d["id"])
    d["firm_id"] = str(d["firm_id"])
    if d.get("invited_by"):
        d["invited_by"] = str(d["invited_by"])
    if d.get("sent_at"):
        d["sent_at"] = d["sent_at"].isoformat()
    return d

import hashlib
# ── Legal Corner — spec-correct endpoints ─────────────────────────────────────────────────

from datetime import timezone

# ── API key auth helper (Bearer token, firm_api_keys table) ────────────────────
async def verify_firm_api_key(request: Request) -> str:
    """
    Validate Authorization: Bearer <key> against firm_api_keys table.
    Returns the firm_id string or raises 401.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed API key")
    raw_key = auth_header.removeprefix("Bearer ").strip()
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT firm_id FROM firm_api_keys WHERE key_hash=$1 AND revoked_at IS NULL",
            key_hash
        )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    return str(row["firm_id"])

# ── Org role permission helper ───────────────────────────────────────────────
async def _check_org_role(user: dict, firm_id, required_role: str):
    """
    Verify the authenticated user holds the required organisation role.
    Raises 403 if not.
    """
    user_id = _uuid_mod.UUID(str(user["id"])) if user.get("id") else None
    if not user_id:
        raise HTTPException(status_code=403, detail="Org role required")
    if isinstance(firm_id, str):
        firm_id = _uuid_mod.UUID(firm_id)
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role FROM organisation_roles WHERE firm_id=$1 AND user_id=$2",
            firm_id, user_id
        )
    if not row or row["role"] != required_role:
        raise HTTPException(status_code=403, detail=f"{required_role} role required")

# ── SLA deadline calculation ────────────────────────────────────────────────────────
# PLACEHOLDER: tier-to-hours mapping needs confirming with Legal Corner before go-live
SLA_HOURS_BY_TIER = {"tier_1": 24, "tier_2": 48, "tier_3": 72, "tier_4": 168}

def calculate_sla_deadline(created_at: datetime, coverage_tier: str) -> datetime:
    hours = SLA_HOURS_BY_TIER.get(coverage_tier, 48)
    return created_at + timedelta(hours=hours)

# ── Pydantic models for spec-correct Legal Corner endpoints ─────────────────────
class AutoCreateMatterRequest(BaseModel):
    external_ref: str
    client_name: str
    assigned_lawyer_id: str
    coverage_tier: str
    service_type: str
    description: Optional[str] = None

class ReassignRequest(BaseModel):
    to_lawyer_id: str
    reason: Optional[str] = None

class FirmApiKeyRequest(BaseModel):
    label: str = "default"

# ── POST /api/matters/auto-create — server-to-server, API key auth ──────────────
@app.post("/api/matters/auto-create", status_code=201)
async def auto_create_matter(req: AutoCreateMatterRequest, request: Request):
    """
    Auto-create a matter from Legal Corner's subscriber platform.
    Authenticates via Authorization: Bearer <api_key>.
    Idempotent on external_ref.
    """
    firm_id_str = await verify_firm_api_key(request)
    firm_uuid = _uuid_mod.UUID(firm_id_str)

    async with _db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM matters WHERE firm_id=$1 AND external_ref=$2",
            firm_uuid, req.external_ref
        )
        if existing:
            return {**_row_to_doc(existing), "created": False,
                    "message": "Matter already exists for this external_ref"}

        # Verify assigned lawyer is a panel_lawyer in this firm
        try:
            lawyer_uuid = _uuid_mod.UUID(req.assigned_lawyer_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid assigned_lawyer_id")

        lawyer_check = await conn.fetchrow(
            "SELECT role FROM organisation_roles WHERE firm_id=$1 AND user_id=$2",
            firm_uuid, lawyer_uuid
        )
        if not lawyer_check or lawyer_check["role"] != "panel_lawyer":
            raise HTTPException(status_code=422,
                detail="assigned_lawyer_id is not a panel_lawyer in this firm")

        created_at = datetime.now(timezone.utc)
        sla_deadline = calculate_sla_deadline(created_at, req.coverage_tier)
        matter_id = _uuid_mod.uuid4()

        row = await conn.fetchrow("""
            INSERT INTO matters (
                id, firm_id, name, client_name, status,
                assigned_lawyer_id, coverage_tier, service_type,
                sla_deadline, external_ref, created_at
            )
            VALUES ($1,$2,$3,$4,'Active',$5,$6,$7,$8,$9,$10)
            RETURNING *
        """,
        matter_id, firm_uuid,
        f"{req.client_name} — {req.service_type}", req.client_name,
        lawyer_uuid, req.coverage_tier,
        req.service_type, sla_deadline, req.external_ref, created_at
        )

    return {**_row_to_doc(row), "created": True}

# ── POST /api/matters/{matter_id}/reassign — ops_manager only ──────────────────
@app.post("/api/matters/{matter_id}/reassign")
async def reassign_matter_spec(matter_id: str, req: ReassignRequest, request: Request):
    """
    Reassign a matter to a different panel lawyer.
    Requires ops_manager organisation role.
    Records full audit trail in matter_reassignments.
    """
    user = await get_current_user(request)

    try:
        matter_uuid = _uuid_mod.UUID(matter_id)
        to_lawyer_uuid = _uuid_mod.UUID(req.to_lawyer_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID")

    async with _db_pool.acquire() as conn:
        matter = await conn.fetchrow(
            "SELECT * FROM matters WHERE id=$1 AND firm_id=$2",
            matter_uuid, FIRM_ID
        )
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")

        await _check_org_role(user, matter["firm_id"], "ops_manager")

        user_id = _uuid_mod.UUID(str(user["id"]))

        # Record audit trail
        await conn.execute("""
            INSERT INTO matter_reassignments
                (matter_id, from_lawyer_id, to_lawyer_id, reassigned_by_id, reason)
            VALUES ($1, $2, $3, $4, $5)
        """,
        matter_uuid, matter.get("assigned_lawyer_id"),
        to_lawyer_uuid, user_id, req.reason
        )

        # Update matter
        await conn.execute(
            "UPDATE matters SET assigned_lawyer_id=$1, assigned_by_id=$2 WHERE id=$3",
            to_lawyer_uuid, user_id, matter_uuid
        )

    return {
        "status": "reassigned",
        "matter_id": matter_id,
        "to_lawyer_id": req.to_lawyer_id,
    }

# ── GET /api/organisations/{firm_id}/lawyers — list panel lawyers ───────────────
@app.get("/api/organisations/{firm_id}/lawyers")
async def list_org_lawyers(firm_id: str, request: Request):
    """
    List panel lawyers for a firm.
    Accepts session auth (ops_manager) or API key auth (matching firm_id).
    """
    try:
        firm_uuid = _uuid_mod.UUID(firm_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid firm_id")

    # Try session auth first, then API key
    try:
        user = await get_current_user(request)
        await _check_org_role(user, firm_uuid, "ops_manager")
    except HTTPException:
        api_firm_id = await verify_firm_api_key(request)
        if api_firm_id != firm_id:
            raise HTTPException(status_code=403, detail="API key does not match firm_id")

    async with _db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.id, u.display_name, u.phone
            FROM users u
            JOIN organisation_roles o ON o.user_id = u.id
            WHERE o.firm_id = $1 AND o.role = 'panel_lawyer'
            ORDER BY u.display_name
        """, firm_uuid)

    return {"lawyers": [{"id": str(r["id"]), "display_name": r["display_name"],
                         "phone": r["phone"]} for r in rows]}

# ── GET /api/organisations/{firm_id}/matters — ops manager SLA view ────────────
@app.get("/api/organisations/{firm_id}/matters")
async def list_org_matters(firm_id: str, request: Request):
    """List all matters with SLA status. Requires ops_manager role."""
    try:
        firm_uuid = _uuid_mod.UUID(firm_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid firm_id")

    user = await get_current_user(request)
    await _check_org_role(user, firm_uuid, "ops_manager")

    async with _db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM v_legal_corner_sla_status
            WHERE firm_id = $1
            ORDER BY sla_deadline ASC NULLS LAST
        """, firm_uuid)

    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
            elif isinstance(v, _uuid_mod.UUID):
                d[k] = str(v)
        result.append(d)
    return {"matters": result}

# ── GET /api/organisations/{firm_id}/dashboard — ops dashboard stats ───────────
@app.get("/api/organisations/{firm_id}/dashboard")
async def org_dashboard_spec(firm_id: str, request: Request):
    """Return SLA dashboard stats. Requires ops_manager role."""
    try:
        firm_uuid = _uuid_mod.UUID(firm_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid firm_id")

    user = await get_current_user(request)
    await _check_org_role(user, firm_uuid, "ops_manager")

    async with _db_pool.acquire() as conn:
        stats = await conn.fetchrow("""
            SELECT
                count(*) FILTER (WHERE status != 'complete') AS active_matters,
                count(*) FILTER (WHERE is_overdue = true) AS overdue_matters,
                count(DISTINCT assigned_lawyer_id) AS active_lawyers,
                count(*) FILTER (WHERE reassignment_count > 0) AS reassigned_matters
            FROM v_legal_corner_sla_status
            WHERE firm_id = $1
        """, firm_uuid)

    return {
        "firm_id": firm_id,
        "active_matters": stats["active_matters"],
        "overdue_matters": stats["overdue_matters"],
        "active_lawyers": stats["active_lawyers"],
        "reassigned_matters": stats["reassigned_matters"],
    }

# ── POST /api/admin/firm-api-keys — generate a firm API key ─────────────────────
@app.post("/api/admin/firm-api-keys", status_code=201)
async def create_firm_api_key(req: FirmApiKeyRequest, request: Request):
    """Generate a new API key for server-to-server auth. Requires admin:users permission."""
    user = await get_current_user(request)
    _check_permission(user, "admin:users")

    raw_key = f"fk_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    async with _db_pool.acquire() as conn:
        try:
            row = await conn.fetchrow("""
                INSERT INTO firm_api_keys (firm_id, key_hash, label)
                VALUES ($1, $2, $3) RETURNING id, label, created_at
            """, FIRM_ID, key_hash, req.label)
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="A key with this label already exists")

    return {
        "id": str(row["id"]),
        "label": row["label"],
        "api_key": raw_key,  # shown once — store securely
        "created_at": row["created_at"].isoformat(),
        "note": "Store this key securely. It will not be shown again.",
    }

# ── GET /api/auth/profile — return user profile including org role ───────────────
@app.get("/api/auth/profile")
async def get_user_profile(request: Request):
    """
    Return the current user's profile including firm role and organisation role.
    Used by the frontend on login to determine which UI features to show.
    """
    user = await get_current_user(request)
    user_id = _uuid_mod.UUID(str(user["id"])) if user.get("id") else None

    org_role = None
    firm_features = []
    firm_logo_url = None

    async with _db_pool.acquire() as conn:
        if user_id:
            org_row = await conn.fetchrow(
                "SELECT role FROM organisation_roles WHERE firm_id=$1 AND user_id=$2",
                FIRM_ID, user_id
            )
            org_role = org_row["role"] if org_row else None

        firm_row = await conn.fetchrow(
            "SELECT features, firm_logo_url FROM firms WHERE id=$1", FIRM_ID
        )
        if firm_row:
            firm_features = firm_row["features"] or []
            firm_logo_url = firm_row["firm_logo_url"]

    return {
        "id": str(user["id"]),
        "display_name": user.get("display_name"),
        "role": user.get("role"),
        "org_role": org_role,
        "firm_id": str(FIRM_ID),
        "firm_name": FIRM_NAME,
        "firm_logo_url": firm_logo_url,
        "features": firm_features,
    }

# ── Frontend static files ─────────────────────────────────────────────────────
frontend_path = os.path.join(os.path.dirname(__file__), "../frontend")
assets_path = os.path.join(frontend_path, "assets")
if os.path.exists(assets_path):
    app.mount("/assets", StaticFiles(directory=assets_path), name="assets")

client = anthropic.Anthropic()

# ── Semantic Search: embeddings + ChromaDB ────────────────────────────────────
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
_embedding_model = None
_chroma_client = None
_firm_collection = None
_legal_collection = None
_zlr_collection = None

def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        print(f"[embeddings] loading model '{EMBEDDING_MODEL_NAME}'...")
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        print("[embeddings] model loaded")
    return _embedding_model

def embed_texts(texts: list) -> list:
    import numpy as np
    model = get_embedding_model()
    vectors = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
    vectors = np.array(vectors)
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    elif vectors.ndim > 2:
        vectors = vectors.reshape(len(texts), -1)
    return [v.tolist() for v in vectors]

def get_chroma_collections():
    global _chroma_client, _firm_collection, _legal_collection, _zlr_collection
    if _chroma_client is None:
        import chromadb
        chroma_path = os.path.join(os.path.dirname(__file__), "..", "data", "chroma")
        os.makedirs(chroma_path, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=chroma_path)
        _firm_collection = _chroma_client.get_or_create_collection(
            "firm_precedents", metadata={"hnsw:space": "cosine"}
        )
        _legal_collection = _chroma_client.get_or_create_collection(
            "legal_updates", metadata={"hnsw:space": "cosine"}
        )
        _zlr_collection = _chroma_client.get_or_create_collection(
            "zlr_index", metadata={"hnsw:space": "cosine"}
        )
        print("[vector_store] ChromaDB initialized")
    return _firm_collection, _legal_collection, _zlr_collection

# ── Pydantic Models ───────────────────────────────────────────────────────────

MATTER_STATUSES = ["Active", "Awaiting Client", "Awaiting Court", "On Hold", "Closed"]

class MatterCreate(BaseModel):
    name: str
    number: Optional[str] = None
    internal_ref: Optional[str] = None
    external_ref: Optional[str] = None
    matter_type: Optional[str] = None
    status: Optional[str] = "Active"
    client_name: Optional[str] = None
    custom_status: Optional[str] = None

class MatterUpdate(BaseModel):
    name: Optional[str] = None
    internal_ref: Optional[str] = None
    external_ref: Optional[str] = None
    matter_type: Optional[str] = None
    status: Optional[str] = None
    client_name: Optional[str] = None
    custom_status: Optional[str] = None

class ProgressNote(BaseModel):
    text: str
    author: Optional[str] = None

class AffidavitRequest(BaseModel):
    matter_type: Optional[str] = None
    court: Optional[str] = "High Court of Zimbabwe"
    deponent_name: Optional[str] = None
    deponent_id: Optional[str] = None
    deponent_capacity: Optional[str] = None
    parties: Optional[str] = None
    matter_summary: str
    key_facts: Optional[str] = None
    relief: Optional[str] = None
    precedent_context: Optional[dict] = None

class SearchRequest(BaseModel):
    query: str
    matter_type: Optional[str] = None
    document_type: Optional[str] = None
    matter_id: Optional[str] = None
    limit: int = 8
    include_legal_updates: bool = True

class ExportRequest(BaseModel):
    affidavit_text: str
    deponent_name: Optional[str] = "Deponent"
    document_id: Optional[str] = "DOC"

class CalendarEvent(BaseModel):
    title: str
    matter_id: Optional[str] = None
    matter_name: Optional[str] = None
    event_type: str
    date: str
    time: Optional[str] = None
    court: Optional[str] = None
    notes: Optional[str] = None

class LegalUpdateSearchRequest(BaseModel):
    query: str
    source_type: Optional[str] = None
    limit: int = 8

class ReminderSettings(BaseModel):
    enabled: bool
    recipient_email: str
    send_hour_utc: int = 5

# ── DB helpers ────────────────────────────────────────────────────────────────

def _row_to_matter(row) -> dict:
    d = dict(row)
    for k in ("id", "firm_id", "created_by"):
        if d.get(k):
            d[k] = str(d[k])
    for k in ("created_at", "last_activity"):
        if d.get(k):
            d[k] = d[k].isoformat()
    return d

def _row_to_doc(row) -> dict:
    d = dict(row)
    for k in ("id", "matter_id", "firm_id", "uploaded_by"):
        if d.get(k):
            d[k] = str(d[k])
    for k in ("uploaded_at",):
        if d.get(k):
            d[k] = d[k].isoformat()
    if d.get("doc_date"):
        d["doc_date"] = str(d["doc_date"])
    return d

def _row_to_note(row) -> dict:
    d = dict(row)
    for k in ("id", "matter_id", "firm_id", "user_id"):
        if d.get(k):
            d[k] = str(d[k])
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    return d

def _row_to_event(row) -> dict:
    d = dict(row)
    for k in ("id", "firm_id", "matter_id", "created_by"):
        if d.get(k):
            d[k] = str(d[k])
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    if d.get("date"):
        d["date"] = str(d["date"])
    if d.get("time"):
        d["time"] = str(d["time"])[:5]  # HH:MM
    return d

# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    import shutil
    embeddings_ok = _embedding_model is not None
    if not embeddings_ok:
        try:
            import sentence_transformers
            embeddings_ok = True
        except Exception:
            pass

    db_ok = False
    try:
        if _db_pool:
            async with _db_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            db_ok = True
    except Exception:
        pass

    deps = {
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "database": db_ok,
        "tesseract": shutil.which("tesseract") is not None,
        "pdftoppm": shutil.which("pdftoppm") is not None,
        "node": shutil.which("node") is not None,
        "smtp_configured": is_email_configured(),
        "semantic_search": embeddings_ok,
    }
    status = "ok" if (deps["anthropic_key"] and deps["database"]) else "degraded"
    return {
        "status": status,
        "version": "2.0.0",
        "service": "Mutemo Desk",
        "dependencies": deps,
    }

@app.post("/api/admin/reindex")
async def reindex_semantic_search(request: Request):
    require_admin_token(request)
    async with _db_pool.acquire() as conn:
        chunk_rows = await conn.fetch(
            "SELECT * FROM chunks WHERE firm_id=$1", FIRM_ID
        )
    chunks = [dict(r) for r in chunk_rows]
    firm_chunks = [c for c in chunks if c["chunk_source"] == "firm"]
    legal_chunks = [c for c in chunks if c["chunk_source"] == "legal"]
    zlr_chunks_list = [c for c in chunks if c["chunk_source"] == "zlr"]
    if firm_chunks:
        await asyncio.to_thread(index_chunks_in_chroma, firm_chunks, "firm")
    if legal_chunks:
        await asyncio.to_thread(index_chunks_in_chroma, legal_chunks, "legal")
    if zlr_chunks_list:
        await asyncio.to_thread(index_chunks_in_chroma, zlr_chunks_list, "zlr")
    return {"reindexed": len(chunks), "firm": len(firm_chunks), "legal": len(legal_chunks), "zlr": len(zlr_chunks_list)}
@app.post("/api/admin/reindex-from-db")
async def reindex_from_db(request: Request):
    """
    Rebuild ChromaDB vectors from raw_text stored in PostgreSQL.
    Use after migration — populates chunks table and ChromaDB from existing DB records.
    """
    require_admin_token(request)
    indexed_zlr = 0
    all_chunks = []

    async with _db_pool.acquire() as conn:
        zlr_rows = await conn.fetch(
            "SELECT id, raw_text, case_name, citation, taxonomy_category FROM zlr_entries WHERE firm_id=$1 AND raw_text IS NOT NULL",
            FIRM_ID
        )

    for row in zlr_rows:
        item_id = str(row["id"])
        new_chunks = chunk_text(row["raw_text"], 1, item_id, "zlr")
        for c in new_chunks:
            c["chunk_source"] = "zlr"
            c["zlr_item_id"] = item_id
            c["citation"] = row.get("citation")
            c["case_name"] = row.get("case_name")
            c["taxonomy_category"] = row.get("taxonomy_category")
        all_chunks.extend(new_chunks)
        indexed_zlr += 1

    if all_chunks:
        await asyncio.to_thread(index_chunks_in_chroma, all_chunks, "zlr")
        async with _db_pool.acquire() as conn:
            for c in all_chunks:
                await conn.execute("""
                    INSERT INTO chunks (id, firm_id, document_id, matter_id, chunk_source,
                                       text, chunk_index, page_number, zlr_item_id, citation,
                                       case_name, taxonomy_category, created_at)
                    VALUES ($1,$2,$3,'zlr','zlr',$4,$5,$6,$7,$8,$9,$10,NOW())
                    ON CONFLICT (id) DO NOTHING
                """,
                c["id"], FIRM_ID, _uuid_mod.UUID(c["document_id"]),
                c["text"], c["chunk_index"], c.get("page_number", 1),
                c.get("zlr_item_id"), c.get("citation"),
                c.get("case_name"), c.get("taxonomy_category")
                )

    return {
        "zlr_entries_processed": indexed_zlr,
        "chunks_created": len(all_chunks),
    }

@app.post("/api/admin/reclassify-zlr")
async def reclassify_zlr(request: Request):
    require_admin_token(request)
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, raw_text, filename FROM zlr_entries WHERE firm_id=$1", FIRM_ID)
    updated = 0
    for row in rows:
        if not row["raw_text"]:
            continue
        ai_meta = await asyncio.to_thread(classify_case_with_ai, row["raw_text"], row["filename"] or "")
        if ai_meta and ai_meta.get("taxonomy_category") and ai_meta["taxonomy_category"] != "General":
            async with _db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE zlr_entries SET taxonomy_category=$1, summary=$2 WHERE id=$3",
                    ai_meta["taxonomy_category"], ai_meta.get("summary"), row["id"]
                )
            updated += 1
    return {"reclassified": updated, "total": len(rows)}

# ── Matters ───────────────────────────────────────────────────────────────────

@app.get("/api/matters")
async def list_matters(request: Request):
    user = await get_current_user(request)
    _check_permission(user, "matter:read")
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM matters WHERE firm_id=$1 ORDER BY last_activity DESC NULLS LAST, created_at DESC",
            FIRM_ID
        )
    matters = []
    for row in rows:
        m = _row_to_matter(row)
        # Attach progress notes
        async with _db_pool.acquire() as conn:
            note_rows = await conn.fetch(
                "SELECT * FROM progress_notes WHERE matter_id=$1 ORDER BY created_at ASC",
                row["id"]
            )
        m["progress_notes"] = [_row_to_note(n) for n in note_rows]
        matters.append(m)
    return matters

@app.post("/api/matters")
async def create_matter(matter: MatterCreate, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "matter:create")
    mid = _uuid_mod.uuid4()
    now = datetime.utcnow()
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO matters (id, firm_id, name, number, internal_ref, external_ref,
                                 client_name, matter_type, status, custom_status,
                                 last_activity, created_at, created_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            RETURNING *
        """,
        mid, FIRM_ID,
        matter.name, matter.number or matter.internal_ref,
        matter.internal_ref, matter.external_ref,
        matter.client_name, matter.matter_type,
        matter.status or "Active", matter.custom_status,
        now, now,
        _uuid_mod.UUID(str(user["id"])) if user.get("id") else None
        )
    m = _row_to_matter(row)
    m["progress_notes"] = []
    return m

@app.get("/api/matters/template")
async def download_matter_template():
    tpl = os.path.join(frontend_path, "MutemoDesk_Matter_Import_Template.docx")
    if os.path.exists(tpl):
        return FileResponse(tpl, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            filename="MutemoDesk_Matter_Import_Template.docx")
    raise HTTPException(status_code=404, detail="Template not found")

@app.get("/api/matters/template-excel")
async def download_matter_template_excel():
    tpl = os.path.join(frontend_path, "MutemoDesk_Matter_Import_Template.xlsx")
    if os.path.exists(tpl):
        return FileResponse(tpl, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            filename="MutemoDesk_Matter_Import_Template.xlsx")
    raise HTTPException(status_code=404, detail="Template not found")

@app.patch("/api/matters/{matter_id}")
async def update_matter(matter_id: str, update: MatterUpdate, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "matter:edit")
    fields = {k: v for k, v in update.dict().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    fields["last_activity"] = datetime.utcnow()
    set_clauses = ", ".join(f"{k}=${i+2}" for i, k in enumerate(fields.keys()))
    values = list(fields.values())
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE matters SET {set_clauses} WHERE id=$1 AND firm_id=${len(values)+2} RETURNING *",
            _uuid_mod.UUID(matter_id), *values, FIRM_ID
        )
    if not row:
        raise HTTPException(status_code=404, detail="Matter not found")
    m = _row_to_matter(row)
    async with _db_pool.acquire() as conn:
        note_rows = await conn.fetch(
            "SELECT * FROM progress_notes WHERE matter_id=$1 ORDER BY created_at ASC",
            _uuid_mod.UUID(matter_id)
        )
    m["progress_notes"] = [_row_to_note(n) for n in note_rows]
    return m

@app.post("/api/matters/{matter_id}/notes")
async def add_progress_note(matter_id: str, note: ProgressNote, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "note:create")
    async with _db_pool.acquire() as conn:
        matter = await conn.fetchrow(
            "SELECT id, name, internal_ref FROM matters WHERE id=$1 AND firm_id=$2",
            _uuid_mod.UUID(matter_id), FIRM_ID
        )
    if not matter:
        raise HTTPException(status_code=404, detail="Matter not found")

    author = note.author or (user.get("display_name") if user else None) or "Unknown"
    now = datetime.utcnow()
    nid = _uuid_mod.uuid4()

    async with _db_pool.acquire() as conn:
        note_row = await conn.fetchrow("""
            INSERT INTO progress_notes (id, matter_id, firm_id, text, author, user_id, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *
        """,
        nid, _uuid_mod.UUID(matter_id), FIRM_ID, note.text, author,
        _uuid_mod.UUID(str(user["id"])) if user.get("id") else None, now
        )
        await conn.execute(
            "UPDATE matters SET last_activity=$1 WHERE id=$2",
            now, _uuid_mod.UUID(matter_id)
        )

    entry = _row_to_note(note_row)

    # Quietly scan the note for actionable dates
    detected_dates = []
    try:
        today = datetime.utcnow().date().isoformat()
        matter_name = matter["name"]
        internal_ref = matter["internal_ref"] or ""

        def scan_note_sync():
            msg = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=400,
                messages=[{"role": "user", "content": f"""Scan this legal progress note for any specific dates, deadlines, or appointments mentioned.
Today is {today}.

Return ONLY valid JSON — no other text:
{{
  "dates": [
    {{
      "title": "brief description of the action",
      "date": "YYYY-MM-DD",
      "time": "HH:MM or null",
      "event_type": "deadline|hearing|meeting|filing|other"
    }}
  ]
}}

If no specific dates are mentioned, return {{"dates": []}}.
Only include dates with a specific day — ignore vague references like "next week" or "soon".

Note text: {note.text}

JSON:"""}]
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r'^```json\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
            parsed = json.loads(raw)
            return parsed.get("dates", [])

        detected_dates = await asyncio.to_thread(scan_note_sync)
        for d in detected_dates:
            d["matter_id"] = matter_id
            d["matter_name"] = matter_name
            d["internal_ref"] = internal_ref
            d["source"] = "progress_note"
    except Exception as e:
        print(f"[notes] date scan failed: {e}")
        detected_dates = []

    return {**entry, "detected_dates": detected_dates}

@app.delete("/api/matters/{matter_id}/notes/{note_id}")
async def delete_progress_note(matter_id: str, note_id: str, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "note:delete")
    async with _db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM progress_notes WHERE id=$1 AND matter_id=$2 AND firm_id=$3",
            _uuid_mod.UUID(note_id), _uuid_mod.UUID(matter_id), FIRM_ID
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Note not found")
    return {"deleted": True}

@app.delete("/api/matters/{matter_id}")
async def delete_matter(matter_id: str, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "matter:delete")
    async with _db_pool.acquire() as conn:
        # Get chunk IDs for ChromaDB cleanup
        chunk_rows = await conn.fetch(
            "SELECT id FROM chunks WHERE matter_id=$1 AND firm_id=$2",
            matter_id, FIRM_ID
        )
        chunk_ids = [r["id"] for r in chunk_rows]
        result = await conn.execute(
            "DELETE FROM matters WHERE id=$1 AND firm_id=$2",
            _uuid_mod.UUID(matter_id), FIRM_ID
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Matter not found")
    if chunk_ids:
        await asyncio.to_thread(remove_chunks_from_chroma, chunk_ids, "firm")
    return {"deleted": True}

# ── Bulk Matter Import ─────────────────────────────────────────────────────────

@app.post("/api/matters/bulk-import")
async def bulk_import_matters(file: UploadFile = File(...), request: Request = None):
    if request:
        user = await get_current_user(request)
        _check_permission(user, "matter:create")
    content = await file.read()
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ("docx", "doc", "xlsx", "xlsm"):
        raise HTTPException(status_code=422, detail="Only .docx, .doc, .xlsx or .xlsm files supported")

    VALID_STATUSES = {"Active", "Awaiting Client", "Awaiting Court", "On Hold", "Closed"}
    LAW_TYPE_MAP = {
        "matrimonial": "matrimonial", "divorce": "matrimonial",
        "estate": "estate", "inheritance": "estate",
        "trust": "trust",
        "conveyancing": "conveyancing", "transfer": "conveyancing",
        "eviction": "eviction",
        "labour": "employment", "employment": "employment",
        "criminal": "criminal",
        "debt": "debt_collection", "debt collection": "debt_collection",
        "mining": "mining",
        "company": "company_law", "commercial": "commercial_contract",
        "property": "commercial_property", "land": "commercial_property",
        "family": "family_law", "custody": "family_law", "guardianship": "family_law",
        "lease": "eviction", "constitutional": "constitutional",
    }

    def detect_matter_type(law_text: str) -> str:
        if not law_text:
            return "other"
        law_lower = law_text.lower()
        for key, val in LAW_TYPE_MAP.items():
            if key in law_lower:
                return val
        return "other"

    def detect_status(next_action: str, action_done: str) -> str:
        combined = (f"{next_action} {action_done}").lower()
        if any(w in combined for w in ["n/a", "file closed", "closed file", "client passed", "passed away", "deceased"]):
            return "Closed"
        if any(w in combined for w in ["awaiting client", "awaiting further instructions", "awaiting instructions"]):
            return "Awaiting Client"
        if any(w in combined for w in ["awaiting set down", "awaiting court", "awaiting hearing", "awaiting order", "awaiting judgment"]):
            return "Awaiting Court"
        if any(w in combined for w in ["on hold", "sleeping dogs", "in abeyance"]):
            return "On Hold"
        return "Active"

    def build_matter_dict(internal_ref, client_name, subject, law_text, external_ref,
                          action_done, next_action, raw_status, latest_comm):
        if not client_name and not internal_ref:
            return None, "No client name or internal ref"
        if client_name and subject:
            matter_name = f"{client_name} — {subject}"
        elif client_name:
            matter_name = client_name
        elif subject:
            matter_name = subject
        else:
            matter_name = internal_ref
        status = raw_status if raw_status in VALID_STATUSES else detect_status(next_action or "", action_done or "")
        matter_type = detect_matter_type(law_text or "")
        now = datetime.utcnow()
        mid = _uuid_mod.uuid4()
        notes = []
        if action_done and str(action_done).lower() not in ("", "n/a", "-"):
            notes.append({"text": f"Action done: {action_done}", "author": "Import"})
        if next_action and str(next_action).lower() not in ("", "n/a", "-"):
            notes.append({"text": f"Next action: {next_action}", "author": "Import"})
        if latest_comm and str(latest_comm).strip():
            notes.append({"text": f"Latest communication: {latest_comm}", "author": "Import"})
        return {
            "id": mid, "name": matter_name, "number": internal_ref,
            "internal_ref": internal_ref or "", "external_ref": external_ref or "",
            "client_name": client_name or "", "matter_type": matter_type,
            "status": status, "custom_status": "",
            "created_at": now, "last_activity": now,
            "document_count": 0, "notes": notes,
        }, None

    created = []
    skipped = []
    matters_to_insert = []

    if ext in ("xlsx", "xlsm"):
        import openpyxl, io as _io
        wb = openpyxl.load_workbook(_io.BytesIO(content), data_only=True, read_only=True)
        ws = wb.active
        header_row = None
        header_map = {}
        COL_ALIASES = {
            "internal ref": "internal_ref", "file name": "internal_ref",
            "client name": "client_name", "client": "client_name",
            "matter description": "subject", "matter": "subject", "re": "subject",
            "opposing party": "opposing", "opposing party / re": "subject",
            "area of law": "law_type", "law": "law_type",
            "external ref": "external_ref", "case number": "external_ref",
            "status": "status", "action done": "action_done",
            "next action": "next_action",
            "latest communication": "latest_comm", "latest": "latest_comm",
        }
        for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            row_vals = [str(c).lower().strip().rstrip("*").strip() if c else "" for c in row]
            if any(v in COL_ALIASES for v in row_vals):
                header_row = r_idx
                for c_idx, val in enumerate(row_vals):
                    canonical = COL_ALIASES.get(val)
                    if canonical:
                        header_map[c_idx] = canonical
                break
        if not header_row:
            raise HTTPException(status_code=422, detail="Could not find a header row in the Excel file.")
        for row in ws.iter_rows(min_row=header_row + 2, values_only=True):
            if not any(row):
                continue
            def g(field):
                for c_idx, f in header_map.items():
                    if f == field and c_idx < len(row):
                        v = row[c_idx]
                        return str(v).strip() if v is not None else ""
                return ""
            if g("internal_ref").upper().startswith("EXAMPLE"):
                continue
            matter, err = build_matter_dict(
                g("internal_ref"), g("client_name"), g("subject") or g("opposing"),
                g("law_type"), g("external_ref"), g("action_done"),
                g("next_action"), g("status"), g("latest_comm")
            )
            if matter:
                matters_to_insert.append(matter)
            else:
                skipped.append({"reason": err, "row": str(row)[:100]})
        wb.close()
    else:
        import docx as docx_lib, io as _io
        try:
            doc = docx_lib.Document(_io.BytesIO(content))
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not read document: {e}")
        FIELD_MAP = {
            "file name": "internal_ref", "file name / internal ref": "internal_ref",
            "internal ref": "internal_ref",
            "name of client": "client_name", "client": "client_name",
            "re": "subject", "re (opposing party / subject)": "subject",
            "area of law": "law_type", "law": "law_type",
            "external reference": "external_ref", "case number": "external_ref",
            "action done": "action_done", "next action": "next_action",
            "status": "status",
            "latest communication": "latest_communication", "latest": "latest_communication",
        }
        for table in doc.tables:
            fields = {}
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if len(cells) >= 2:
                    label = cells[0].lower().strip().rstrip(":")
                    value = "\n".join(cells[1:]).strip()
                    canonical = FIELD_MAP.get(label)
                    if canonical:
                        fields[canonical] = value
            matter, err = build_matter_dict(
                fields.get("internal_ref", ""), fields.get("client_name", ""),
                fields.get("subject", ""), fields.get("law_type", ""),
                fields.get("external_ref", ""), fields.get("action_done", ""),
                fields.get("next_action", ""), fields.get("status", ""),
                fields.get("latest_communication", ""),
            )
            if matter:
                matters_to_insert.append(matter)
            else:
                skipped.append({"reason": err, "fields": {k: v[:50] for k, v in fields.items()}})

    # Bulk insert into PostgreSQL
    async with _db_pool.acquire() as conn:
        for m in matters_to_insert:
            row = await conn.fetchrow("""
                INSERT INTO matters (id, firm_id, name, number, internal_ref, external_ref,
                                     client_name, matter_type, status, custom_status,
                                     last_activity, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *
            """,
            m["id"], FIRM_ID, m["name"], m["number"], m["internal_ref"], m["external_ref"],
            m["client_name"], m["matter_type"], m["status"], m["custom_status"],
            m["last_activity"], m["created_at"]
            )
            for note in m.get("notes", []):
                await conn.execute("""
                    INSERT INTO progress_notes (matter_id, firm_id, text, author, created_at)
                    VALUES ($1,$2,$3,$4,$5)
                """, m["id"], FIRM_ID, note["text"], note["author"], m["created_at"])
            created.append({
                "id": str(m["id"]), "name": m["name"],
                "internal_ref": m["internal_ref"], "client_name": m["client_name"],
                "status": m["status"], "matter_type": m["matter_type"]
            })

    return {"created": len(created), "skipped": len(skipped), "matters": created, "skipped_details": skipped}

# ── Documents ─────────────────────────────────────────────────────────────────

@app.get("/api/matters/{matter_id}/documents")
async def list_documents(matter_id: str, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "matter:read")
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM documents WHERE matter_id=$1 AND firm_id=$2 ORDER BY uploaded_at DESC",
            _uuid_mod.UUID(matter_id), FIRM_ID
        )
    return [_row_to_doc(r) for r in rows]

async def _process_document_background(doc_id: str, matter_id: str, content: bytes, filename: str, ext: str):
    """
    Background task: extract text, classify, chunk, and index a document.
    Updates the document record in PostgreSQL when complete.
    This runs after the upload endpoint has already returned 202 to the client.
    """
    text = ""
    word_count = 0
    page_count = 1
    ocr_used = False

    try:
        if ext == "pdf":
            text, page_count, ocr_used = extract_pdf_text(content)
        elif ext in ("docx", "doc"):
            text = extract_docx_text(content)
        elif ext in ("xlsx", "xlsm"):
            text = extract_xlsx_text(content)
        else:
            text = content.decode("utf-8", errors="replace")
        word_count = len(text.split())
    except Exception as e:
        print(f"[upload] text extraction failed for {filename}: {e}")

    metadata = {}
    if text:
        try:
            metadata = await asyncio.to_thread(classify_document_sync, text[:2000])
        except Exception:
            metadata = {}

    chunk_count = 0
    if text:
        new_chunks = chunk_text(text, page_count, doc_id, matter_id)
        for c in new_chunks:
            c["chunk_source"] = "firm"
        if new_chunks:
            await asyncio.to_thread(index_chunks_in_chroma, new_chunks, "firm")
            # Persist chunks to PostgreSQL
            async with _db_pool.acquire() as conn:
                for c in new_chunks:
                    await conn.execute("""
                        INSERT INTO chunks (id, firm_id, document_id, matter_id, chunk_source,
                                           text, chunk_index, page_number, created_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
                        ON CONFLICT (id) DO NOTHING
                    """,
                    c["id"], FIRM_ID, _uuid_mod.UUID(doc_id),
                    matter_id, "firm", c["text"], c["chunk_index"], c.get("page_number", 1)
                    )
            chunk_count = len(new_chunks)

    # Parse doc_date safely
    raw_date = metadata.get("doc_date")
    doc_date = None
    if raw_date:
        try:
            doc_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except Exception:
            doc_date = None

    async with _db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE documents SET
                document_type=$1, matter_type=$2, parties=$3,
                doc_date=$4, court=$5, word_count=$6, page_count=$7,
                chunk_count=$8, ocr_used=$9, status='complete'
            WHERE id=$10 AND firm_id=$11
        """,
        metadata.get("document_type"), metadata.get("matter_type"),
        str(metadata.get("parties", "")) if metadata.get("parties") else None,
        doc_date, metadata.get("court"),
        word_count, page_count, chunk_count, ocr_used,
        _uuid_mod.UUID(doc_id), FIRM_ID
        )
        await conn.execute(
            "UPDATE matters SET document_count = document_count + 1, last_activity=NOW() WHERE id=$1 AND firm_id=$2",
            _uuid_mod.UUID(matter_id), FIRM_ID
        )

    print(f"[upload] processed {filename}: {word_count} words, {chunk_count} chunks, ocr={ocr_used}")

@app.post("/api/upload", status_code=202)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    matter_id: str = Form(...),
    request: Request = None,
):
    if request:
        user = await get_current_user(request)
        _check_permission(user, "document:upload")
    else:
        user = None

    async with _db_pool.acquire() as conn:
        matter = await conn.fetchrow(
            "SELECT id FROM matters WHERE id=$1 AND firm_id=$2",
            _uuid_mod.UUID(matter_id), FIRM_ID
        )
    if not matter:
        raise HTTPException(status_code=404, detail="Matter not found")

    content = await file.read()
    filename = file.filename or "document"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "bin"
    doc_id = str(_uuid_mod.uuid4())

    # Insert document record immediately with status='processing'
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO documents (id, matter_id, firm_id, filename, status, uploaded_at, uploaded_by)
            VALUES ($1,$2,$3,$4,'processing',NOW(),$5) RETURNING *
        """,
        _uuid_mod.UUID(doc_id), _uuid_mod.UUID(matter_id), FIRM_ID, filename,
        _uuid_mod.UUID(str(user["id"])) if user and user.get("id") else None
        )

    # Schedule heavy processing in the background — returns immediately to client
    background_tasks.add_task(
        _process_document_background, doc_id, matter_id, content, filename, ext
    )

    return {**_row_to_doc(row), "processing": True,
            "message": "Document received. Text extraction and indexing are running in the background."}

# ── Legal Updates ─────────────────────────────────────────────────────────────

@app.get("/api/legal-updates")
async def list_legal_updates(source_type: Optional[str] = None, request: Request = None):
    if request:
        user = await get_current_user(request)
        _check_permission(user, "matter:read")
    async with _db_pool.acquire() as conn:
        if source_type:
            rows = await conn.fetch(
                "SELECT * FROM legal_updates WHERE firm_id=$1 AND source_type=$2 ORDER BY uploaded_at DESC",
                FIRM_ID, source_type
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM legal_updates WHERE firm_id=$1 ORDER BY uploaded_at DESC",
                FIRM_ID
            )
    return [_row_to_doc(r) for r in rows]

async def _process_legal_update_background(item_id: str, content: bytes, filename: str, ext: str,
                                            source_type: str, source_name: str, reference: str):
    """Background task: extract, classify, chunk, and index a legal update document."""
    text = ""
    word_count = 0
    page_count = 1
    ocr_used = False

    try:
        if ext == "pdf":
            text, page_count, ocr_used = extract_pdf_text(content)
        elif ext in ("docx", "doc"):
            text = extract_docx_text(content)
        else:
            text = content.decode("utf-8", errors="replace")
        word_count = len(text.split())
    except Exception as e:
        print(f"[legal-update] text extraction failed for {filename}: {e}")

    metadata = {}
    if text:
        try:
            metadata = await asyncio.to_thread(classify_document_sync, text[:2000])
        except Exception:
            metadata = {}

    chunk_count = 0
    if text:
        new_chunks = chunk_text(text, page_count, item_id, "legal_updates")
        for c in new_chunks:
            c["chunk_source"] = "legal"
            c["source_type"] = source_type
            c["source_name"] = source_name
            c["reference"] = reference
        if new_chunks:
            await asyncio.to_thread(index_chunks_in_chroma, new_chunks, "legal")
            async with _db_pool.acquire() as conn:
                for c in new_chunks:
                    await conn.execute("""
                        INSERT INTO chunks (id, firm_id, document_id, matter_id, chunk_source,
                                           text, chunk_index, page_number, source_type, source_name, reference, created_at)
                        VALUES ($1,$2,$3,'legal_updates','legal',$4,$5,$6,$7,$8,$9,NOW())
                        ON CONFLICT (id) DO NOTHING
                    """,
                    c["id"], FIRM_ID, _uuid_mod.UUID(item_id),
                    c["text"], c["chunk_index"], c.get("page_number", 1),
                    source_type, source_name, reference
                    )
            chunk_count = len(new_chunks)

    raw_date = metadata.get("doc_date")
    doc_date = None
    if raw_date:
        try:
            doc_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except Exception:
            doc_date = None

    async with _db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE legal_updates SET
                document_type=$1, matter_type=$2, doc_date=$3, court=$4,
                word_count=$5, chunk_count=$6, ocr_used=$7,
                status=CASE WHEN $5 > 0 THEN 'complete' ELSE 'error' END,
                error_message=CASE WHEN $5 = 0 THEN 'Could not extract text' ELSE NULL END
            WHERE id=$8 AND firm_id=$9
        """,
        metadata.get("document_type"), metadata.get("matter_type"), doc_date,
        metadata.get("court"), word_count, chunk_count, ocr_used,
        _uuid_mod.UUID(item_id), FIRM_ID
        )

@app.post("/api/legal-updates/upload", status_code=202)
async def upload_legal_update(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    source_type: str = Form(...),
    source_name: str = Form(""),
    reference: str = Form(""),
    source_url: str = Form(""),
    scraped_at: str = Form(""),
    request: Request = None,
):
    if request:
        user = await get_current_user(request)
        _check_permission(user, "legal:upload")

    content = await file.read()
    filename = file.filename or "document"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "bin"
    item_id = str(_uuid_mod.uuid4())

    # Parse scraped_at timestamp if provided by the feed service
    scraped_at_ts = None
    if scraped_at:
        try:
            scraped_at_ts = datetime.fromisoformat(scraped_at.replace("Z", "+00:00"))
        except ValueError:
            scraped_at_ts = None

    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO legal_updates
                (id, firm_id, filename, source_type, source_name, reference,
                 source_url, scraped_at, status, uploaded_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'processing',NOW()) RETURNING *
        """,
        _uuid_mod.UUID(item_id), FIRM_ID, filename, source_type, source_name, reference,
        source_url or None, scraped_at_ts
        )

    background_tasks.add_task(
        _process_legal_update_background, item_id, content, filename, ext,
        source_type, source_name, reference
    )

    return {**_row_to_doc(row), "processing": True,
            "message": "Document received. Indexing is running in the background."}

@app.delete("/api/legal-updates/{item_id}")
async def delete_legal_update(item_id: str, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "legal:delete")
    async with _db_pool.acquire() as conn:
        chunk_rows = await conn.fetch(
            "SELECT id FROM chunks WHERE document_id=$1 AND firm_id=$2",
            _uuid_mod.UUID(item_id), FIRM_ID
        )
        chunk_ids = [r["id"] for r in chunk_rows]
        result = await conn.execute(
            "DELETE FROM legal_updates WHERE id=$1 AND firm_id=$2",
            _uuid_mod.UUID(item_id), FIRM_ID
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Not found")
    if chunk_ids:
        await asyncio.to_thread(remove_chunks_from_chroma, chunk_ids, "legal")
    return {"deleted": True}

@app.post("/api/legal-updates/search")
async def search_legal_updates(req: LegalUpdateSearchRequest, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "search")
    async with _db_pool.acquire() as conn:
        chunk_rows = await conn.fetch(
            "SELECT * FROM chunks WHERE firm_id=$1 AND chunk_source='legal'",
            FIRM_ID
        )
    chunks = [dict(r) for r in chunk_rows]
    if not chunks:
        return {"answer": None, "results": [], "message": "No legislation or case law indexed yet."}

    query_words = set(req.query.lower().split())
    scored = []
    async with _db_pool.acquire() as conn:
        items_rows = await conn.fetch("SELECT * FROM legal_updates WHERE firm_id=$1", FIRM_ID)
    items_map = {str(r["id"]): dict(r) for r in items_rows}

    for chunk in chunks:
        if req.source_type and chunk.get("source_type") != req.source_type:
            continue
        item = items_map.get(str(chunk["document_id"]), {})
        chunk_words = set(chunk["text"].lower().split())
        overlap = len(query_words & chunk_words)
        total = len(query_words | chunk_words)
        score = overlap / total if total > 0 else 0
        if req.query.lower() in chunk["text"].lower():
            score += 0.3
        if score > 0:
            scored.append((score, chunk, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:req.limit]
    if not top:
        return {"answer": None, "results": [], "message": f'No relevant results for: "{req.query}"'}

    results = []
    for score, chunk, item in top:
        results.append({
            "chunk_id": chunk["id"], "text": chunk["text"],
            "similarity": round(score, 3),
            "document_id": str(chunk["document_id"]),
            "filename": item.get("filename", "Unknown"),
            "source_type": item.get("source_type"),
            "source_name": item.get("source_name"),
            "reference": item.get("reference"),
            "document_type": item.get("document_type"),
            "doc_date": str(item["doc_date"]) if item.get("doc_date") else None,
            "court": item.get("court"),
            "page_number": chunk.get("page_number"),
            "chunk_index": chunk.get("chunk_index"),
        })
    return {"answer": None, "results": results}

# ── Text extraction helpers ───────────────────────────────────────────────────

def extract_pdf_text(content: bytes):
    """Extract text from PDF. Falls back to OCR for scanned/image-only pages.
    Returns (text, page_count, ocr_used)"""
    try:
        import pdfplumber, io
        pages = []
        needs_ocr_pages = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for i, page in enumerate(pdf.pages):
                t = page.extract_text()
                if t and t.strip():
                    pages.append(t)
                else:
                    pages.append(None)
                    needs_ocr_pages.append(i)
        total_pages = len(pages)
        ocr_used = bool(needs_ocr_pages)
        if needs_ocr_pages:
            ocr_results = ocr_pdf_pages(content, needs_ocr_pages)
            for i, ocr_text in ocr_results.items():
                pages[i] = ocr_text
        final_pages = [p for p in pages if p and p.strip()]
        return "\n\n".join(final_pages), max(1, total_pages), ocr_used
    except Exception:
        return content.decode("utf-8", errors="replace"), 1, False

def ocr_pdf_pages(content: bytes, page_indices: list) -> dict:
    import subprocess as sp
    results = {}
    if not page_indices:
        return results
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        with open(pdf_path, "wb") as f:
            f.write(content)
        for idx in page_indices:
            page_num = idx + 1
            try:
                img_prefix = os.path.join(tmpdir, f"page_{page_num}")
                sp.run(["pdftoppm", "-png", "-r", "200", "-f", str(page_num), "-l", str(page_num), pdf_path, img_prefix],
                       capture_output=True, timeout=60, check=False)
                candidates = [f"{img_prefix}-{page_num}.png", f"{img_prefix}.png", f"{img_prefix}-1.png"]
                img_path = next((c for c in candidates if os.path.exists(c)), None)
                if not img_path:
                    for fn in os.listdir(tmpdir):
                        if fn.startswith(f"page_{page_num}") and fn.endswith(".png"):
                            img_path = os.path.join(tmpdir, fn)
                            break
                if not img_path:
                    continue
                ocr_result = sp.run(["tesseract", img_path, "stdout", "-l", "eng"],
                                    capture_output=True, text=True, timeout=60, check=False)
                text = ocr_result.stdout.strip()
                if text:
                    results[idx] = text
            except Exception:
                continue
    return results

def extract_xlsx_text(content: bytes):
    try:
        import openpyxl, io
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        lines = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            lines.append(f"=== Sheet: {sheet_name} ===")
            for row in ws.iter_rows(values_only=True):
                if all(cell is None for cell in row):
                    continue
                row_text = " | ".join(str(cell) if cell is not None else "" for cell in row).strip(" |")
                if row_text:
                    lines.append(row_text)
        wb.close()
        return "\n".join(lines)
    except Exception as e:
        print(f"[extract_xlsx_text] failed: {e}")
        return ""

def extract_docx_text(content: bytes):
    if content[:4] == b'\xd0\xcf\x11\xe0':
        try:
            import subprocess, tempfile
            with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                result = subprocess.run(["antiword", tmp_path], capture_output=True, text=True, timeout=30)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        except Exception as e:
            print(f"[extract_docx_text] antiword failed: {e}")
        return ""
    try:
        import docx, io
        doc = docx.Document(io.BytesIO(content))
        return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
    except Exception:
        return ""

def chunk_text(text: str, page_count: int, doc_id: str, matter_id: str) -> list:
    CHUNK_WORDS = 500
    OVERLAP_WORDS = 50
    words = text.split()
    if not words:
        return []
    chunks = []
    start = 0
    idx = 0
    while start < len(words):
        end = min(start + CHUNK_WORDS, len(words))
        chunk_str = " ".join(words[start:end])
        progress = start / len(words)
        page_num = max(1, round(progress * page_count))
        chunks.append({
            "id": str(_uuid_mod.uuid4()),
            "document_id": doc_id,
            "matter_id": matter_id,
            "text": chunk_str,
            "chunk_index": idx,
            "page_number": page_num,
        })
        idx += 1
        start = end - OVERLAP_WORDS
        if start >= len(words) - OVERLAP_WORDS:
            break
    return chunks

def index_chunks_in_chroma(chunks: list, collection_type: str = "firm"):
    if not chunks:
        return
    try:
        firm_col, legal_col, zlr_col = get_chroma_collections()
        collection = {"firm": firm_col, "legal": legal_col, "zlr": zlr_col}.get(collection_type, firm_col)
        texts = [c["text"] for c in chunks]
        ids = [c["id"] for c in chunks]
        embeddings = embed_texts(texts)
        metadatas = [{
            "document_id": c["document_id"],
            "matter_id": c.get("matter_id", "zlr"),
            "chunk_index": c["chunk_index"],
            "page_number": c.get("page_number") or 0,
        } for c in chunks]
        collection.add(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    except Exception as e:
        print(f"[vector_store] failed to index chunks ({collection_type}): {e}")

def remove_chunks_from_chroma(chunk_ids: list, collection_type: str = "firm"):
    if not chunk_ids:
        return
    try:
        firm_col, legal_col, zlr_col = get_chroma_collections()
        collection = {"firm": firm_col, "legal": legal_col, "zlr": zlr_col}.get(collection_type, firm_col)
        collection.delete(ids=chunk_ids)
    except Exception as e:
        print(f"[vector_store] failed to remove chunks ({collection_type}): {e}")

def classify_document_sync(text_preview: str) -> dict:
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            messages=[{"role": "user", "content": f"""Zimbabwean law firm document classifier.
Return ONLY valid JSON with keys:
document_type, parties, matter_type, doc_date (YYYY-MM-DD or null), court (or null)

document_type options: affidavit, founding_affidavit, opposing_affidavit, replying_affidavit, lease_agreement, heads_of_argument, correspondence, court_order, summons, declaration, plea, notice_of_motion, deed_of_settlement, power_of_attorney, will_and_testament, contract, opinion, other

matter_type options: eviction, estate, employment, commercial_property, commercial_contract, customary_law, matrimonial, company_law, criminal, constitutional, other

Excerpt:
{text_preview[:2000]}

JSON only:"""}]
        )
        raw = msg.content[0].text
        m = re.search(r'\{[\s\S]*\}', raw)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}

# ── Zimbabwe Law Reports Index ─────────────────────────────────────────────────

JURISDICTION_MAP = {
    "ZLR": "Zimbabwe", "ZimLII": "Zimbabwe", "SC": "Zimbabwe",
    "SADC": "SADC", "ECOWAS": "ECOWAS",
    "UKSC": "United Kingdom", "UKHL": "United Kingdom",
    "NZCA": "New Zealand", "NZSC": "New Zealand",
    "HCA": "Australia", "FCAFC": "Australia",
    "SCA": "South Africa", "ZACC": "South Africa", "ZASCA": "South Africa",
}
AUTHORITY_WEIGHT = {
    "Zimbabwe": "Binding", "SADC": "Persuasive", "ECOWAS": "Persuasive",
    "United Kingdom": "Persuasive", "New Zealand": "Persuasive",
    "Australia": "Persuasive", "South Africa": "Persuasive", "Other": "Persuasive",
}

def get_jurisdiction(source: str) -> str:
    return JURISDICTION_MAP.get(source, "Other")

def get_authority_weight(source: str) -> str:
    return AUTHORITY_WEIGHT.get(get_jurisdiction(source), "Persuasive")

ZLR_SUBJECT_TAXONOMY = {
    "constitutional": "Constitutional Law",
    "administrative": "Administrative Law & Review",
    "civil procedure": "Civil Procedure",
    "appeal": "Appeals & Review",
    "contract": "Contract Law",
    "property": "Property Law",
    "family": "Family Law & Matrimonial",
    "matrimonial": "Family Law & Matrimonial",
    "customary": "Customary Law & Succession",
    "succession": "Customary Law & Succession",
    "company": "Company & Commercial Law",
    "commercial": "Company & Commercial Law",
    "employment": "Employment & Labour Law",
    "labour": "Employment & Labour Law",
    "delict": "Delict",
    "criminal": "Criminal Law & Procedure",
    "revenue": "Revenue & Tax Law",
    "tax": "Revenue & Tax Law",
    "insolvency": "Insolvency & Sequestration",
    "liquidation": "Insolvency & Sequestration",
    "intellectual property": "Intellectual Property",
    "mining": "Environmental & Mining Law",
    "environmental": "Environmental & Mining Law",
    "human rights": "Human Rights",
    "stock exchange": "Company & Commercial Law",
    "banking": "Company & Commercial Law",
    "land": "Property Law",
    "evidence": "Civil Procedure",
    "prescription": "Civil Procedure",
    "costs": "Civil Procedure",
    "interdict": "Civil Procedure",
    "urgent": "Civil Procedure",
}

def classify_zlr_subject(subject_chains: list) -> str:
    text = " ".join(subject_chains).lower()
    for keyword, category in ZLR_SUBJECT_TAXONOMY.items():
        if keyword in text:
            return category
    return "General"

def parse_zlr_headnote(text: str) -> dict:
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    result = {
        "citation": None, "case_name": None, "court": None,
        "judgment_number": None, "judge": None, "case_type": None,
        "hearing_date": None, "judgment_date": None,
        "subject_chains": [], "taxonomy_category": None,
        "summary": None, "zimlii_url": None,
    }
    for line in lines:
        if re.search(r'\d{4}\s*\(\d+\)\s*ZLR\s*\d+', line):
            result["citation"] = line.strip()
            break
    for line in lines:
        m = re.search(r'(?:Judgment No\.?\s*)?((?:HH|SC|CCZ|LC|HB|HM|HMT)[-\s]?\d+[-/]\d+)', line, re.IGNORECASE)
        if m:
            result["judgment_number"] = m.group(1).strip()
            break
    courts = ["High Court, Harare", "High Court, Bulawayo", "High Court, Masvingo",
              "High Court, Mutare", "Supreme Court", "Constitutional Court",
              "Labour Court", "Administrative Court", "Magistrates Court"]
    for line in lines:
        for court in courts:
            if court.lower() in line.lower():
                result["court"] = court
                break
    for line in lines[:5]:
        if re.search(r'\bv\b', line, re.IGNORECASE) and len(line) > 10:
            if not re.search(r'\d{4}.*ZLR', line):
                result["case_name"] = line.strip()
                break
    for line in lines:
        if re.search(r'\b(J|JA|CJ|DCJ|AJA|JP|AJ)\b$', line.strip()):
            result["judge"] = line.strip()
            break
    case_types = ["Chamber application", "Urgent application", "Appeal", "Review",
                  "Action", "Application", "Trial", "Motion"]
    for line in lines:
        for ct in case_types:
            if ct.lower() == line.lower().strip():
                result["case_type"] = ct
                break
    for line in lines:
        if "Date of Judgment" in line or "Judgment date" in line.lower():
            result["judgment_date"] = re.sub(r'Date of Judgment:?\s*', '', line).strip()
        elif re.search(r'\d+\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}', line):
            if not result["hearing_date"]:
                result["hearing_date"] = line.strip()
    chains = []
    for line in lines:
        if (' – ' in line or ' — ' in line or ' - ' in line) and not re.search(r'\d{4}.*ZLR', line):
            chains.append(line.strip())
    for line in lines:
        if re.search(r'[A-Z][a-z]+ (law|procedure|Act|rights) —', line):
            if line not in chains:
                chains.append(line.strip())
    result["subject_chains"] = chains
    result["taxonomy_category"] = classify_zlr_subject(chains)
    for line in lines:
        if (len(line) > 50 and ' – ' not in line and ' — ' not in line
                and not re.search(r'\d{4}.*ZLR', line)
                and not re.search(r'(HH|SC|CCZ)-?\d+', line)
                and line != result.get("case_name")
                and not re.search(r'\b(J|JA|CJ)\b$', line)):
            result["summary"] = line.strip()
            break
    return result

def classify_case_with_ai(text: str, filename: str) -> dict:
    categories = [
        "Constitutional Law", "Administrative Law & Review", "Civil Procedure",
        "Appeals & Review", "Contract Law", "Property Law",
        "Family Law & Matrimonial", "Customary Law & Succession",
        "Company & Commercial Law", "Employment & Labour Law", "Delict",
        "Criminal Law & Procedure", "Revenue & Tax Law",
        "Insolvency & Sequestration", "Intellectual Property",
        "Environmental & Mining Law", "Human Rights"
    ]
    text_lower = text.lower()
    keyword_map = {
        "Revenue & Tax Law": ["zimra", "zimbabwe revenue authority", "income tax act", "value added tax", "vat"],
        "Constitutional Law": ["constitutional court", "declaration of rights", "bill of rights", "constitutionality"],
        "Property Law": ["deeds registry", "deed of transfer", "immoveable property", "rei vindicatio", "eviction"],
        "Family Law & Matrimonial": ["divorce", "matrimonial causes", "custody", "maintenance", "lobola"],
        "Administrative Law & Review": ["judicial review", "administrative court", "minister of public service"],
        "Employment & Labour Law": ["labour court", "labour act", "unfair dismissal", "retrenchment", "nec"],
        "Criminal Law & Procedure": ["accused", "state v", "criminal procedure", "magistrate", "bail"],
        "Company & Commercial Law": ["companies act", "cobe act", "shareholders", "liquidation", "winding up"],
    }
    for category, keywords in keyword_map.items():
        if any(kw in text_lower for kw in keywords):
            return {"taxonomy_category": category, "summary": None, "case_type": None, "subject_chains": []}
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": f"""Classify this Zimbabwe case law excerpt.
Return ONLY JSON: {{"taxonomy_category": "...", "summary": "...", "case_type": "...", "subject_chains": []}}
Categories: {', '.join(categories)}
Text: {text[:1500]}
JSON:"""}]
        )
        raw = msg.content[0].text
        m = re.search(r'\{[\s\S]*\}', raw)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}

@app.get("/api/zlr")
async def list_zlr_entries(category: Optional[str] = None, limit: int = 50, request: Request = None):
    if request:
        user = await get_current_user(request)
        _check_permission(user, "matter:read")
    async with _db_pool.acquire() as conn:
        if category:
            rows = await conn.fetch(
                "SELECT * FROM zlr_entries WHERE firm_id=$1 AND taxonomy_category=$2 ORDER BY uploaded_at DESC LIMIT $3",
                FIRM_ID, category, limit
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM zlr_entries WHERE firm_id=$1 ORDER BY uploaded_at DESC LIMIT $2",
                FIRM_ID, limit
            )
    result = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["firm_id"] = str(d["firm_id"])
        if d.get("uploaded_at"):
            d["uploaded_at"] = d["uploaded_at"].isoformat()
        if isinstance(d.get("subject_chains"), str):
            try:
                d["subject_chains"] = json.loads(d["subject_chains"])
            except Exception:
                d["subject_chains"] = []
        result.append(d)
    return result

@app.get("/api/zlr/categories")
async def zlr_categories(request: Request = None):
    if request:
        user = await get_current_user(request)
        _check_permission(user, "matter:read")
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT taxonomy_category, COUNT(*) as count
            FROM zlr_entries WHERE firm_id=$1
            GROUP BY taxonomy_category ORDER BY count DESC
        """, FIRM_ID)
    return [{"category": r["taxonomy_category"], "count": r["count"]} for r in rows]

async def _process_zlr_background(item_id: str, content: bytes, filename: str, ext: str,
                                   source: str, volume_year: Optional[str], zimlii_url: Optional[str]):
    """Background task: parse, classify, chunk, and index a ZLR entry."""
    text = ""
    page_count = 1
    ocr_used = False

    try:
        if ext == "pdf":
            text, page_count, ocr_used = extract_pdf_text(content)
        elif ext in ("docx", "doc"):
            text = extract_docx_text(content)
        elif ext in ("txt", "rtf"):
            text = content.decode("utf-8", errors="replace")
        elif ext in ("jpg", "jpeg", "png", "webp"):
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                import subprocess as sp
                ocr_result = sp.run(["tesseract", tmp_path, "stdout", "-l", "eng"],
                                    capture_output=True, text=True, timeout=60)
                text = ocr_result.stdout.strip()
                ocr_used = True
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        else:
            text = content.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[zlr] text extraction failed for {filename}: {e}")

    if not text:
        async with _db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE zlr_entries SET status='error' WHERE id=$1",
                _uuid_mod.UUID(item_id)
            )
        return

    parsed = parse_zlr_headnote(text)
    if parsed.get("taxonomy_category") == "General" or not parsed.get("summary") or len(parsed.get("subject_chains", [])) == 0:
        ai_meta = await asyncio.to_thread(classify_case_with_ai, text, filename)
        if ai_meta:
            if ai_meta.get("taxonomy_category") and ai_meta["taxonomy_category"] != "General":
                parsed["taxonomy_category"] = ai_meta["taxonomy_category"]
            if ai_meta.get("summary") and not parsed.get("summary"):
                parsed["summary"] = ai_meta["summary"]
            if ai_meta.get("case_type") and not parsed.get("case_type"):
                parsed["case_type"] = ai_meta["case_type"]
            if ai_meta.get("subject_chains") and not parsed.get("subject_chains"):
                parsed["subject_chains"] = ai_meta["subject_chains"]

    jurisdiction = get_jurisdiction(source)
    authority_weight = get_authority_weight(source)
    subject_chains_json = json.dumps(parsed.get("subject_chains", []))

    enriched_text = f"""CASE: {parsed.get('case_name') or ''}
CITATION: {parsed.get('citation') or ''}
JUDGMENT: {parsed.get('judgment_number') or ''}
COURT: {parsed.get('court') or ''}
JUDGE: {parsed.get('judge') or ''}
CATEGORY: {parsed.get('taxonomy_category') or ''}
SUBJECT: {' | '.join(parsed.get('subject_chains', []))}
SUMMARY: {parsed.get('summary') or ''}

FULL TEXT:
{text}"""

    new_chunks = chunk_text(enriched_text, page_count, item_id, "zlr")
    for c in new_chunks:
        c["chunk_source"] = "zlr"
        c["zlr_item_id"] = item_id
        c["citation"] = parsed.get("citation")
        c["case_name"] = parsed.get("case_name")
        c["taxonomy_category"] = parsed.get("taxonomy_category")

    if new_chunks:
        await asyncio.to_thread(index_chunks_in_chroma, new_chunks, "zlr")
        async with _db_pool.acquire() as conn:
            for c in new_chunks:
                await conn.execute("""
                    INSERT INTO chunks (id, firm_id, document_id, matter_id, chunk_source,
                                       text, chunk_index, page_number, zlr_item_id, citation,
                                       case_name, taxonomy_category, created_at)
                    VALUES ($1,$2,$3,'zlr','zlr',$4,$5,$6,$7,$8,$9,$10,NOW())
                    ON CONFLICT (id) DO NOTHING
                """,
                c["id"], FIRM_ID, _uuid_mod.UUID(item_id),
                c["text"], c["chunk_index"], c.get("page_number", 1),
                item_id, c.get("citation"), c.get("case_name"), c.get("taxonomy_category")
                )

    async with _db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE zlr_entries SET
                case_name=$1, citation=$2, judgment_number=$3, court=$4, judge=$5,
                case_type=$6, hearing_date=$7, judgment_date=$8,
                subject_chains=$9::jsonb, taxonomy_category=$10, summary=$11,
                raw_text=$12, word_count=$13, chunk_count=$14, ocr_used=$15,
                jurisdiction=$16, authority_weight=$17,
                zimlii_url=COALESCE($18, zimlii_url)
            WHERE id=$19 AND firm_id=$20
        """,
        parsed.get("case_name") or filename,
        parsed.get("citation"), parsed.get("judgment_number"),
        parsed.get("court"), parsed.get("judge"), parsed.get("case_type"),
        parsed.get("hearing_date"), parsed.get("judgment_date"),
        subject_chains_json, parsed.get("taxonomy_category", "General"),
        parsed.get("summary"), text, len(text.split()), len(new_chunks), ocr_used,
        jurisdiction, authority_weight, zimlii_url or parsed.get("zimlii_url"),
        _uuid_mod.UUID(item_id), FIRM_ID
        )

@app.post("/api/zlr/upload", status_code=202)
async def upload_zlr_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    source: str = Form("ZLR"),
    volume_year: Optional[str] = Form(None),
    zimlii_url: Optional[str] = Form(None),
    request: Request = None,
):
    if request:
        user = await get_current_user(request)
        _check_permission(user, "legal:upload")

    content = await file.read()
    filename = file.filename or "zlr_entry"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "bin"
    item_id = str(_uuid_mod.uuid4())

    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO zlr_entries (id, firm_id, filename, source, volume_year, zimlii_url,
                                     jurisdiction, authority_weight, uploaded_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW()) RETURNING *
        """,
        _uuid_mod.UUID(item_id), FIRM_ID, filename, source, volume_year, zimlii_url,
        get_jurisdiction(source), get_authority_weight(source)
        )

    background_tasks.add_task(
        _process_zlr_background, item_id, content, filename, ext, source, volume_year, zimlii_url
    )

    d = dict(row)
    d["id"] = str(d["id"])
    d["firm_id"] = str(d["firm_id"])
    if d.get("uploaded_at"):
        d["uploaded_at"] = d["uploaded_at"].isoformat()
    return {**d, "processing": True, "message": "ZLR entry received. Parsing and indexing are running in the background."}

@app.post("/api/zlr/bulk-import")
async def bulk_import_zlr(
    file: UploadFile = File(...),
    source: str = Form("ZLR"),
    volume_year: Optional[str] = Form(None),
    request: Request = None,
):
    if request:
        user = await get_current_user(request)
        _check_permission(user, "legal:upload")

    content = await file.read()
    filename = file.filename or "zlr_index"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "bin"

    try:
        if ext in ("docx", "doc"):
            text = extract_docx_text(content)
        elif ext == "pdf":
            text, _, _ = extract_pdf_text(content)
        elif ext in ("txt",):
            text = content.decode("utf-8", errors="replace")
        else:
            raise HTTPException(status_code=422, detail=f"Unsupported file type: {ext}")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not extract text: {e}")

    if not text:
        raise HTTPException(status_code=422, detail="No text extracted from document")

    parsed_cases = await asyncio.to_thread(parse_zlr_subject_index, text, source, volume_year)
    if not parsed_cases:
        raise HTTPException(status_code=422, detail="No cases could be parsed from this document.")

    imported = 0
    all_chunks = []
    async with _db_pool.acquire() as conn:
        for case in parsed_cases:
            item_id = str(_uuid_mod.uuid4())
            case["id"] = item_id
            raw_text = f"""CASE: {case['case_name']}
JUDGMENT: {case['judgment_number']}
COURT: {case['court']}
JUDGE: {case.get('judge') or ''}
DATE: {case.get('judgment_date') or ''}
CATEGORY: {case['taxonomy_category']}
SUBJECT: {' | '.join(case['subject_chains'])}
SUMMARY: {case.get('summary') or ''}"""

            subject_chains_json = json.dumps(case.get("subject_chains", []))
            await conn.execute("""
                INSERT INTO zlr_entries (id, firm_id, filename, source, volume_year,
                    jurisdiction, authority_weight, case_name, judgment_number, court, judge,
                    judgment_date, subject_chains, taxonomy_category, summary, raw_text,
                    word_count, chunk_count, uploaded_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,$15,$16,$17,0,NOW())
            """,
            _uuid_mod.UUID(item_id), FIRM_ID,
            f"{case['case_name']} [{case['judgment_number']}]",
            source, volume_year,
            case.get("jurisdiction", get_jurisdiction(source)),
            case.get("authority_weight", get_authority_weight(source)),
            case.get("case_name"), case.get("judgment_number"),
            case.get("court"), case.get("judge"),
            case.get("judgment_date"), subject_chains_json,
            case.get("taxonomy_category", "General"), case.get("summary"),
            raw_text, len((case.get("summary") or "").split())
            )

            new_chunks = chunk_text(raw_text, 1, item_id, "zlr")
            for c in new_chunks:
                c["chunk_source"] = "zlr"
                c["zlr_item_id"] = item_id
                c["citation"] = case.get("citation")
                c["case_name"] = case.get("case_name")
                c["taxonomy_category"] = case.get("taxonomy_category")
            all_chunks.extend(new_chunks)

            await conn.execute(
                "UPDATE zlr_entries SET chunk_count=$1 WHERE id=$2",
                len(new_chunks), _uuid_mod.UUID(item_id)
            )
            imported += 1

    if all_chunks:
        await asyncio.to_thread(index_chunks_in_chroma, all_chunks, "zlr")
        async with _db_pool.acquire() as conn:
            for c in all_chunks:
                await conn.execute("""
                    INSERT INTO chunks (id, firm_id, document_id, matter_id, chunk_source,
                                       text, chunk_index, page_number, zlr_item_id, citation,
                                       case_name, taxonomy_category, created_at)
                    VALUES ($1,$2,$3,'zlr','zlr',$4,$5,$6,$7,$8,$9,$10,NOW())
                    ON CONFLICT (id) DO NOTHING
                """,
                c["id"], FIRM_ID, _uuid_mod.UUID(c["document_id"]),
                c["text"], c["chunk_index"], c.get("page_number", 1),
                c.get("zlr_item_id"), c.get("citation"), c.get("case_name"), c.get("taxonomy_category")
                )

    from collections import Counter
    categories = Counter(c["taxonomy_category"] for c in parsed_cases)
    return {"imported": imported, "total_parsed": len(parsed_cases), "categories": dict(categories), "source": source, "volume_year": volume_year}

@app.delete("/api/zlr/{item_id}")
async def delete_zlr_entry(item_id: str, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "legal:delete")
    async with _db_pool.acquire() as conn:
        chunk_rows = await conn.fetch(
            "SELECT id FROM chunks WHERE document_id=$1 AND firm_id=$2",
            _uuid_mod.UUID(item_id), FIRM_ID
        )
        chunk_ids = [r["id"] for r in chunk_rows]
        result = await conn.execute(
            "DELETE FROM zlr_entries WHERE id=$1 AND firm_id=$2",
            _uuid_mod.UUID(item_id), FIRM_ID
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Not found")
    if chunk_ids:
        await asyncio.to_thread(remove_chunks_from_chroma, chunk_ids, "zlr")
    return {"deleted": True}

@app.post("/api/zlr/search")
async def search_zlr(req: LegalUpdateSearchRequest, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "search")
    async with _db_pool.acquire() as conn:
        chunk_rows = await conn.fetch(
            "SELECT * FROM chunks WHERE firm_id=$1 AND chunk_source='zlr'",
            FIRM_ID
        )
    zlr_chunks = [dict(r) for r in chunk_rows]
    if not zlr_chunks:
        return {"results": [], "message": "No ZLR entries indexed yet."}
    results = await asyncio.to_thread(_zlr_semantic_search, zlr_chunks, req.query, req.source_type, req.limit)
    return {"results": results, "count": len(results)}

def _zlr_semantic_search(zlr_chunks: list, query: str, category_filter: Optional[str], limit: int) -> list:
    results = []
    try:
        _, _, zlr_col = get_chroma_collections()
        if zlr_col.count() > 0:
            query_vec = embed_texts([query])[0]
            if hasattr(query_vec[0], "__len__"): query_vec = query_vec[0]
            res = zlr_col.query(query_embeddings=[query_vec], n_results=min(limit * 3, zlr_col.count()))
            ids = res["ids"][0] if res["ids"] else []
            distances = res["distances"][0] if res["distances"] else []
            chunk_by_id = {c["id"]: c for c in zlr_chunks}
            seen_items = set()
            for cid, dist in zip(ids, distances):
                chunk = chunk_by_id.get(cid)
                if not chunk:
                    continue
                item_id = str(chunk["document_id"])
                if item_id in seen_items:
                    continue
                if category_filter and chunk.get("taxonomy_category") != category_filter:
                    continue
                seen_items.add(item_id)
                similarity = max(0.0, 1.0 - dist)
                results.append({
                    "item_id": item_id,
                    "similarity": round(similarity, 3),
                    "case_name": chunk.get("case_name"),
                    "citation": chunk.get("citation"),
                    "taxonomy_category": chunk.get("taxonomy_category"),
                    "relevant_excerpt": chunk["text"][:400],
                })
                if len(results) >= limit:
                    break
    except Exception as e:
        print(f"[zlr_search] semantic search failed, using keyword fallback: {e}")
        query_words = set(query.lower().split())
        scored = []
        for chunk in zlr_chunks:
            if category_filter and chunk.get("taxonomy_category") != category_filter:
                continue
            score = len(query_words & set(chunk["text"].lower().split())) / max(len(query_words), 1)
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        for score, chunk in scored[:limit]:
            results.append({
                "item_id": str(chunk["document_id"]),
                "similarity": round(score, 3),
                "case_name": chunk.get("case_name"),
                "citation": chunk.get("citation"),
                "taxonomy_category": chunk.get("taxonomy_category"),
                "relevant_excerpt": chunk["text"][:400],
            })
    return results

def parse_zlr_subject_index(text: str, source: str, volume_year: Optional[str]) -> list:
    """Parse a ZLR 'Cases Decided' subject index into individual case records."""
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    cases = []
    current_subject_chains = []
    i = 0
    while i < len(lines):
        line = lines[i]
        judgment_match = re.search(r'((?:HH|SC|CCZ|LC|HB|HM|HMT)[-\s]?\d+[-/]\d+)', line, re.IGNORECASE)
        if judgment_match:
            judgment_number = judgment_match.group(1).strip()
            case_name = None
            court = None
            judge = None
            judgment_date = None
            if i > 0:
                prev = lines[i-1]
                if re.search(r'\bv\b', prev, re.IGNORECASE) and len(prev) > 10:
                    case_name = prev
            court_keywords = {
                "HH": "High Court, Harare", "HB": "High Court, Bulawayo",
                "HM": "High Court, Masvingo", "HMT": "High Court, Mutare",
                "SC": "Supreme Court", "CCZ": "Constitutional Court", "LC": "Labour Court",
            }
            prefix = judgment_match.group(1)[:2].upper()
            court = court_keywords.get(prefix, "High Court, Harare")
            for j in range(i, min(i+5, len(lines))):
                if re.search(r'\b(J|JA|CJ|DCJ|AJA)\b$', lines[j].strip()):
                    judge = lines[j].strip()
                    break
            for j in range(i, min(i+5, len(lines))):
                if re.search(r'\d+\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}', lines[j]):
                    judgment_date = lines[j].strip()
                    break
            summary_parts = []
            j = i + 1
            while j < min(i + 10, len(lines)):
                next_line = lines[j]
                if re.search(r'((?:HH|SC|CCZ|LC|HB|HM|HMT)[-\s]?\d+[-/]\d+)', next_line, re.IGNORECASE):
                    break
                if next_line.startswith('See below') or next_line.startswith('See above'):
                    j += 1
                    continue
                if len(next_line) > 40:
                    summary_parts.append(next_line)
                j += 1
            taxonomy = classify_zlr_subject(current_subject_chains)
            cases.append({
                'case_name': case_name or f"Case {judgment_number}",
                'judgment_number': judgment_number,
                'court': court,
                'judge': judge,
                'judgment_date': judgment_date,
                'subject_chains': list(current_subject_chains),
                'taxonomy_category': taxonomy,
                'summary': ' '.join(summary_parts)[:600] if summary_parts else None,
                'citation': None,
                'source': source,
                'volume_year': volume_year,
                'jurisdiction': get_jurisdiction(source),
                'authority_weight': get_authority_weight(source),
            })
            current_subject_chains = []
        elif ' – ' in line or ' — ' in line:
            if not re.search(r'\d{4}.*ZLR', line):
                current_subject_chains.append(line)
        i += 1
    return cases

# ── Search ────────────────────────────────────────────────────────────────────

@app.post("/api/search")
async def search_documents(req: SearchRequest, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "search")

    # Load chunks from DB for keyword fallback
    async with _db_pool.acquire() as conn:
        firm_chunk_rows = await conn.fetch(
            "SELECT * FROM chunks WHERE firm_id=$1 AND chunk_source='firm'", FIRM_ID
        )
        legal_chunk_rows = await conn.fetch(
            "SELECT * FROM chunks WHERE firm_id=$1 AND chunk_source='legal'", FIRM_ID
        )
        zlr_chunk_rows = await conn.fetch(
            "SELECT * FROM chunks WHERE firm_id=$1 AND chunk_source='zlr'", FIRM_ID
        )

    firm_chunks = [dict(r) for r in firm_chunk_rows]
    legal_chunks = [dict(r) for r in legal_chunk_rows]
    zlr_chunks_list = [dict(r) for r in zlr_chunk_rows]

    results = await asyncio.to_thread(_semantic_search_firm, req, firm_chunks)
    legal_results = []
    if req.include_legal_updates:
        legal_results = await asyncio.to_thread(_semantic_search_legal, req, legal_chunks)

    zlr_results = []
    if zlr_chunks_list:
        raw_zlr = await asyncio.to_thread(_zlr_semantic_search, zlr_chunks_list, req.query, None, 3)
        for r in raw_zlr:
            zlr_results.append({
                "result_source": "zlr",
                "chunk_id": r.get("item_id"),
                "text": r.get("relevant_excerpt", ""),
                "similarity": r.get("similarity", 0),
                "document_id": r.get("item_id"),
                "filename": r.get("case_name") or r.get("citation") or "ZLR Entry",
                "citation": r.get("citation"),
                "taxonomy_category": r.get("taxonomy_category"),
                "summary": r.get("summary"),
            })

    all_results = results + legal_results + zlr_results
    if not all_results:
        return {"answer": None, "results": [], "message": f'No relevant documents found for: "{req.query}"'}

    answer = await asyncio.to_thread(synthesise_answer_sync, req.query, results[:5], legal_results[:3])
    return {"answer": answer, "results": all_results}

def _semantic_search_firm(req, chunks: list) -> list:
    results = []
    try:
        firm_col, _, _ = get_chroma_collections()
        if firm_col.count() > 0:
            query_vec = embed_texts([req.query])[0]
            if hasattr(query_vec[0], "__len__"): query_vec = query_vec[0]
            where = {}
            if req.matter_id:
                where["matter_id"] = req.matter_id
            n_fetch = max(req.limit * 4, 20)
            query_kwargs = {"query_embeddings": [query_vec], "n_results": n_fetch}
            if where:
                query_kwargs["where"] = where
            res = firm_col.query(**query_kwargs)
            ids = res["ids"][0] if res["ids"] else []
            distances = res["distances"][0] if res["distances"] else []
            chunk_by_id = {c["id"]: c for c in chunks}
            for cid, dist in zip(ids, distances):
                chunk = chunk_by_id.get(cid)
                if not chunk:
                    continue
                similarity = max(0.0, 1.0 - dist)
                results.append({
                    "result_source": "firm",
                    "chunk_id": chunk["id"],
                    "text": chunk["text"],
                    "similarity": round(similarity, 3),
                    "document_id": str(chunk["document_id"]),
                    "matter_id": chunk.get("matter_id"),
                    "page_number": chunk.get("page_number"),
                    "chunk_index": chunk.get("chunk_index"),
                })
                if len(results) >= req.limit:
                    break
    except Exception as e:
        print(f"[search] semantic search failed, falling back to keyword: {e}")
        query_words = set(req.query.lower().split())
        scored = []
        for chunk in chunks:
            score = len(query_words & set(chunk["text"].lower().split())) / max(len(query_words), 1)
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        for score, chunk in scored[:req.limit]:
            results.append({
                "result_source": "firm",
                "chunk_id": chunk["id"],
                "text": chunk["text"],
                "similarity": round(score, 3),
                "document_id": str(chunk["document_id"]),
                "matter_id": chunk.get("matter_id"),
            })
    return results

def _semantic_search_legal(req, chunks: list) -> list:
    results = []
    try:
        _, legal_col, _ = get_chroma_collections()
        if legal_col.count() > 0:
            query_vec = embed_texts([req.query])[0]
            if hasattr(query_vec[0], "__len__"): query_vec = query_vec[0]
            res = legal_col.query(query_embeddings=[query_vec], n_results=min(req.limit * 2, legal_col.count()))
            ids = res["ids"][0] if res["ids"] else []
            distances = res["distances"][0] if res["distances"] else []
            chunk_by_id = {c["id"]: c for c in chunks}
            for cid, dist in zip(ids, distances):
                chunk = chunk_by_id.get(cid)
                if not chunk:
                    continue
                similarity = max(0.0, 1.0 - dist)
                results.append({
                    "result_source": "legal",
                    "chunk_id": chunk["id"],
                    "text": chunk["text"],
                    "similarity": round(similarity, 3),
                    "document_id": str(chunk["document_id"]),
                    "source_type": chunk.get("source_type"),
                    "source_name": chunk.get("source_name"),
                    "reference": chunk.get("reference"),
                })
                if len(results) >= req.limit:
                    break
    except Exception as e:
        print(f"[search] legal semantic search failed: {e}")
    return results

def synthesise_answer_sync(query: str, results: list, legal_results: list) -> str:
    if not results and not legal_results:
        return None
    context_parts = []
    for r in results[:5]:
        context_parts.append(f"[FIRM PRECEDENT — {r.get('document_id','')}]\n{r['text']}")
    for r in (legal_results or [])[:3]:
        ref = r.get("reference") or r.get("source_name") or "Legal Source"
        context_parts.append(f"[{ref}]\n{r['text']}")
    context = "\n\n---\n\n".join(context_parts)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": f"""You are a legal research assistant for {FIRM_NAME}, Harare.

Query: {query}

Sources:
{context}

Answer directly and practically:
- If firm precedents are present, identify patterns and note them by document ID
- If legislation or case law is present, summarise the relevant legal position and cite by reference
- Flag variations over time
- For drafting queries, suggest specific language from the firm precedents

Professional, direct, max 4 paragraphs. Clearly distinguish firm precedent from public legal sources."""}]
        )
        return msg.content[0].text
    except Exception:
        total = len(results) + len(legal_results or [])
        return f"Found {total} relevant excerpt(s). Review the sources below."

# ── Affidavit Generator ───────────────────────────────────────────────────────

AFFIDAVIT_SYSTEM = """You are a legal drafting assistant for {FIRM_NAME}, Harare.
Draft affidavits in proper Zimbabwe High Court form per SI 202/2021.
- Full court caption with case number, party names and designations
- Opening: deponent full name, ID, capacity, competency declaration
- Numbered paragraphs, first person, chronological facts
- Prayer paragraph with specific relief
- Commissioner of oaths block at end
- Use [_____] for unknown specifics"""

@app.post("/api/generate-affidavit")
async def generate_affidavit(req: AffidavitRequest, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "draft:affidavit")
    precedent_block = ""
    if req.precedent_context:
        fname = req.precedent_context.get("filename", "precedent")
        mname = req.precedent_context.get("matter_name", "")
        text = str(req.precedent_context.get("text", ""))[:2000]
        precedent_block = f"\n\nFIRM PRECEDENT ({fname} \u2014 {mname}):\n---\n{text}\n---"
    prompt = f"""Draft a Zimbabwe High Court affidavit.

Matter type: {req.matter_type or 'General'}
Court: {req.court}
Deponent: {req.deponent_name or '[DEPONENT NAME]'}
ID Number: {req.deponent_id or '[ID NUMBER]'}
Capacity: {req.deponent_capacity or 'the Applicant'}
Parties: {req.parties or '[PARTIES]'}
Matter summary: {req.matter_summary}
Key facts: {req.key_facts or 'As per matter summary above'}
Relief sought: {req.relief or '[RELIEF TO BE SPECIFIED]'}
{precedent_block}

Draft the complete affidavit in proper Zimbabwe High Court form. Number all paragraphs. Include the commissioner of oaths block at the end."""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=AFFIDAVIT_SYSTEM.format(FIRM_NAME=FIRM_NAME),
            messages=[{"role": "user", "content": prompt}]
        )
        return {"affidavit": msg.content[0].text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Affidavit generation failed: {e}")

# ── DOCX Export ───────────────────────────────────────────────────────────────

@app.post("/api/export-docx")
async def export_docx(req: ExportRequest, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "draft:document")
    node_path = None
    for candidate in ["/usr/local/bin/node", "/usr/bin/node", "node"]:
        import shutil
        if shutil.which(candidate):
            node_path = candidate
            break
    if not node_path:
        raise HTTPException(status_code=503, detail="Node.js is not available. Copy the affidavit text manually.")

    with tempfile.TemporaryDirectory() as tmpdir:
        script_path = os.path.join(tmpdir, "make_docx.js")
        output_path = os.path.join(tmpdir, f"affidavit_{req.document_id}.docx")
        escaped_text = req.affidavit_text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
        node_script = f"""
const {{ Document, Packer, Paragraph, TextRun, AlignmentType }} = require('docx');
const fs = require('fs');
const text = `{escaped_text}`;
const lines = text.split('\\n');
const paragraphs = lines.map(line => {{
    const isCentered = line.trim().startsWith('IN THE') || line.trim().startsWith('CASE NO') ||
                       line.trim().startsWith('BETWEEN') || line.trim() === '';
    return new Paragraph({{
        alignment: isCentered ? AlignmentType.CENTER : AlignmentType.JUSTIFIED,
        spacing: {{ after: 120 }},
        children: [new TextRun({{
            text: line,
            font: 'Times New Roman',
            size: 24,
            bold: line.trim().startsWith('IN THE') || line.trim().startsWith('CASE NO'),
        }})]
    }});
}});
const doc = new Document({{
    sections: [{{
        properties: {{ page: {{ margin: {{ top: 1440, right: 1440, bottom: 1440, left: 1440 }} }} }},
        children: paragraphs
    }}]
}});
Packer.toBuffer(doc).then(buffer => {{
    fs.writeFileSync('{output_path}', buffer);
    console.log('done');
}});
"""
        with open(script_path, "w") as f:
            f.write(node_script)

        pkg_path = os.path.join(tmpdir, "package.json")
        with open(pkg_path, "w") as f:
            json.dump({"dependencies": {"docx": "^8.5.0"}}, f)

        install = subprocess.run(["npm", "install", "--prefix", tmpdir, "docx"],
                                  capture_output=True, text=True, timeout=60, cwd=tmpdir)
        if install.returncode != 0:
            raise HTTPException(status_code=500, detail="Failed to install docx package")

        result = subprocess.run([node_path, script_path],
                                capture_output=True, text=True, timeout=30, cwd=tmpdir)
        if result.returncode != 0 or not os.path.exists(output_path):
            raise HTTPException(status_code=500, detail=f"DOCX generation failed: {result.stderr[:200]}")

        with open(output_path, "rb") as f:
            docx_bytes = f.read()

    from fastapi.responses import Response as FastAPIResponse
    return FastAPIResponse(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="affidavit_{req.document_id}.docx"'}
    )

# ── Calendar ──────────────────────────────────────────────────────────────────

@app.get("/api/calendar")
async def list_calendar(request: Request):
    user = await get_current_user(request)
    _check_permission(user, "calendar:read")
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM calendar_events WHERE firm_id=$1 ORDER BY date ASC, time ASC NULLS LAST",
            FIRM_ID
        )
    return [_row_to_event(r) for r in rows]

@app.post("/api/calendar")
async def add_calendar_event(event: CalendarEvent, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "calendar:create")
    try:
        datetime.strptime(event.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid date format: {event.date}. Use YYYY-MM-DD.")

    matter_id_uuid = None
    if event.matter_id:
        try:
            matter_id_uuid = _uuid_mod.UUID(event.matter_id)
        except Exception:
            pass

    time_val = None
    if event.time:
        try:
            time_val = datetime.strptime(event.time, "%H:%M").time()
        except ValueError:
            pass

    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO calendar_events (firm_id, matter_id, title, date, time, event_type, court, matter_name, notes, created_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *
        """,
        FIRM_ID, matter_id_uuid, event.title,
        datetime.strptime(event.date, "%Y-%m-%d").date(),
        time_val, event.event_type, event.court, event.matter_name, event.notes,
        _uuid_mod.UUID(str(user["id"])) if user and user.get("id") else None
        )
    return _row_to_event(row)

@app.delete("/api/calendar/{event_id}")
async def delete_calendar_event(event_id: str, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "calendar:delete")
    async with _db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM calendar_events WHERE id=$1 AND firm_id=$2",
            _uuid_mod.UUID(event_id), FIRM_ID
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Event not found")
    return {"deleted": True}

@app.get("/api/calendar/export-ics")
async def export_calendar_ics(request: Request):
    user = await get_current_user(request)
    _check_permission(user, "calendar:read")
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM calendar_events WHERE firm_id=$1 ORDER BY date ASC",
            FIRM_ID
        )
    events = [_row_to_event(r) for r in rows]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Mutemo Desk//{FIRM_NAME}//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for ev in events:
        uid = f"{ev['id']}@mutemodesk"
        dtstart = ev["date"].replace("-", "")
        if ev.get("time"):
            t = ev["time"].replace(":", "")
            dtstart = f"{dtstart}T{t}00"
        summary = ev["title"].replace(",", "\\,").replace(";", "\\;")
        desc = ""
        if ev.get("matter_name"):
            desc += f"Matter: {ev['matter_name']}\\n"
        if ev.get("court"):
            desc += f"Court: {ev['court']}\\n"
        if ev.get("notes"):
            desc += f"Notes: {ev['notes']}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTART:{dtstart}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    ics_content = "\r\n".join(lines) + "\r\n"
    from fastapi.responses import Response as FastAPIResponse
    return FastAPIResponse(
        content=ics_content,
        media_type="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="mutemo_calendar.ics"'}
    )

# ── Date Extraction from Documents ────────────────────────────────────────────

@app.post("/api/extract-dates")
async def extract_dates_from_document(
    file: UploadFile = File(...),
    matter_id: Optional[str] = Form(None),
    matter_name: Optional[str] = Form(None),
    request: Request = None,
):
    if request:
        user = await get_current_user(request)
        _check_permission(user, "calendar:create")

    content = await file.read()
    filename = file.filename or "document"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "bin"

    text = ""
    try:
        if ext == "pdf":
            text, _, _ = await asyncio.to_thread(extract_pdf_text, content)
        elif ext in ("docx", "doc"):
            text = await asyncio.to_thread(extract_docx_text, content)
        else:
            text = content.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not extract text: {e}")

    if not text:
        raise HTTPException(status_code=422, detail="No text could be extracted from this document.")

    today = datetime.utcnow().date().isoformat()

    def extract_dates_sync():
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": f"""Extract all legal deadlines, hearing dates, filing dates, and appointments from this document.
Today is {today}. Focus on specific, actionable dates.

Return ONLY valid JSON:
{{
  "dates": [
    {{
      "title": "brief description",
      "date": "YYYY-MM-DD",
      "time": "HH:MM or null",
      "event_type": "deadline|hearing|meeting|filing|other",
      "party": "which party this applies to, or null",
      "notes": "any additional context"
    }}
  ],
  "document_summary": "one sentence summary of the document"
}}

Document text (first 8000 chars):
{text[:8000]}

JSON:"""}]
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r'^```json\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
        return json.loads(raw)

    try:
        parsed = await asyncio.to_thread(extract_dates_sync)
        dates = parsed.get("dates", [])
        summary = parsed.get("document_summary", "")
        for d in dates:
            d["matter_id"] = matter_id
            d["matter_name"] = matter_name
            d["source_document"] = filename
        return {"dates": dates, "count": len(dates), "document_summary": summary, "filename": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Date extraction failed: {e}")


@app.post("/api/extract-dates-by-id")
async def extract_dates_by_document_id(
    request: Request,
    document_id: str = Form(...),
):
    user = await get_current_user(request)
    _check_permission(user, "calendar:create")

    # Get document record
    async with _db_pool.acquire() as conn:
        doc = await conn.fetchrow(
            "SELECT * FROM documents WHERE id=$1 AND firm_id=$2",
            _uuid_mod.UUID(document_id), FIRM_ID
        )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Get chunks for this document
    async with _db_pool.acquire() as conn:
        chunk_rows = await conn.fetch(
            "SELECT text FROM chunks WHERE document_id=$1 ORDER BY chunk_index ASC",
            _uuid_mod.UUID(document_id)
        )

    if not chunk_rows:
        raise HTTPException(status_code=422, detail="No text content found for this document. It may still be processing.")

    text = " ".join(r["text"] for r in chunk_rows)
    filename = doc["filename"]
    matter_id = str(doc["matter_id"]) if doc["matter_id"] else None

    # Get matter name
    matter_name = None
    if matter_id:
        async with _db_pool.acquire() as conn:
            matter = await conn.fetchrow("SELECT name FROM matters WHERE id=$1", doc["matter_id"])
            if matter:
                matter_name = matter["name"]

    today = datetime.utcnow().date().isoformat()

    def extract_dates_sync():
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": f"""Extract all legal deadlines, hearing dates, filing dates, and appointments from this document.
Today is {today}. Focus on specific, actionable dates.

Return ONLY valid JSON:
{{
  "dates": [
    {{
      "title": "brief description",
      "date": "YYYY-MM-DD",
      "time": "HH:MM or null",
      "event_type": "deadline|hearing|meeting|filing|other",
      "party": "which party this applies to, or null",
      "notes": "any additional context"
    }}
  ],
  "document_summary": "one sentence summary of the document"
}}

Document text:
{text[:8000]}

JSON:"""}]
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r'^```json\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
        return json.loads(raw)

    try:
        parsed = await asyncio.to_thread(extract_dates_sync)
        dates = parsed.get("dates", [])
        summary = parsed.get("document_summary", "")
        for d in dates:
            d["matter_id"] = matter_id
            d["matter_name"] = matter_name
            d["source_document"] = filename
        return {"dates": dates, "count": len(dates), "document_summary": summary, "filename": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Date extraction failed: {e}")

# ── Email / Reminders ─────────────────────────────────────────────────────────

EVENT_TYPE_LABELS = {
    "hearing": "Hearing",
    "deadline": "Deadline",
    "filing": "Filing",
    "meeting": "Client Meeting",
    "manual": "Appointment",
    "other": "Event",
}

def _escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def build_reminder_email_body(events: list) -> tuple:
    """Returns (plain_text_body, html_body). Events must have a 'days_until' field."""
    if not events:
        text = "Good morning. You have no court dates, deadlines, or filings scheduled in the next 30 days.\n\n\u2014 Mutemo Desk"
        html = "<p>Good morning. You have no court dates, deadlines, or filings scheduled in the next 30 days.</p><p style='color:#6b6b64'>\u2014 Mutemo Desk</p>"
        return text, html

    today_items    = [e for e in events if e.get("days_until", 99) == 0]
    tomorrow_items = [e for e in events if e.get("days_until", 99) == 1]
    week_items     = [e for e in events if 1 < (e.get("days_until") or 0) <= 7]
    later_items    = [e for e in events if (e.get("days_until") or 0) > 7]

    def fmt_text(e):
        bits = [EVENT_TYPE_LABELS.get(e.get("event_type"), "Event") + ":", e.get("title", "")]
        if e.get("time"):        bits.append(f"at {e['time']}")
        if e.get("court"):       bits.append(f"\u2014 {e['court']}")
        if e.get("matter_name"): bits.append(f"({e['matter_name']})")
        return " ".join(bits)

    text_lines = ["Good morning. Here is your Mutemo Desk reminder summary:\n"]
    if today_items:
        text_lines.append("TODAY:")
        for e in today_items: text_lines.append(f"  \u2022 {fmt_text(e)}")
        text_lines.append("")
    if tomorrow_items:
        text_lines.append("TOMORROW:")
        for e in tomorrow_items: text_lines.append(f"  \u2022 {fmt_text(e)}")
        text_lines.append("")
    if week_items:
        text_lines.append("LATER THIS WEEK:")
        for e in week_items: text_lines.append(f"  {e['date']}  \u2014  {fmt_text(e)}")
        text_lines.append("")
    if later_items:
        text_lines.append("COMING UP:")
        for e in later_items: text_lines.append(f"  {e['date']}  \u2014  {fmt_text(e)}")
        text_lines.append("")
    text_lines.append("A calendar file (.ics) is attached \u2014 open it to add these to your phone or computer calendar.")
    text_lines.append("\n\u2014 Mutemo Desk")
    text = "\n".join(text_lines)

    def fmt_html(e):
        type_chip = EVENT_TYPE_LABELS.get(e.get("event_type"), "Event")
        meta = []
        if e.get("time"):        meta.append(e["time"])
        if e.get("court"):       meta.append(_escape_html(e["court"]))
        if e.get("matter_name"): meta.append(_escape_html(e["matter_name"]))
        meta_str = " \u00b7 ".join(meta)
        return (
            f'<div style="padding:8px 0;border-bottom:1px solid #e8e4da">'
            f'<span style="font-size:11px;font-weight:700;color:#b8922a;text-transform:uppercase;letter-spacing:0.5px">{type_chip}</span><br/>'
            f'<strong>{_escape_html(e.get("title",""))}</strong><br/>'
            f'<span style="font-size:13px;color:#6b6b64">{meta_str}</span>'
            f'</div>'
        )

    html_sections = []
    if today_items:
        html_sections.append('<h3 style="color:#b83232;margin:16px 0 8px">Today</h3>' + "".join(fmt_html(e) for e in today_items))
    if tomorrow_items:
        html_sections.append('<h3 style="color:#b8922a;margin:16px 0 8px">Tomorrow</h3>' + "".join(fmt_html(e) for e in tomorrow_items))
    if week_items:
        html_sections.append(
            '<h3 style="color:#1b4d2e;margin:16px 0 8px">Later This Week</h3>' +
            "".join(
                f'<div style="padding:8px 0;border-bottom:1px solid #e8e4da">'
                f'<span style="font-size:12px;color:#6b6b64">{e["date"]}</span><br/>{fmt_html(e)}</div>'
                for e in week_items
            )
        )
    if later_items:
        html_sections.append(
            '<h3 style="color:#1b4d2e;margin:16px 0 8px">Coming Up</h3>' +
            "".join(
                f'<div style="padding:8px 0;border-bottom:1px solid #e8e4da">'
                f'<span style="font-size:12px;color:#6b6b64">{e["date"]}</span><br/>{fmt_html(e)}</div>'
                for e in later_items
            )
        )

    html = f"""<div style="font-family:Georgia,serif;color:#1a1a18;max-width:560px">
        <div style="background:#1b4d2e;color:white;padding:16px 20px;border-radius:6px 6px 0 0">
            <strong style="font-size:18px">&#9878; Mutemo Desk</strong><br/>
            <span style="font-size:13px;opacity:0.8">Daily Calendar Reminder &mdash; {FIRM_NAME}</span>
        </div>
        <div style="padding:16px 20px;border:1px solid #d8d3c8;border-top:none;border-radius:0 0 6px 6px">
            <p>Good morning. Here is your reminder summary for the next 30 days.</p>
            {''.join(html_sections)}
            <p style="margin-top:16px;font-size:13px;color:#6b6b64">A calendar file (.ics) is attached &mdash; open it to add these events to your phone or computer calendar.</p>
        </div>
    </div>"""

    return text, html

def build_ics(events: list) -> str:
    """Build an ICS calendar string from a list of event dicts."""
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        f"PRODID:-//Mutemo Desk//{FIRM_NAME}//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
    ]
    for ev in events:
        uid = f"{ev.get('id', ev.get('title','evt'))}@mutemodesk"
        dtstart = str(ev["date"]).replace("-", "")
        if ev.get("time"):
            t = str(ev["time"]).replace(":", "")[:4]
            dtstart = f"{dtstart}T{t}00"
        summary = str(ev.get("title", "")).replace(",", "\\,").replace(";", "\\;")
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTART:{dtstart}", f"SUMMARY:{summary}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"

def is_email_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY") or os.environ.get("SMTP_HOST"))

def _send_via_resend_sync(to: str, subject: str, html_body: str, text_body: str, ics_content: str = None) -> None:
    """Synchronous Resend send (called via asyncio.to_thread)."""
    import base64
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY not configured")
    from_addr = os.environ.get("RESEND_FROM", f"reminders@{os.environ.get('RESEND_FROM_DOMAIN', 'tofamba.com')}")
    payload = {
        "from": f"Mutemo Desk <{from_addr}>",
        "to": [to],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    if ics_content:
        payload["attachments"] = [{
            "filename": "mutemo-events.ics",
            "content": base64.b64encode(ics_content.encode("utf-8")).decode("utf-8"),
        }]
    import httpx
    with httpx.Client(timeout=15) as http:
        resp = http.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Resend API error {resp.status_code}: {resp.text}")

async def send_reminder_email(recipient: str, events: list, test: bool = False) -> bool:
    """Send rich HTML daily calendar reminder via Resend with ICS attachment."""
    text_body, html_body = build_reminder_email_body(events)
    if test:
        text_body = "[TEST EMAIL]\n\n" + text_body
        html_body = '<p style="background:#fdf6e8;padding:8px;border-radius:4px;font-size:13px"><strong>This is a test email.</strong></p>' + html_body
    subject_prefix = "[TEST] " if test else ""
    if any(e.get("days_until") == 0 for e in events):
        subject = f"{subject_prefix}\u2696 Mutemo Desk \u2014 Court date TODAY + upcoming"
    elif events:
        subject = f"{subject_prefix}\u2696 Mutemo Desk \u2014 Daily reminder ({len(events)} upcoming)"
    else:
        subject = f"{subject_prefix}\u2696 Mutemo Desk \u2014 Daily reminder (nothing upcoming)"
    ics_content = build_ics(events) if events else None
    try:
        await asyncio.to_thread(_send_via_resend_sync, recipient, subject, html_body, text_body, ics_content)
        return True
    except Exception as e:
        print(f"[email] send failed: {e}")
        return False

@app.get("/api/reminders/settings")
async def get_reminder_settings(request: Request):
    user = await get_current_user(request)
    _check_permission(user, "admin:settings")
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM reminder_settings WHERE firm_id=$1", FIRM_ID)
    if not row:
        return {"enabled": False, "recipient_email": "", "send_hour_utc": 5}
    d = dict(row)
    d["firm_id"] = str(d["firm_id"])
    if d.get("last_run_date"):
        d["last_run_date"] = str(d["last_run_date"])
    return d

@app.post("/api/reminders/settings")
async def update_reminder_settings(settings: ReminderSettings, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "admin:settings")
    async with _db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO reminder_settings (firm_id, enabled, recipient_email, send_hour_utc)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (firm_id) DO UPDATE SET
                enabled=$2, recipient_email=$3, send_hour_utc=$4
        """, FIRM_ID, settings.enabled, settings.recipient_email, settings.send_hour_utc)
    return {"saved": True}

@app.post("/api/reminders/test")
async def test_reminder(request: Request):
    user = await get_current_user(request)
    _check_permission(user, "admin:settings")
    if not is_email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured on this server.")
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM reminder_settings WHERE firm_id=$1", FIRM_ID)
    if not row or not row["recipient_email"]:
        raise HTTPException(status_code=400, detail="No recipient email configured.")
    # Send a test with a dummy upcoming event so the HTML template renders
    today = datetime.utcnow().date()
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM calendar_events
            WHERE firm_id=$1 AND date >= $2
            ORDER BY date ASC, time ASC NULLS LAST
            LIMIT 10
        """, FIRM_ID, today)
    test_events = [_row_to_event(r) for r in rows]
    for e in test_events:
        try:
            event_date = datetime.strptime(e["date"], "%Y-%m-%d").date()
            e["days_until"] = (event_date - today).days
        except Exception:
            e["days_until"] = 99
    if not test_events:
        test_events = [{
            "id": "test", "title": "No events scheduled yet",
            "date": today.isoformat(), "time": None, "event_type": "other",
            "court": None, "matter_name": FIRM_NAME, "notes": None, "days_until": 0,
        }]
    sent = await send_reminder_email(row["recipient_email"], test_events, test=True)
    if not sent:
        raise HTTPException(status_code=500, detail="Failed to send test email.")
    return {"sent": True, "recipient": row["recipient_email"], "event_count": len(test_events)}

# ── Reminder Scheduler ────────────────────────────────────────────────────────

async def reminder_scheduler_loop():
    """Runs every hour. Sends daily digest of upcoming deadlines if enabled."""
    await asyncio.sleep(30)  # brief startup delay
    while True:
        try:
            await _maybe_send_reminder()
        except Exception as e:
            print(f"[reminder] scheduler error: {e}")
        await asyncio.sleep(3600)

async def _maybe_send_reminder():
    if not _db_pool:
        return
    async with _db_pool.acquire() as conn:
        settings = await conn.fetchrow("SELECT * FROM reminder_settings WHERE firm_id=$1", FIRM_ID)
    if not settings or not settings["enabled"] or not settings["recipient_email"]:
        return

    now_utc = datetime.utcnow()
    if now_utc.hour != settings["send_hour_utc"]:
        return
    today = now_utc.date()
    # Skip weekends — reminders only on Mon–Fri
    if today.weekday() >= 5:
        return
    if settings.get("last_run_date") == today:
        return

    # Collect upcoming events (next 30 days, including today)
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM calendar_events
            WHERE firm_id=$1 AND date >= $2 AND date <= $3
            ORDER BY date ASC, time ASC NULLS LAST
        """, FIRM_ID, today, today + timedelta(days=30))

    events = [_row_to_event(r) for r in rows]
    if not events:
        async with _db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE reminder_settings SET last_run_date=$1 WHERE firm_id=$2",
                today, FIRM_ID
            )
        return

    # Enrich events with days_until for the HTML email builder
    for e in events:
        try:
            event_date = datetime.strptime(str(e["date"])[:10], "%Y-%m-%d").date()
            e["days_until"] = (event_date - today).days
        except Exception:
            e["days_until"] = 0  # treat as today if date parse fails

    sent = await send_reminder_email(settings["recipient_email"], events)
    if sent:
        async with _db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE reminder_settings SET last_run_date=$1 WHERE firm_id=$2",
                today, FIRM_ID
            )
        print(f"[reminder] digest sent to {settings['recipient_email']}: {len(events)} events")

# ── Inactivity Alerts ─────────────────────────────────────────────────────────

@app.post("/api/reminders/inactivity-check")
async def inactivity_check(request: Request):
    user = await get_current_user(request)
    _check_permission(user, "admin:settings")
    async with _db_pool.acquire() as conn:
        settings = await conn.fetchrow("SELECT * FROM reminder_settings WHERE firm_id=$1", FIRM_ID)
    if not settings or not settings["recipient_email"]:
        raise HTTPException(status_code=400, detail="No recipient email configured.")

    threshold = datetime.utcnow() - timedelta(days=14)
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name, internal_ref, last_activity
            FROM matters
            WHERE firm_id=$1 AND status='Active'
              AND (last_activity IS NULL OR last_activity < $2)
            ORDER BY last_activity ASC NULLS FIRST
        """, FIRM_ID, threshold)

    inactive = [dict(r) for r in rows]
    for m in inactive:
        m["id"] = str(m["id"])
        if m.get("last_activity"):
            m["last_activity"] = m["last_activity"].isoformat()

    if not inactive:
        return {"inactive_count": 0, "message": "All active matters have recent activity."}

    lines = [f"Mutemo Desk — Inactivity Alert for {FIRM_NAME}", ""]
    lines.append(f"The following {len(inactive)} active matter(s) have had no activity in 14+ days:")
    lines.append("")
    for m in inactive:
        ref = m.get("internal_ref") or m.get("id", "")[:8]
        last = m.get("last_activity", "Never")[:10] if m.get("last_activity") else "Never"
        lines.append(f"  • [{ref}] {m['name']} — last activity: {last}")
    body = "\n".join(lines)
    sent = await send_reminder_email(
        settings["recipient_email"],
        f"Mutemo Desk — {len(inactive)} inactive matter(s)",
        body
    )
    return {"inactive_count": len(inactive), "matters": inactive, "email_sent": sent}

# ── Document status polling ───────────────────────────────────────────────────

@app.get("/api/documents/{doc_id}/status")
async def get_document_status(doc_id: str, request: Request):
    """Poll this endpoint after upload to check if background processing is complete."""
    user = await get_current_user(request)
    _check_permission(user, "matter:read")
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, filename, status, chunk_count, word_count, error_message FROM documents WHERE id=$1 AND firm_id=$2",
            _uuid_mod.UUID(doc_id), FIRM_ID
        )
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")
    d = dict(row)
    d["id"] = str(d["id"])
    return d

# ── Firm settings ─────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings(request: Request):
    user = await get_current_user(request)
    _check_permission(user, "admin:settings")
    async with _db_pool.acquire() as conn:
        firm = await conn.fetchrow("SELECT * FROM firms WHERE id=$1", FIRM_ID)
    if not firm:
        return {"firm_name": FIRM_NAME, "firm_city": FIRM_CITY}
    d = dict(firm)
    d["id"] = str(d["id"])
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    return d

@app.patch("/api/settings")
async def update_settings(body: dict, request: Request):
    user = await get_current_user(request)
    _check_permission(user, "admin:settings")
    allowed = {"name", "short_name", "city", "country"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    set_clauses = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates.keys()))
    values = list(updates.values())
    async with _db_pool.acquire() as conn:
        await conn.execute(
            f"UPDATE firms SET {set_clauses} WHERE id=$1",
            FIRM_ID, *values
        )
    return {"saved": True}

# ── Frontend catch-all ────────────────────────────────────────────────────────

@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    index = os.path.join(frontend_path, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return JSONResponse(status_code=404, content={"detail": "Frontend not found"})
