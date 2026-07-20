# MutemoOS-V2

Legal practice management and AI-assisted research/drafting platform for Zimbabwean law firms — FastAPI backend, PostgreSQL, ChromaDB (semantic search), Cloudflare R2 (document storage), and Anthropic Claude (research synthesis, drafting, contract review).

Each deployment serves **one firm**, identified by a fixed `FIRM_ID` in that instance's environment — this is a single-tenant-per-deployment model, not a shared multi-tenant database. See [Multi-Firm / Sub-Organisation Model](#multi-firm--sub-organisation-model) below for how firms with an internal panel-lawyer structure (e.g. Legal Corner) are handled within a single instance.

## Companion Repository

**[`mutemo-legal-feed`](../mutemo-legal-feed)** is a separate service that scrapes ZimLII, Veritas, the Laws.Africa KB API, and legal news sources, then pushes new legislation, judgments, and news into one or more MutemoOS-V2 instances via the admin API (`/api/legal-updates/upload`, `/api/zlr/upload`). It supports pushing to multiple firm instances from a single deployment via `FIRM_1_*` through `FIRM_10_*` environment variables — see that repo's own README for scraper-specific detail (including the Laws.Africa KB API integration in `scrapers/lawsafrica.py`).

## Document Storage (Cloudflare R2)

Original uploaded files (PDFs, Word documents) are stored in Cloudflare R2 — an S3-compatible object store — separately from the extracted text, which is chunked and indexed in PostgreSQL/ChromaDB for search. R2 holds the file for viewing/downloading the original; it is not used for search.

- Objects are keyed as `{firm_id}/{matter_id}/{doc_id}/{filename}`
- Viewing a document generates a short-lived (1 hour) presigned URL on demand — the app never proxies file bytes directly
- If R2 isn't configured, uploads still work (text extraction/search is unaffected), but the "view original file" action will not be available for documents uploaded while it was disabled
- Required environment variables: `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` (defaults to `mutemoos-documents` if unset)

## Multi-Firm / Sub-Organisation Model

Each MutemoOS-V2 instance serves one firm (`FIRM_ID`), but a firm can have an internal structure beyond simple staff roles. This matters specifically for firms like **Legal Corner**, which has a panel of affiliated lawyers alongside firm operations staff.

- Standard per-user roles (`admin`, `partner`, `associate`, `secretary`) live on the `users` table and control what a user can do in the system
- A separate `organisation_roles` table layers an additional, optional role — `ops_manager` or `panel_lawyer` — on top of a user's standard role, for firms that need this distinction
- This is set at invite time (`organisation_role` field on the invite) and does not require a separate firm/tenant — it's a within-firm distinction, not a multi-tenant one

## Authentication

Login is OTP-based (no passwords), gated by invitation:

- Account creation requires a matching, not-yet-accepted invite for the exact phone number being verified — an unrecognised number cannot self-provision an account, regardless of what any outer access layer (e.g. Cloudflare Access) allows through
- OTP delivery prefers WhatsApp (Meta Business Cloud API, `WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID`) if configured, and falls back to email via Resend otherwise — no code change is needed to switch once WhatsApp credentials are added, it happens automatically
- If neither channel is configured, authentication is disabled and the app falls back to a synthetic development user — **this is a dev/demo fallback only** and should never be relied on in a deployment handling real client data

## Known Architecture Debt

`main.py` is a single large file (several thousand lines) covering routing, business logic, and data access together. Splitting this into `/routers`, `/services`, `/models` has been discussed and **deliberately deferred**, pending a planned PostgreSQL schema migration to a proper workspace model — the split is intended to happen alongside that migration rather than twice. This is a known, accepted tradeoff, not an oversight.

## Environment Variables

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `ANTHROPIC_API_KEY` | Claude API access (research, drafting, contract review) |
| `CHROMA_DATA_DIR` | Persistent path for the ChromaDB vector index — **must point at a mounted volume**, or the index is silently lost on every redeploy |
| `MUTEMO_FIRM_ID`, `MUTEMO_FIRM_NAME`, `MUTEMO_FIRM_CITY` | Identifies which firm this instance serves |
| `MUTEMO_ADMIN_TOKEN` | Admin API token, used by `mutemo-legal-feed` to push content |
| `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | Cloudflare R2 document storage (see above) |
| `RESEND_API_KEY`, `RESEND_FROM` | Email delivery — calendar invites, reminders, daily digest, OTP fallback |
| `SMTP_HOST` | Alternative email delivery path, if not using Resend |
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_OTP_TEMPLATE_NAME`, `WHATSAPP_OTP_TEMPLATE_LANG` | WhatsApp OTP delivery via Meta Business Cloud API |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCESS_APP_ID` | Syncing invited users to Cloudflare Access automatically |

## Core Features

- **Search Vault** — semantic search across firm documents, indexed legislation/case law, and ZLR judgments, with an honest grounding indicator (genuinely retrieved sources vs. general knowledge) and optional document attachment for a grounded opinion on a specific file
- **Contract Review** — structured findings (missing clauses, risky terms, compliance concerns) with every quoted finding independently verified against the actual document text before being shown
- **Draft Document** — 20+ document types (litigation, conveyancing, commercial agreements, correspondence) with Zimbabwe-specific structural guidance, plus smart precedent matching against the firm's own past documents
- **Matter management** — conflict-of-interest checking (fuzzy name matching against existing matters, including closed ones), matter-level deadline tracking surfaced on the matter list and in the daily reminder email, progress notes, document uploads with OCR and confidence flagging
- **Calendar** — attendee invites with proper Accept/Decline via ICS, reschedule/cancel notifications, daily reminder email
- **Daily digest** — summary of new legislation, judgments, and news added to the vault each day, fed by `mutemo-legal-feed`
