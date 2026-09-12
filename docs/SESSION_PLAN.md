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

**Status (2026-09-10):** Complete. The Django browser slice now resolves photographer tenants
from the host, enforces active memberships, provides a tenant-filtered dashboard, protects
Published events with Argon2 PINs and event-scoped signed cookies, rate limits authentication
failures in the database, and records immutable security audit events. ADR 0002 records the
interview decisions and accepted PIN risk.

**Goal:** Establish the authorization boundary before accepting media.

**Work:** Implement photographer membership, events, state transitions, admin provisioning,
host/subdomain resolution, photographer login, visitor PIN sessions, Argon2, and initial audit
events. Add migrations and cross-tenant denial tests.

**Done when:** An administrator can create the pilot photographer and event; the photographer sees
only assigned events; a visitor can unlock only a matching published sample event; altered tenant,
event, and host identifiers are denied.

**Handoff:**

1. Administrators can provision the user, photographer, membership, and event; drive legal event
   transitions; rotate the write-only PIN or public token; and revoke visitor sessions. A
   photographer can sign in on its own subdomain and see only that tenant's events. A visitor sees
   event metadata only after unlocking a matching, unexpired Published event.
2. Migration `events/0001_initial.py`, server-rendered templates, one static stylesheet, host and
   request middleware, admin controls, `AUTH_FAILURE_LIMIT`, and
   `AUTH_FAILURE_WINDOW_SECONDS` were added. The container now collects static files.
3. `./scripts/check.sh` passes with 23 tests, and a fresh in-memory database applies all Django and
   events migrations. Static-file discovery succeeds in dry-run mode.
4. Six-digit PINs retain the accepted database-only offline-guessing risk. Trusted proxy client-IP
   handling waits for Session 9. Sessions 4 and 5 must add manifest and derivative readiness to the
   publication gate. Desktop access and refresh tokens remain Session 4 scope.
5. Session 3's first failing acceptance test should scan a synthetic nested directory and report
   exact accepted/rejected JPEG counts and bytes while persisting enough SQLite state to resume the
   same scan after restart.

## Session 3: Desktop discovery and durable local state

**Status (2026-09-10):** Complete. ADR 0003 makes five-to-ten-device concurrent contribution a
required event boundary. Each device owns durable event-scoped contribution batches; the server will
own aggregate quota, intake closure, and final manifest reconciliation in Session 4.

**Goal:** Reliably inventory a client folder before any network transfer.

**Work:** Build honest lead/invitation and event-selection scaffolding, recursive folder plus
single/multiple-file discovery, extension/content/decode validation, size limits, checksums, the
per-installation event/batch SQLite checkpoint, and folder/validation/progress screens. Keep
processing and upload services behind interfaces. Use explicit synthetic demo mode until Session 4
implements network identity.

**Done when:** The app reports exact accepted/rejected counts and bytes, survives a forced exit,
detects changed files, and resumes without decoding or hashing completed unchanged assets. Ten
independent device checkpoints targeting one event must create distinct batch/asset identities.

**Handoff:**

1. `python -m openfotos_desktop --demo` now opens a functional synthetic event. A user can add
   recursive folders, one or multiple files, mix selection types, pause/resume background scans,
   review exact accepted/rejected counts and bytes, approve a frozen contribution, export redacted
   diagnostics, and explicitly remove local event metadata without deleting source photographs.
2. The desktop now has a versioned WAL SQLite checkpoint, stable scan/batch states and reason codes,
   full JPEG decode plus 100 MiB/120 MP limits, SHA-256, changed-file review, verified source-root
   relocation, per-user data paths, a single-instance lock, explicit network/processing/upload ports,
   and a redacted representative-data benchmark command. Pillow was added to desktop/dev
   dependencies and CI now installs the desktop extra.
3. `./scripts/check.sh` passes with 44 tests, including a real subprocess termination and WAL
   recovery, ten independent installation stores, duplicate-byte policy, link exclusion, profile and
   capacity failures, diagnostic privacy, Qt offscreen behavior, all Session 2 tests, and Django's
   system check. The demo window also remained healthy through a headless launch smoke test.
4. No authentication, invitation redemption, device token, cloud reservation, processing, or upload
   occurs in Session 3. Native Windows/macOS smoke/package runs and a real 10,000-photo hardware
   benchmark remain release gates. A timestamp-preserving content mutation may use the fast local
   metadata path; Session 4 must checksum bytes while transferring and let the server reject any
   mismatch. Network shares and cloud placeholder folders remain unsupported.
5. Session 4's first failing acceptance test should enroll ten simulated device sessions into one
   event, submit concurrent immutable contribution reservations, prove the transactionally reserved
   byte total never exceeds the event allowance, deny cross-device batch details, and permit only the
   lead to close intake and finalize after all batches are terminal or explicitly excluded.

## Session 4: Direct upload and manifest reconciliation

