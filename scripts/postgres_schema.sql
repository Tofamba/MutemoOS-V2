-- ============================================================
-- MutemoOS PostgreSQL Schema v2.0 — Production
-- Includes: firm_id multi-tenancy, RBAC, full data model
-- Deploy: psql $DATABASE_URL -f postgres_schema.sql
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Firms ────────────────────────────────────────────────────
CREATE TABLE firms (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    short_name  TEXT,
    city        TEXT DEFAULT 'Harare',
    country     TEXT DEFAULT 'Zimbabwe',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Users ────────────────────────────────────────────────────
-- Roles: partner | associate | secretary | admin
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    phone           TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('partner','associate','secretary','admin')),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (firm_id, phone)
);

-- ── Sessions ─────────────────────────────────────────────────
CREATE TABLE sessions (
    token       TEXT PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);

-- ── OTP Store ────────────────────────────────────────────────
CREATE TABLE otp_store (
    phone       TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    attempts    INT NOT NULL DEFAULT 0,
    expires_at  TIMESTAMPTZ NOT NULL
);

-- ── Matters ──────────────────────────────────────────────────
CREATE TABLE matters (
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
CREATE INDEX idx_matters_firm ON matters(firm_id);
CREATE INDEX idx_matters_status ON matters(firm_id, status);

-- ── Progress Notes ───────────────────────────────────────────
CREATE TABLE progress_notes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    matter_id   UUID NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    firm_id     UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    author      TEXT NOT NULL DEFAULT 'Unknown',
    user_id     UUID REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_notes_matter ON progress_notes(matter_id);

-- ── Documents ────────────────────────────────────────────────
CREATE TABLE documents (
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
    status          TEXT DEFAULT 'complete',
    error_message   TEXT,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    uploaded_by     UUID REFERENCES users(id)
);
CREATE INDEX idx_documents_matter ON documents(matter_id);
CREATE INDEX idx_documents_firm ON documents(firm_id);

-- ── Legal Updates ────────────────────────────────────────────
CREATE TABLE legal_updates (
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
    status          TEXT DEFAULT 'complete',
    ocr_used        BOOLEAN DEFAULT FALSE,
    error_message   TEXT,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_legal_updates_firm ON legal_updates(firm_id);

-- ── ZLR Index ────────────────────────────────────────────────
CREATE TABLE zlr_entries (
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
CREATE INDEX idx_zlr_firm ON zlr_entries(firm_id);
CREATE INDEX idx_zlr_category ON zlr_entries(firm_id, taxonomy_category);

-- ── Calendar Events ──────────────────────────────────────────
CREATE TABLE calendar_events (
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
CREATE INDEX idx_calendar_firm ON calendar_events(firm_id);
CREATE INDEX idx_calendar_date ON calendar_events(firm_id, date);

-- ── Reminder Settings ────────────────────────────────────────
CREATE TABLE reminder_settings (
    firm_id             UUID PRIMARY KEY REFERENCES firms(id) ON DELETE CASCADE,
    enabled             BOOLEAN NOT NULL DEFAULT FALSE,
    recipient_email     TEXT,
    send_hour_utc       INT NOT NULL DEFAULT 5,
    last_run_date       DATE
);

-- ── Chunks (keyword fallback metadata) ───────────────────────
-- Vector embeddings remain in ChromaDB. This table stores
-- the text and metadata for keyword fallback search and
-- for rebuilding ChromaDB after a redeploy.
CREATE TABLE chunks (
    id              TEXT PRIMARY KEY,
    firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL,
    matter_id       TEXT,
    chunk_source    TEXT NOT NULL CHECK (chunk_source IN ('firm','legal','zlr')),
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
CREATE INDEX idx_chunks_firm ON chunks(firm_id);
CREATE INDEX idx_chunks_document ON chunks(document_id);
CREATE INDEX idx_chunks_source ON chunks(firm_id, chunk_source);

-- ── Seed: Sawyer & Mkushi (Nyari's firm) ─────────────────────
INSERT INTO firms (id, name, short_name, city, country)
VALUES (
    'a1b2c3d4-0000-0000-0000-000000000001',
    'Sawyer & Mkushi Legal Practitioners',
    'S&M',
    'Harare',
    'Zimbabwe'
) ON CONFLICT (id) DO NOTHING;

INSERT INTO reminder_settings (firm_id) VALUES
    ('a1b2c3d4-0000-0000-0000-000000000001')
ON CONFLICT (firm_id) DO NOTHING;
