# OpenFotos implementation sessions

This is the execution plan for the supervised pilot. Read the product plan, this file, the
applicable ADRs, nearby tests, and `git status` before each session. A session finishes only after
its observable slice, failure behavior, migrations, documentation, and repository checks pass.

## Post-Session-5 product revision

**Status (2026-09-13): complete in the current worktree.**

The client replaced three earlier assumptions:

- a main wedding now contains mandatory one-level sub-events;
- customer-visible collections and all pre-clustering are removed;
- access PINs are exactly four auto-generated ASCII digits.

The retrofit applies only to work implemented in Sessions 1–5. It does not implement future face
indexing, owner/guest sharing, selfie search, or original-download endpoints.

### Retrofit work

- Keep `Event` as the tenant/lifecycle/quota/manifest/security boundary.
- Add photographer-managed `SubEvent` records with create, rename, order, archive, restore, main
  gallery aggregation, and strict filtered galleries.
- Require every local/server contribution batch to reference exactly one active sub-event.
- Permit whole-batch reassignment before publication and audit both child identifiers.
- Replace lead/uploader invitation roles with one photographer account and up to ten tracked,
  revocable event installations.
- Remove the original-download policy setting; future downloads derive from visibility.
- Generate four-digit PINs, hash PIN+dedicated pepper with Argon2, and retain throttling/audit.
- Revise shared contracts, SQLite/Django migrations, desktop/UI/API behavior, tests, README, plan,
  and ADRs.

### Retrofit completion checks

- Wrong-tenant, wrong-event, archived, or missing sub-events fail closed.
- The desktop selects a child before creating a batch and sends the child in the strict manifest.
- Main/filtered gallery routes cannot widen their asset set; archived child media disappears.
- The old invitation/uploader/download-policy surfaces are absent.
- Fresh migrations, the deterministic suite, lint/type checks, and Django checks pass.
- No active plan/session describes clustering or collections.

## Session 6 product clarification

**Status (2026-09-16): accepted and reflected in the completed Session 6 contract.**

Photographer interviews removed the outsider/coworker sharing journey. Future guest capabilities
grant full gallery access within an event or sub-event scope. Face search is a best-effort,
recall-first filter over that already-authorized set, never a source of photo authorization.
ADR 0009 records the decision and supersedes the `selfie_only` parts of ADR 0008.

## Session 1: repository foundation

**Status:** complete.

**Goal:** Make system boundaries and one repeatable toolchain visible.

**Delivered:** Monorepo packages, Python/Django/PySide entry points, shared contracts and storage
boundaries, local infrastructure, CI, baseline checks, and secret/generated-data exclusions.

**Invariant:** Future work stays inside the documented desktop, server, storage, contract, and
vision boundaries and does not commit private media or credentials.

## Session 2: accounts, tenancy, event lifecycle, and private access

**Status:** complete; revised by ADR 0008.

**Goal:** Establish authorization before accepting media.

**Delivered:** Photographer tenants and memberships, host scoping, event state transitions, admin
provisioning, private visitor access, Argon2 PIN checks, event cookies, database throttling, audit
events, and cross-tenant denial tests.

**Revision:** PINs are now system-generated four-digit ASCII values with `EVENT_PIN_PEPPER`.
Publication also requires at least one active sub-event. The current event token/PIN flow is an
interim private gallery access path until Session 8 introduces owner/guest capabilities.

**Done when:** Matching tenant+event+host access succeeds; all identifier substitution, expiry,
revocation, invalid PIN, and throttling cases fail without leaking event/PIN details.

## Session 3: desktop discovery and durable local state

**Status:** complete; revised by ADR 0008.

**Goal:** Reliably inventory a customer folder before network transfer.

**Delivered:** Recursive and individual file selection, JPEG validation, limits, hashes, durable WAL
SQLite checkpoints, changed-file detection, pause/restart, redacted diagnostics, synthetic demo, and
background Qt flows.

**Revision:** The login scaffold has one photographer path. Event snapshots cache ordered active
sub-events. A user selects an event then a sub-event; every local batch stores `sub_event_id` and
`installation_id` before source selection. A checkpoint upgraded from the older role schema drops
incompatible unsubmitted batches because no production checkpoint data exists.

**Done when:** Exact counts/bytes and rejection reasons survive restart; changed media stops safely;
ten independent installation stores create distinct batch/asset identities; a batch cannot be
created for a missing or inactive cached child.

## Session 4: direct upload and manifest reconciliation

**Status:** complete; revised by ADR 0008.

**Goal:** Move originals safely from photographer desktops to private object storage.

