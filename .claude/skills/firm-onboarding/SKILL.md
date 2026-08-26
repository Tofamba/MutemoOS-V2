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
- The `firms` table row is seeded automatically by `run_migrations()`, from four env vars — this is the single source of truth `get_firm_identity()` reads from at runtime for every AI-generated document's system prompt. Set all four before the first deploy:
  - `MUTEMO_FIRM_NAME` — **required, no default.** As of the fix in commit `1b4046f` (2026-08-26), a missing `MUTEMO_FIRM_NAME` makes `run_migrations()` raise and refuse to start, rather than silently seeding a blank name. Before that fix, this field alone was correctly read from the env var — the bug below was in the other three columns.
  - `MUTEMO_FIRM_CITY` — optional, defaults to `"Harare, Zimbabwe"`.
  - `MUTEMO_FIRM_COUNTRY` — optional, defaults to `"Zimbabwe"` (a safe product-wide default given this app's entire domain, not a per-firm guess).
  - `MUTEMO_FIRM_SHORT_NAME` — optional; if unset, it's derived from `MUTEMO_FIRM_NAME`'s initials (e.g. "Sawyer & Mkushi" → "SM"), not hardcoded to any other firm's value.
  - **Fixed bug, worth knowing about**: before commit `1b4046f`, this seed unconditionally hardcoded `short_name="S&M"`, `city="Harare"`, `country="Zimbabwe"` — Sawyer & Mkushi's own values — regardless of any env var, and silently ignored `MUTEMO_FIRM_CITY` entirely. Following README's documented "deploy a second firm" steps literally, before this fix, would have seeded a new firm outside Harare with another firm's city/country/short_name. Confirmed and fixed via an actual walkthrough of provisioning a fresh second firm against a real throwaway Postgres instance, 2026-08-26 — see git history for the test. If you're working against a checkout older than this commit, apply the fix first.
  - `ON CONFLICT (id) DO NOTHING` means this seed only ever fires once per `firm_id`, on first migration run against an empty database — it will never silently overwrite a firm's data on a later redeploy. If a firm's seeded data needs correcting after the fact, use `PATCH /api/settings` (needs an authenticated admin — see the bootstrap step below for a zero-user firm).
- Seed the sentinel "General/Firm Precedent" matter for this firm (used by Rapid Precedent Capture's unmatched-matter path) — check `provision_case_binder`/sentinel-matter logic in main.py for the exact seed pattern used for the first firm.

### Creating the first admin — zero users, zero chicken-and-egg problem

The normal invite flow (`POST /api/admin/invite`) requires an existing admin to send invites — no use for a firm with zero users. This is a solved, already-built problem, not a manual-SQL workaround: `POST /api/admin/bootstrap` (main.py) exists specifically for this.

- Gated by `MUTEMO_ADMIN_TOKEN` (this firm's deploy-time secret) via the `X-Admin-Token` header — stricter than other admin-token checks in the codebase, which silently allow access if the token isn't configured; bootstrap refuses outright if it's missing.
- **Self-disabling**: refuses with a 403 the instant any active admin already exists for this `firm_id` — it's a one-time provisioning credential, not a standing backdoor. Safe to call again after the first admin exists; it'll just refuse.
- It doesn't create a `users` row directly — it creates an `invites` row (role `admin`) and reuses the exact same phone/OTP login path everyone else goes through. The first admin becomes a real, normal user on their first login, not a special-cased account.
- Call it once, with the firm's intended first admin's phone/email/display name, then have that person log in normally via the phone/OTP screen to complete setup.

## Step 3 — Legal corpus (existing backlog + going-forward feed)

Every new firm starts from the same shared ZLR/legislation corpus, then builds
its own firm-specific precedents on top via Rapid Precedent Capture and manual
uploads. Two genuinely separate pieces, confirmed via direct investigation of
`mutemo-legal-feed` (2026-08-26) — don't conflate them:

**Going forward (new content from the moment of onboarding onward): already solved, just needs config, not a build.**
`mutemo-legal-feed/pusher.py`'s `_load_firms()` genuinely supports up to 10
firms via `FIRM_1_*` through `FIRM_10_*` env vars, and `push_with_retry()`
pushes every scraped item to every registered firm independently, with its own
retry/failure logging per firm — this is real, working, generic code, not
hardcoded to Sawyer & Mkushi. (**README.md's claim that this is "still a
from-scratch task" is stale/wrong** — flag that if you see it repeated
elsewhere; it was true once, isn't anymore. Fix the doc if you're touching
that section anyway.) The live feed service just doesn't have a second firm
*configured* — as of 2026-08-26 its env vars only include `FIRM_1_*`. Add
`FIRM_2_BASE_URL`, `FIRM_2_SERVICE_TOKEN` (matching the new firm's own
`LEGAL_FEED_SERVICE_TOKEN`), `FIRM_2_ID`, and `FIRM_2_NAME` to
`mutemo-legal-feed`'s environment, and every future scrape starts flowing to
the new firm automatically. A few minutes of config, not a project.

**Historical backlog (everything scraped before the new firm existed): a real gap, now closed by `scripts/copy_shared_corpus_to_new_firm.py`.**
Fixing the feed config alone leaves a new firm's Search Vault empty on day one
— it only accumulates content going forward, at the scraper's daily cadence,
which reads as "broken" to a new paying customer even though nothing is
actually wrong. Run this script once per new firm, at onboarding time:

```
# Against the reference (source) firm's environment:
python3 scripts/copy_shared_corpus_to_new_firm.py export \
    --database-url <source DATABASE_URL> --chroma-path <source Chroma volume path> \
    --firm-id <source firm_id> --out corpus_export.json

# Against the new (target) firm's environment:
python3 scripts/copy_shared_corpus_to_new_firm.py import \
    --database-url <target DATABASE_URL> --chroma-path <target Chroma volume path> \
    --firm-id <new firm_id> --in corpus_export.json --apply   # omit --apply for a dry-run preview first
```

- Copies `legal_updates`/`zlr_entries`/`chunks` rows (`chunk_source IN
  ('legal','zlr')` only) from Postgres, plus matching vectors from Chroma's
  `legal_updates`/`zlr_index` collections — **never `firm_precedents`**, which
  is the source firm's own private client documents and must never cross into
  another firm's data. The script enforces this exclusion; don't build an
  alternative path that skips it.
- Row/vector ids are preserved exactly (not regenerated) — only `firm_id` is
  remapped. This matters: `chunks.id` doubles as the Chroma chunk id, and a
  chunk copied into Chroma but not resolvable against the target's own
  Postgres `chunks` table (or vice versa) is silently unfindable in a real
  search, not obviously broken. Tested end-to-end against two real throwaway
  Postgres+Chroma instances specifically to catch this — confirmed a real
  query on the target resolves to the correct judgment via a real Postgres
  lookup, not just that rows/vectors exist. See the script's own docstring
  and commit history for the full test writeup.
- Idempotent (`ON CONFLICT DO NOTHING` / `upsert`) — safe to re-run `import`
  against the same target without duplicating anything.
- The firm's own documents/precedents (Vault uploads, case-binder
  auto-provisioned docs, Rapid Precedent Capture) are written with that
  firm's `firm_id` in the chunk metadata from the moment of ingestion —
  already true architecturally after the ChromaDB isolation work, so no
  extra step is needed to keep firm-specific content separate from the
  shared base going forward.
- This replaces the earlier-considered R2-snapshot-pipeline design for this
  step. That's still worth building eventually if onboarding becomes frequent
  enough to want a repeatable, always-fresh snapshot rather than a one-time
  export/import — it isn't the minimum bar for getting one new firm launched
  non-empty, which is what this script is for.

## Step 4 — Domain and Cloudflare Access

- Add a DNS CNAME for `<firmname>.tofamba.com` pointing at this Railway service's domain.
- Configure Cloudflare Access for the new subdomain, mirroring the existing production Access policy (same auth method — email OTP/SSO as appropriate) — do not skip this even for a demo/trial deployment; an unprotected subdomain with real client data is not acceptable.
- Confirm HTTPS is live on the new subdomain (required for the PWA manifest, camera capture, and service worker to function).

## Step 5 — Verification (do not skip)

Run the same real-data-first discipline used for every feature shipped this session — trust actual behavior, not just "the deploy succeeded":

- [ ] `/health/ready` and `/health/alerts` both return healthy on the new deployment.
- [ ] Query the `firms` table row directly (or `GET /api/settings` as an authenticated admin) and confirm `name`/`short_name`/`city`/`country` are all this firm's own real values — not `"S&M"`/`"Harare"`/`"Zimbabwe"`. Cheap to check, and was silently wrong for every second-firm deploy before commit `1b4046f`.
- [ ] Log in as the firm's first real user; confirm `get_firm_identity()` resolves correctly — generate a test AI document (affidavit/contract review) and confirm the firm's actual name appears in the output, not a placeholder or another firm's name.
- [ ] Create a test client/matter through the intake flow; confirm the case-binder auto-provisioning and sentinel-matter behavior work correctly on this fresh instance.
- [ ] Run a real search; confirm firm-document search returns results scoped to this firm only (this is the step that broke on the very first firm's Part 3 rollout because a backfill script wasn't run — a fresh firm shouldn't need that backfill, since chunks are written with `firm_id` from day one, but confirm this explicitly rather than assuming the code path is exercised correctly).
- [ ] Run a real ZLR/legislation search for a query you know the shared corpus can answer, and confirm it returns real results — not "no relevant documents found." Confirms `copy_shared_corpus_to_new_firm.py`'s import actually landed correctly, not just that the script exited without an error (the exact Postgres/Chroma consistency risk it was built and tested to guard against).
- [ ] Confirm the AML compliance module (Beneficial Ownership, PEP, conflict check) renders and functions in the UI — not just via API, given this exact gap was found and fixed on the first firm.
- [ ] Confirm the PWA installs correctly on a real device for this new subdomain (manifest/service-worker scoped correctly per-origin — this should work automatically since each subdomain is its own origin, but verify once).

## Step 6 — Handoff

- Document the new firm's deployment details (Railway service name, subdomain, `firm_id`) somewhere durable — a firms registry, even a simple markdown file in the repo, so future onboarding or debugging doesn't require hunting through Railway's dashboard to remember what exists.
- Do NOT reuse or share the `MUTEMO_ADMIN_TOKEN` across firms — treat each as a fully separate secret.

## Known limitations to flag to the user, not silently work around

- This process is currently manual/semi-scripted, not self-service. If firm count grows enough that this becomes the bottleneck (see README.md's Option A trigger condition), that's a signal to build real self-service provisioning — not to start improvising shortcuts in this checklist.
- The legal corpus duplication-per-firm (Step 3's `copy_shared_corpus_to_new_firm.py`) has a real, growing storage cost as firm count increases — full Postgres rows + Chroma vectors, copied whole, per firm. Worth monitoring, not urgent yet. If it becomes worth avoiding the duplication itself (not just making it repeatable), that's a bigger architectural change, not a tweak to this script.
- Don't forget the `mutemo-legal-feed` `FIRM_{n}_*` config step (Step 3) alongside the corpus copy — they're separate actions covering separate content (historical backlog vs. going forward), and doing only one leaves the other silently missing.
- **Bulk staff import doesn't exist.** Confirmed via direct check of both the API and the UI, 2026-08-25/26: `POST /api/admin/invite` takes one invite per call — no batch/array parameter — and the frontend's "Invite New User" panel is a single-entry form (name/email/phone/role, one submit button), no CSV upload or multi-row entry. For a firm with 5-10 lawyers, that's 5-10 individual invite submissions, each triggering its own Cloudflare Access rule addition and invite email. There is no faster path today — don't imply one to the client, and don't try to script around `POST /api/admin/invite` in a loop as an unannounced workaround without flagging that this is filling a real product gap, not using a supported bulk feature.