**Status (2026-09-11):** Complete. ADRs 0004 and 0005 replace prefix-wide upload credentials with
exact-object PUT leases and record the device, quota, revocation, intake-generation, reconciliation,
and publication-gate decisions.

**Goal:** Move originals and metadata safely from desktop to private R2.

**Work:** Implement lead authentication plus timed uploader invitations, per-device event-scoped
sessions, server-owned object keys, immutable contribution manifests, atomic event quota
reservation, one-to-four-way resumable transfers per device, credential refresh, retries,
idempotency records, per-variant completion, intake closure, and lead-owned aggregate manifest
validation. Use an S3-compatible local test service or fakes before real R2.

**Done when:** An interrupted upload resumes without repeating verified assets, clients cannot
choose object keys, byte/checksum limits are enforced, and the server reconciles a complete test
manifest while ten simulated clients race on one event without exceeding quota. Finalization is
lead-only, requires closed intake, and rejects nonterminal contributions.

**Handoff:**

1. Normal desktop mode now signs in a photographer or redeems a 72-hour uploader invitation,
   persists rotating refresh tokens only in the operating-system credential store, caches
   event/device scope, reserves a frozen contribution, streams one to four private original uploads,
   pauses between objects, and resumes at the verified-object boundary.
2. Django owns exact object keys, transactional event quota, the one-to-ten active contribution
   device cap, invitation/device revocation, strict JSON and idempotency records, upload leases,
   `HeadObject` reconciliation, reasoned lead exclusions, intake generations, and immutable
   generation manifests. Publication now requires both the current committed ingestion manifest and
   matching derivative readiness.
3. The S3 adapter signs exact five-minute PUT operations with length, Content-MD5, JPEG type,
   create-only semantics, and SHA-256 metadata. The same synthetic provider contract passed against
   local MinIO and Cloudflare R2. R2 Object Read & Write credentials are sufficient; no Cloudflare
   account API token is required.
4. `./scripts/check.sh` passes with 70 deterministic tests plus one opt-in provider test. Session 4
   authentication, ten-client quota locking, upload/reconciliation, and migrations also passed
   against PostgreSQL 18.6 rather than SQLite.
5. Native Windows/macOS packaging and the representative 10,000-photo rehearsal remain release
   gates. Session 5's first failing acceptance test should prove a derivative object loses GPS and
   nonessential EXIF while its original object's checksum remains unchanged, then mark derivative
   readiness only for the current ingestion generation.

## Session 5: Image derivatives and private gallery

**Status (2026-09-12):** Complete. ADR 0006 records the immutable optional preview policy,
desktop-owned derivative profile, exact private GET authorization, retry/exclusion workflow,
gallery presentation, publication gate, and accepted screenshot/residual-URL risks.

**Goal:** Publish fast, authorized browsing assets while preserving uploaded originals.

**Work:** Apply EXIF orientation, generate clean thumbnails and optionally watermarked previews,
remove derivative metadata, add gallery pagination/lightbox pages, authorize short-lived object
URLs, and implement download policy plumbing.

**Done when:** Authorized visitors can browse private derivatives, unauthorized and expired
sessions cannot obtain URLs, enabled previews contain the selected watermark while disabled ones
remain clean, derivatives contain no GPS data, and original checksums remain unchanged.

**Handoff:**

1. The lead desktop now provides a clean-by-default preview settings screen with a local sample,
   built-in OFTS or custom transparent logo, Unicode text, and four fixed layouts. Confirmation is
   explicit, event-scoped, immutable, stored by Django, and readable by contributor installations.
2. One restart-safe sync uploads originals and then produces a 2048/q85 preview and clean 512/q78
   thumbnail. It applies orientation, converts to sRGB, strips derivative metadata, validates source
   and derivative checksums, can read back only the same contributor's exact original, and removes
   private temporary files after server verification.
3. Django validates derivative manifests, issues exact PUT/GET URLs, reconciles readiness at both
   derivative completion and finalization, and permits audited gallery exclusion only after five
   failures. Publication requires the current manifest, confirmed policy, and complete non-excluded
   derivatives.
4. The photographer dashboard supports review, failure/exclusion status, publish/unpublish, and the
   three-mode original-download policy. Authorized visitors receive a 48-item masonry gallery and
   standalone preview navigation with private/no-store HTML and five-minute signed media URLs;
   uploaded filenames and original URLs are absent.
5. Original downloads and explicit share enforcement remain Session 8. Disabling downloads cannot
   prevent saving or screenshotting an authorized 2048 preview, and a revoked five-minute signed URL
   can remain usable until expiry. Native Windows/macOS packaging and representative visual review
   remain release gates. Session 6's first failing acceptance test should reject a recognizer whose
   model/hash/embedding contract differs from the event before persisting any face vector.

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
domain checks, secret scanning, monitoring, database backup/restore, Windows/macOS packaging, failure
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