**Delivered:** Photographer desktop sessions, rotating refresh tokens, server-owned keys, strict
immutable reservations, atomic quota, exact PUT leases, one-to-four-way resumable transfer,
verification, idempotency, exclusions/cancellation, intake generations, and aggregate manifests.

**Revision:** Upload invitations and upload-only sessions are removed. One authenticated
photographer account may register up to ten event installations. Reservations require an active
sub-event in the same event; aggregate manifests record its ID/name. Revoking an installation
blocks new lease work while photographer management can reconcile already uploaded bytes.

**Done when:** Interruptions resume at verified objects; clients cannot choose keys; cross-tenant,
cross-installation transfer, wrong-child, quota, checksum, and lifecycle cases fail intentionally;
concurrent reservations cannot exceed event quota.

## Session 5: derivatives, sub-events, and private gallery

**Status:** complete; revised by ADR 0008.

**Goal:** Publish fast authorized gallery media while preserving exact originals and event section
boundaries.

**Delivered:** Immutable optional preview policy, clean thumbnails, optionally watermarked previews,
orientation/color/metadata handling, derivative upload/verification/readiness, audited exclusions,
private signed gallery media, 48-item pagination, photo navigation, and publish/unpublish.

**Revision:** The photographer dashboard manages sub-events and whole-batch reassignment. `All
Photos` aggregates active children; filtered list/photo navigation stays child-scoped. Archiving a
child hides its assets and blocks processing/new uploads until restoration. The download-policy UI,
model, service, and tests are removed; actual original downloads remain Session 8.

**Done when:** Unauthorized, expired, cross-tenant, cross-child, and archived-child requests cannot
obtain media; derivatives contain no source metadata; original hashes do not change; publication
requires current manifest/policy/derivatives and an active child.

## Session 6: face-engine benchmark and model contract

**Status:** complete; accepted on 2026-09-16 in ADR 0010.

**Goal:** Prove CPU throughput and matching quality and freeze a compatible contract.

**Delivered:** A deterministic redacted harness, strict model/result contract, candidate comparison,
LFW calibration/holdout reports, and complete consented wedding/dense-group measurements on the
i5-6200U/8 GB performance floor. Reports cover throughput, memory, detection/usable-face yield,
false matches/misses, model rights and hashes, preprocessing, vector dimension/normalization,
quality rules, metric, threshold, and the 10,000-photo/100,000-usable-face projection. ADR 0010
accepts the passing YuNet/SFace contract.

**Not in scope:** Database face rows, production upload endpoints, clustering, or collections.

**Done when:** A reproducible redacted report and explicit go/no-go decision are reviewed; the model
ID/hash/dimension/normalization/quality/threshold contract is accepted; model weights and test faces
are not tracked.

**LFW evidence (2026-09-14):** The frozen benchmark and redacted reports are in
`docs/session6/`. YuNet + SFace passes the LFW recall/FAR, preliminary one-face timing, and memory
gates and is the current lead. Buffalo-M fails end-to-end recall under the same quality rules despite
strong conditional matching. This established the candidate before representative-data acceptance.

**Representative evidence (2026-09-16):** All 361 consented full-resolution photos completed with
zero processing failures, 2,343 usable face instances, and 74 photos containing at least ten usable
faces. The 10,000-photo/100,000-usable-face projection is 7.183 hours with 781.4 MiB peak RSS. Every
other technical gates pass, but the report remains a no-go because it does not meet the agreed
minimum of 500 representative photos. Face-instance count is not treated as identity-labelled
uniqueness.

**Final acceptance evidence (2026-09-16):** A separate complete run processed 1,782 consented
full-resolution photos (31.51 GB) with zero failures, 6,604 usable face instances, and 119 dense
photos containing at least ten usable faces. It projected 6.022 hours for 10,000 photos/100,000
usable faces at 797.8 MiB peak RSS. Combined with 98.204712% LFW holdout recall and 0.00318334%
false accepts, every gate passes. ADR 0010 freezes YuNet/SFace, the ordered hashes, preprocessing,
128-dimensional L2 vectors, quality rules, cosine metric, and maximum distance `0.55514365`.

**First failing acceptance test:** Reject a result document whose model hash, dimension, finite
values, or normalization differs from the accepted contract before any vector is persisted.

## Session 7: direct per-photo face indexing

**Status:** complete; accepted on 2026-09-17 in ADR 0011.

**Goal:** Create a conservative event-scoped face search index without grouping people.

**Work:** Add pgvector migration, `FaceAnalysis` terminal state, and per-face embeddings attached to
assets. Generate embeddings on the photographer desktop and upload strict idempotent documents to
Django. Validate tenant/event/asset/model/content/count/vector invariants. Add exact event and
sub-event vector-query boundaries, retries/rebuilds, and a publication gate requiring every visible
asset to be `indexed` or `no_usable_face`.

