---
name: firm-onboarding
description: Provision a new MutemoOS deployment for a new law firm client. Use this whenever the user says "onboard a new firm", "set up MutemoOS for [firm name]", "new firm deployment", or asks to add a firm under the tofamba.com domain. MutemoOS runs one Railway deployment per firm (Option B multi-tenancy, documented in README.md) — this skill captures the full, repeatable checklist so nothing gets missed between firms. Always confirm each step's result before moving to the next; this touches real infrastructure and a new firm's real data.
---

# Firm Onboarding

Provisions a complete, isolated MutemoOS deployment for a new law firm. Follow this in order — each step depends on the one before it, and skipping the verification checks is how firm #2 quietly inherits firm #1's problems.

## Before starting

Confirm with the user:
1. The firm's legal name and city (goes into `firms` table — this is what the AI system prompts and generated documents will reflect, via `get_firm_identity()`).
2. The desired subdomain, e.g. `firmname.tofamba.com`.
3. Whether this is a real paying client or a demo/trial deployment (affects whether to seed real vs. placeholder data).

## Step 1 — Provision the Railway service

- Create a new Railway service for this firm, cloning the same deploy config (buildpack/Dockerfile, env structure) as the existing production service — do not reinvent it per firm.
- Provision this firm's own Postgres instance and its own Chroma volume. These must be genuinely separate from every other firm's — this is the whole point of Option B. Do not share a database or volume across firms under any circumstance.
- Set environment variables for this deployment, including `DATABASE_URL`, `MUTEMO_ADMIN_TOKEN` (generate a fresh one per firm, never reuse), and any API keys (Anthropic, R2/storage, Twilio/SMS) needed.

## Step 2 — Run migrations and seed firm identity

- Run the full migration suite against the new, empty Postgres instance (`run_migrations()` or equivalent) — this creates every table from scratch, including the multi-tenancy-hardened schema (firm_id columns, sentinel matter, compliance tables, etc.) already in main.
- Seed the `firms` table with this firm's name and city — this is the single source of truth `get_firm_identity()` reads from. Do NOT rely on the old `FIRM_NAME`/`FIRM_CITY` env-var pattern; that's deprecated in favor of the DB lookup.
- Seed the sentinel "General/Firm Precedent" matter for this firm (used by Rapid Precedent Capture's unmatched-matter path) — check `provision_case_binder`/sentinel-matter logic in main.py for the exact seed pattern used for the first firm.

## Step 3 — Legal corpus (shared base via R2 snapshot)

Decision (confirmed): every new firm starts from the same shared ZLR/legislation
corpus, then builds its own firm-specific precedents on top via Rapid Precedent
Capture and manual uploads. This is NOT a live shared corpus service. Instead:

- The corpus snapshot lives in the existing Cloudflare R2 bucket (same
  infrastructure already used for the document Vault) at a dedicated path,
  e.g. `corpus-snapshots/latest/` — an export of the ZLR/legislation Chroma
  collection including embeddings and metadata, not just raw text, so it
  restores directly without needing to re-embed on the new firm's instance.
- Whenever the scraper/ingestion pipeline successfully adds new statutes or
  case law (on the reference deployment), export the updated corpus
  collection and push it to that R2 path, replacing or versioning the
  previous snapshot.
- For a new firm's onboarding, download the snapshot from R2 and restore it
  directly into the firm's own Chroma instance — no live scraping against
  the new deployment, no cross-firm database access. This should be fast
  and require no new external network access beyond what Claude Code
  already has to R2.
- The firm's own documents/precedents (Vault uploads, case-binder
  auto-provisioned docs, Rapid Precedent Capture) are written with that
  firm's `firm_id` in the chunk metadata from the moment of ingestion —
  already true architecturally after this session's ChromaDB isolation
  work, so no extra step is needed to keep firm-specific content separate
  from the shared base.
- If the export/snapshot script doesn't exist yet, that's a prerequisite
  to build (a short, one-time script — export collection, push to R2)
  before the next firm's onboarding. Flag it rather than falling back to
  a live re-scrape per firm as a permanent pattern.
- Verify the `firm_id` metadata field is present on every embedded chunk from the start (this was a backfill fix on the first firm — for a new firm, it should be correct by default since the code now writes it at ingestion time; confirm this rather than assuming).

## Step 4 — Domain and Cloudflare Access

- Add a DNS CNAME for `<firmname>.tofamba.com` pointing at this Railway service's domain.
- Configure Cloudflare Access for the new subdomain, mirroring the existing production Access policy (same auth method — email OTP/SSO as appropriate) — do not skip this even for a demo/trial deployment; an unprotected subdomain with real client data is not acceptable.
- Confirm HTTPS is live on the new subdomain (required for the PWA manifest, camera capture, and service worker to function).

## Step 5 — Verification (do not skip)

Run the same real-data-first discipline used for every feature shipped this session — trust actual behavior, not just "the deploy succeeded":

- [ ] `/health/ready` and `/health/alerts` both return healthy on the new deployment.
- [ ] Log in as the firm's first real user; confirm `get_firm_identity()` resolves correctly — generate a test AI document (affidavit/contract review) and confirm the firm's actual name appears in the output, not a placeholder or another firm's name.
- [ ] Create a test client/matter through the intake flow; confirm the case-binder auto-provisioning and sentinel-matter behavior work correctly on this fresh instance.
- [ ] Run a real search; confirm firm-document search returns results scoped to this firm only (this is the step that broke on the very first firm's Part 3 rollout because a backfill script wasn't run — a fresh firm shouldn't need that backfill, since chunks are written with `firm_id` from day one, but confirm this explicitly rather than assuming the code path is exercised correctly).
- [ ] Confirm the AML compliance module (Beneficial Ownership, PEP, conflict check) renders and functions in the UI — not just via API, given this exact gap was found and fixed on the first firm.
- [ ] Confirm the PWA installs correctly on a real device for this new subdomain (manifest/service-worker scoped correctly per-origin — this should work automatically since each subdomain is its own origin, but verify once).

## Step 6 — Handoff

- Document the new firm's deployment details (Railway service name, subdomain, `firm_id`) somewhere durable — a firms registry, even a simple markdown file in the repo, so future onboarding or debugging doesn't require hunting through Railway's dashboard to remember what exists.
- Do NOT reuse or share the `MUTEMO_ADMIN_TOKEN` across firms — treat each as a fully separate secret.

## Known limitations to flag to the user, not silently work around

- This process is currently manual/semi-scripted, not self-service. If firm count grows enough that this becomes the bottleneck (see README.md's Option A trigger condition), that's a signal to build real self-service provisioning — not to start improvising shortcuts in this checklist.
- The legal corpus duplication-per-firm (Step 3) has a real, growing storage cost as firm count increases. Worth monitoring, not urgent yet.
