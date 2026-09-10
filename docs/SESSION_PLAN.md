# OpenFotos implementation sessions

Use nine focused Codex sessions for the pilot. This is a better boundary than treating each
calendar day as a session: several features span the desktop, API, database, and object store and
should be completed together. Start every new session by reading the product plan, this file, and
the latest git status. End it with the listed completion checks, updated documentation, and a
verified commit.

## Session 1: Repository foundation

**Goal:** Make architectural boundaries visible and give later sessions one repeatable toolchain.

**Work:** Create the monorepo layout, Python project metadata, Django and PySide6 entry points,
shared contract/storage packages, local infrastructure definitions, CI, and baseline checks.

**Done when:** The unit suite and linter pass, Django's system check passes with server
dependencies installed, the container configuration parses, and no secret or generated-data path
is tracked.

## Session 2: Accounts, tenancy, and event lifecycle

**Goal:** Establish the authorization boundary before accepting media.

**Work:** Implement photographer membership, events, state transitions, admin provisioning,
host/subdomain resolution, photographer login, visitor PIN sessions, Argon2, and initial audit
events. Add migrations and cross-tenant denial tests.

**Done when:** An administrator can create the pilot photographer and event; the photographer sees
only assigned events; a visitor can unlock only a matching published sample event; altered tenant,
event, and host identifiers are denied.

## Session 3: Desktop discovery and durable local state

**Goal:** Reliably inventory a client folder before any network transfer.

**Work:** Build login/event-selection scaffolding, recursive JPEG discovery, extension/MIME/decode
validation, size limits, checksums, the per-event SQLite checkpoint, and folder/validation/progress
screens. Keep processing and upload services behind interfaces.

**Done when:** The app reports exact accepted/rejected counts and bytes, survives a forced exit,
detects changed files, and resumes without rescanning completed unchanged assets.

## Session 4: Direct upload and manifest reconciliation

**Goal:** Move originals and metadata safely from desktop to private R2.

**Work:** Implement API tokens, event-scoped upload sessions, server-owned object keys, asset
reservation, four-way resumable transfers, credential refresh, retries, idempotency records,
per-variant completion, and final manifest validation. Use an S3-compatible local test service or
fakes before real R2.

**Done when:** An interrupted upload resumes without duplicate assets or transfers, invalid object
keys are rejected, byte/checksum limits are enforced, and the server reconciles a complete test
manifest.

## Session 5: Image derivatives and private gallery

**Goal:** Publish fast, authorized browsing assets while preserving uploaded originals.

**Work:** Apply EXIF orientation, generate thumbnails and watermarked previews, remove derivative
metadata, add gallery pagination/lightbox pages, authorize short-lived object URLs, and implement
download policy plumbing.

**Done when:** Authorized visitors can browse private derivatives, unauthorized and expired
sessions cannot obtain URLs, previews contain the watermark, derivatives contain no GPS data, and
original checksums remain unchanged.

## Session 6: Face-engine benchmark and embedding contract

**Goal:** Prove CPU throughput and matching quality before the face workflow depends on it.

**Work:** Build the replaceable InsightFace adapter, verified model download/checksum mechanism,
normal and group-photo detection modes, quality filters, normalization, model/version contract,
and benchmark report. Use only synthetic or consented images; do not commit weights or embeddings.

**Done when:** A 500-to-1,000-image representative benchmark records hardware, timings, face yield,
memory, false merges/misses, thresholds, model hashes, and projected full-event duration. The go/no
go decision for `buffalo_m` is documented.

## Session 7: Face ingestion, clustering, and photographer review

**Goal:** Turn compatible embeddings into conservative, correctable anonymous collections.

**Work:** Add pgvector migrations, face and cluster ingestion, event-scoped neighbor queries,
mutual-neighbor clustering, minimum-photo rules, ranking, and merge/hide/feature/rebuild controls.

**Done when:** Batch retries create no duplicate faces, all vector queries are event-scoped, the
highest-ranked collections can be reviewed and corrected, and a false-merge security test passes.

## Session 8: Shares, selfie search, and original downloads

**Goal:** Complete the customer discovery and delivery flow.

**Work:** Add full-event and collection share links, expiry and revocation, consent recording,
strict selfie validation, in-memory inference, event-filtered vector search, result deduplication,
rate limits, and authorized signed original downloads.

**Done when:** Selfies are absent from storage, database fields, logs, and error paths; model
mismatches fail closed; restricted shares expose only their collection; expired links and URLs are
denied; downloaded originals match uploaded checksums.

## Session 9: Production hardening and launch rehearsal

**Goal:** Turn the feature-complete pilot into an operable release.

**Work:** Finish Railway/R2/Supabase configuration, security headers and cookie policy, wildcard
domain checks, secret scanning, monitoring, database backup/restore, Windows packaging, failure
runbooks, performance fixes, and the end-to-end rehearsal. Resolve the application licence before
making the repository public.

**Done when:** The complete acceptance checklist passes from an empty database and bucket prefix;
one backup restores into a separate database; upload interruption and rollback are rehearsed; the
photographer walkthrough is complete; the launch commit is tagged.

## Session handoff template

At the end of each session, record:

1. The user-visible behavior now working.
2. Files, migrations, environment variables, and external resources added or changed.
3. Commands run and their results.
4. Known limitations and any decisions that the next session must preserve.
5. The next session's first failing acceptance test.

Do not begin a later session while an earlier session's authorization or data-isolation checks are
failing. Session 6 may run earlier on separate hardware if representative client images become
available; its confirmed model contract must land before Sessions 7 and 8.