**Not in scope:** Clusters, collections, person labels, customer identity profiles, or selfie UI.

**Done when:** Retry creates no duplicates; model mismatch and malformed vectors fail before write;
every query is event-filtered and optional child-filtered in SQL; archived-child faces are excluded;
publication blocks on nonterminal visible assets.

**Delivered:** Migration `0005_face_index` conditionally enables pgvector, refuses already-published
events, and adds generation-scoped readiness, per-asset analysis state, and 128-dimensional
per-face vectors. The server accepts only strict canonical documents from the batch's originating
active workstation, validates event/sub-event/source/model/count/vector invariants before writes,
keeps identical retries duplicate-free, and requires audited per-photo reset for conflicts or five
failed attempts. Exact cosine search applies event and optional child predicates in SQL and excludes
archived children and gallery-excluded assets.

The desktop now has hash-verified Download/Verify and Locate Existing Files model setup, a third
sequential resumable face-index stage, metadata-only SQLite checkpoints, five-attempt recovery, and
face progress/blocker UI. The photographer dashboard reports indexed, no-face, pending, and blocked
counts; supports audited per-photo reset; and publication requires current-generation terminal face
analysis for every visible verified original. No clusters, people, crops, landmarks, filenames,
local paths, runtime details, or vectors are added to desktop checkpoints.

**Verification evidence:** The fast SQLite suite passes 159 tests with the pgvector-only query and
opt-in S3 provider tests skipped. A separate PostgreSQL 18 cluster with the installed vector
extension passes all eight Session 7 integration tests, including the exact cosine query. Ruff,
migration drift, Django system checks, and the repository check script pass. CI has a dedicated
`pgvector/pgvector:pg16` job. The first failing acceptance test now proves an otherwise-valid
sibling-sub-event upload returns `asset_not_found` and writes zero analysis/vector rows.

**First failing acceptance test:** Attempt to upload an otherwise valid face result to an asset in a
different tenant or sub-event and prove that no analysis/vector row is written.

## Session 8: owner/guest sharing, ephemeral search, and downloads

**Status:** ready; not started. Sessions 6–7 are accepted.

**Goal:** Complete customer discovery and delivery with independently scoped full-gallery links.

**Work:** Add a photographer-created owner capability for the main event. Let the owner create and
revoke full-gallery guest capabilities scoped to the whole event or one active sub-event. Give every
link an independent high-entropy URL, auto-generated four-digit PIN, required/capped expiry,
throttling, audit, and revocation. Implement exactly-one-usable-face input, ephemeral
processing/cleanup, scoped recall-first vector search as a gallery filter, deduplicated ranked
results, and visibility-derived signed original downloads.

**Done when:**

- Owner and guest permissions cannot widen through changed identifiers.
- Zero-face and multiple-face inputs are rejected clearly; exactly one usable face is required.
- Raw input/crop is absent from object storage, database, logs, diagnostics, analytics, and error
  paths after every success/failure/cancellation.
- Every guest can browse/download all scoped visible photos; search returns only photos in that same
  scope and never changes download authorization.
- Expired/revoked links and residual sessions fail; downloaded original hashes match uploads.

**First failing acceptance test:** Use a valid sub-event capability to search with an embedding that
would match a photo in another sub-event and prove the photo is absent and no signed URL is issued.

## Session 9: production hardening and launch rehearsal

**Status:** not started.

**Goal:** Turn the feature-complete pilot into an operable release.

**Work:** Finalize Railway/R2/Supabase regions and secrets, wildcard DNS/TLS, trusted proxies,
headers/cookies, secret scanning, monitoring, cost limits, backups/restores, retention deletion,
privacy/legal notices, incident runbooks, native packaging, performance tuning, licence decisions,
and a clean end-to-end rehearsal.

**Done when:** Empty-database/bucket deployment passes; backup restores separately; upload outage,
link leak/revocation, pepper rotation, and retention deletion are rehearsed; Windows/macOS packages
pass; the client walkthrough is accepted; the release commit is tagged.

## Handoff template

At the end of every session record:

1. User-visible behavior delivered.
2. Contracts, schema/migrations, files, variables, and external resources changed.
3. Commands/checks run and exact results.
4. Known limitations and preserved invariants.
5. The next session's first failing acceptance test.

Session 8 may begin against ADRs 0009–0011. Do not begin a later session while an earlier authorization,
privacy-cleanup, tenant, sub-event, checksum, or lifecycle test is failing.
